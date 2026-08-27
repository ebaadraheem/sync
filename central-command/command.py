"""
Agentic Bridge - Central Command

  1. Hosts the WebSocket server to which devices are connected.
  2. Plans and dispatches actions from prompts.

Devices (laptop agent, phone agent) connect in, register a name, and either:
  - respond to action requests central sends them, or
  - send a "prompt" message, which central plans and dispatches on their
    behalf, replying with the final result.

Run:
    pip install websockets zeroconf --break-system-packages
    CENTRAL_TOKEN=dev-secret-change-me python3 central_command.py
"""

import asyncio
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone

import websockets

PORT = int(os.environ.get("CENTRAL_PORT", "8765"))
AUTH_TOKEN = os.environ.get("CENTRAL_TOKEN", "dev-secret-change-me")
ADVERTISE = os.environ.get("CENTRAL_ADVERTISE", "1") != "0"
SERVICE_NAME = os.environ.get("CENTRAL_SERVICE_NAME", "central-command")
DEFAULT_ACTION_TIMEOUT = 15


def log(*args):
    print(f"[central {datetime.now(timezone.utc).isoformat()}]", *args, file=sys.stderr)

# ---------------------------------------------------------------------------
# Planner - decides which device + action a prompt maps to. Swap for an LLM-backed planner
# later without touching the connection/routing code below.
# ---------------------------------------------------------------------------

class RulePlanner:
    KNOWN_DEVICES = ["laptop", "phone"]

    def plan(self, prompt: str, requester: str):
        p = prompt.lower()

        device = requester if requester in self.KNOWN_DEVICES else "laptop"
        for d in self.KNOWN_DEVICES:
            if d in p:
                device = d
                break

        if "screenshot" in p or "screen shot" in p:
            return [{"device": device, "action": "screenshot", "params": {}}]

        if "photo" in p or "camera" in p:
            return [{"device": device, "action": "take_photo", "params": {}}]

        if "location" in p or "gps" in p:
            return [{"device": device, "action": "get_location", "params": {}}]

        if "clipboard" in p:
            if "set" in p or "copy" in p:
                m = re.search(r"(?:to|copy)\s+['\"](.+?)['\"]", prompt)
                text = m.group(1) if m else prompt
                return [{"device": device, "action": "set_clipboard", "params": {"text": text}}]
            return [{"device": device, "action": "get_clipboard", "params": {}}]

        run_match = re.search(r"run(?: shell)?\s*[:\-]?\s*(.+)", prompt, re.IGNORECASE)
        if run_match and device == "laptop":
            return [{"device": device, "action": "run_shell", "params": {"command": run_match.group(1)}}]

        list_match = re.search(r"list(?: files| dir)?(?: in)?\s+(.+)", prompt, re.IGNORECASE)
        if list_match:
            return [{"device": device, "action": "list_dir", "params": {"path": list_match.group(1).strip()}}]

        if "ping" in p:
            return [{"device": device, "action": "ping", "params": {}}]

        raise ValueError(
            f"couldn't parse prompt: '{prompt}'. Try: 'screenshot on laptop', "
            f"'take a photo on phone', 'get clipboard from laptop', 'ping phone'"
        )


# ---------------------------------------------------------------------------
# Central Command server
# ---------------------------------------------------------------------------

class CentralCommand:
    def __init__(self, planner):
        self.planner = planner
        self.clients: dict[str, "websockets.ServerConnection"] = {}
        self.pending: dict[str, asyncio.Future] = {}

    async def send(self, name: str, obj: dict) -> bool:
        ws = self.clients.get(name)
        if not ws:
            return False
        await ws.send(json.dumps(obj))
        return True

    async def call_action(self, device: str, action: str, params: dict, timeout=DEFAULT_ACTION_TIMEOUT):
        if device not in self.clients:
            return {"status": "error", "error": f"'{device}' is not connected"}

        req_id = str(uuid.uuid4())
        fut = asyncio.get_event_loop().create_future()
        self.pending[req_id] = fut

        await self.send(device, {"id": req_id, "to": device, "action": action, "params": params})

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self.pending.pop(req_id, None)
            return {"status": "error", "error": f"timed out waiting for '{device}' ({timeout}s)"}

    async def handle_prompt(self, requester: str, prompt: str):
        try:
            plan = self.planner.plan(prompt, requester)
        except Exception as e:
            return {"status": "error", "error": str(e)}

        results = []
        for step in plan:
            log(f"dispatching -> device={step['device']} action={step['action']} params={step['params']}")
            result = await self.call_action(step["device"], step["action"], step["params"])
            results.append({"step": step, "result": result})

        return {"status": "ok", "results": results}

    async def handle_connection(self, ws):
        name = None
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await ws.send(json.dumps({"type": "error", "error": "invalid_json"}))
                    continue

                # --- Registration ---
                if msg.get("type") == "register":
                    if msg.get("token") != AUTH_TOKEN:
                        await ws.send(json.dumps({"type": "error", "error": "bad_token"}))
                        await ws.close()
                        return
                    name = msg.get("name")
                    if not name:
                        await ws.send(json.dumps({"type": "error", "error": "missing_name"}))
                        continue
                    self.clients[name] = ws
                    log(f"registered '{name}'. peers online: {list(self.clients.keys())}")
                    await ws.send(json.dumps({
                        "type": "registered", "name": name, "peers": list(self.clients.keys())
                    }))
                    continue

                if not name:
                    await ws.send(json.dumps({"type": "error", "error": "not_registered"}))
                    continue

                # --- A device originating a prompt ("do X") ---
                if msg.get("type") == "prompt":
                    text = msg.get("text", "")
                    log(f"prompt from '{name}': {text!r}")
                    outcome = await self.handle_prompt(name, text)
                    await self.send(name, {
                        "type": "prompt_result", "to": name, "id": msg.get("id"), **outcome
                    })
                    continue

                # --- A device replying to an action request central sent it ---
                if msg.get("type") == "action_result":
                    mid = msg.get("id")
                    fut = self.pending.pop(mid, None)
                    if fut and not fut.done():
                        fut.set_result(msg)
                    continue

                await ws.send(json.dumps({"type": "error", "error": "unrecognized_message"}))

        except websockets.ConnectionClosed:
            pass
        finally:
            if name and self.clients.get(name) is ws:
                del self.clients[name]
                log(f"'{name}' disconnected")


def start_mdns_advertisement():
    """Advertise this process on the local network via mDNS so agents can
    find it without a hardcoded IP. Returns a cleanup function."""
    from zeroconf import ServiceInfo, Zeroconf
    import socket

    zeroconf = Zeroconf()
    
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
    except OSError:
        local_ip = "127.0.0.1"
    finally:
        s.close()

    info = ServiceInfo(
        "_agentic-bridge._tcp.local.",
        f"{SERVICE_NAME}._agentic-bridge._tcp.local.",
        addresses=[socket.inet_aton(local_ip)],
        port=PORT,
        properties={"token_required": "true"},
    )
    zeroconf.register_service(info)
    log(f"advertising via mDNS as '{SERVICE_NAME}' at {local_ip}:{PORT}")

    def cleanup():
        zeroconf.unregister_service(info)
        zeroconf.close()

    return cleanup


async def main():
    planner = RulePlanner()
    central = CentralCommand(planner)

    cleanup_mdns = start_mdns_advertisement() if ADVERTISE else (lambda: None)

    async with websockets.serve(central.handle_connection, "0.0.0.0", PORT):
        log(f"central command listening on ws://0.0.0.0:{PORT}")
        try:
            await asyncio.Future()  # run forever
        finally:
            cleanup_mdns()


if __name__ == "__main__":
    asyncio.run(main())
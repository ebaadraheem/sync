"""
Laptop Agent

Connects to Central Command and registers as "laptop". Waits for action
requests, executes them locally, and sends the result back.

On startup it negotiates a transport (local network, then bluetooth, then
remote relay - see shared/transport.py) rather than connecting to one
fixed address. Whichever one succeeds is used for the whole session.

Run:
    pip install websockets zeroconf --break-system-packages
    CENTRAL_TOKEN=dev-secret-change-me python3 agent.py
"""

import asyncio
import base64
import json
import os
import platform
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime, timezone

import websockets

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
from transport import negotiate_transport

CENTRAL_TOKEN = os.environ.get("CENTRAL_TOKEN", "dev-secret-change-me")
AGENT_NAME = os.environ.get("AGENT_NAME", "laptop")

LOCAL_URL = os.environ.get("CENTRAL_LOCAL_URL")
REMOTE_URL = os.environ.get("CENTRAL_REMOTE_URL")

# Cap how much output we'll ever send back for one action, so a runaway
# command or huge file can't flood the relay / orchestrator.
MAX_RESULT_BYTES = 2_000_000


def log(*args):
    print(f"[laptop-agent {datetime.now(timezone.utc).isoformat()}]", *args, file=sys.stderr)

# ---------------------------------------------------------------------------
# Action implementations. Each takes a `params` dict and returns a JSON-
# serializable result dict. Raise an exception to report failure.
# ---------------------------------------------------------------------------

def action_ping(params):
    return {"pong": True, "platform": platform.platform()}


def action_run_shell(params):
    """Run a shell command and return stdout/stderr/exit code.

    SECURITY NOTE: this executes arbitrary commands sent over the relay.
    Only run this agent against a relay + token you control, and treat the
    token like a password. For anything beyond personal experimentation,
    replace this with an allowlist of specific commands.
    """
    cmd = params.get("command")
    if not cmd:
        raise ValueError("missing 'command' param")
    timeout = params.get("timeout", 20)

    proc = subprocess.run(
        cmd, shell=True, capture_output=True, text=True, timeout=timeout
    )
    return {
        "exit_code": proc.returncode,
        "stdout": proc.stdout[-MAX_RESULT_BYTES:],
        "stderr": proc.stderr[-MAX_RESULT_BYTES:],
    }


def action_screenshot(params):
    """Take a screenshot and return it as base64 PNG."""
    with tempfile.TemporaryDirectory() as tmp:
        out_path = os.path.join(tmp, "shot.png")
        system = platform.system()

        if system == "Darwin":
            subprocess.run(["screencapture", "-x", out_path], check=True)
        elif system == "Linux":
            # Requires one of these installed: gnome-screenshot, scrot, or import (imagemagick)
            for cmd in (
                ["gnome-screenshot", "-f", out_path],
                ["scrot", out_path],
                ["import", "-window", "root", out_path],
            ):
                try:
                    subprocess.run(cmd, check=True, capture_output=True)
                    break
                except (FileNotFoundError, subprocess.CalledProcessError):
                    continue
            else:
                raise RuntimeError(
                    "no screenshot tool found (tried gnome-screenshot, scrot, import)"
                )
        elif system == "Windows":
            # Requires: pip install pillow --break-system-packages
            from PIL import ImageGrab
            img = ImageGrab.grab()
            img.save(out_path)
        else:
            raise RuntimeError(f"unsupported platform: {system}")

        with open(out_path, "rb") as f:
            data = f.read()

    if len(data) > MAX_RESULT_BYTES:
        raise RuntimeError(
            f"screenshot too large ({len(data)} bytes) to send inline; "
            "add upload-to-storage support for large payloads"
        )

    return {"mime_type": "image/png", "data_base64": base64.b64encode(data).decode()}


def action_read_file(params):
    path = params.get("path")
    if not path:
        raise ValueError("missing 'path' param")
    path = os.path.expanduser(path)
    with open(path, "rb") as f:
        data = f.read()
    if len(data) > MAX_RESULT_BYTES:
        raise RuntimeError(f"file too large ({len(data)} bytes)")
    return {"path": path, "size": len(data), "data_base64": base64.b64encode(data).decode()}


def action_write_file(params):
    path = params.get("path")
    data_b64 = params.get("data_base64")
    text = params.get("text")
    if not path:
        raise ValueError("missing 'path' param")
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    if data_b64 is not None:
        with open(path, "wb") as f:
            f.write(base64.b64decode(data_b64))
    elif text is not None:
        with open(path, "w") as f:
            f.write(text)
    else:
        raise ValueError("provide either 'data_base64' or 'text'")

    return {"path": path, "written": True}


def action_list_dir(params):
    path = os.path.expanduser(params.get("path", "~"))
    entries = []
    with os.scandir(path) as it:
        for entry in it:
            entries.append({
                "name": entry.name,
                "is_dir": entry.is_dir(),
                "size": entry.stat().st_size if entry.is_file() else None,
            })
    return {"path": path, "entries": entries}


def action_get_clipboard(params):
    system = platform.system()
    if system == "Darwin":
        out = subprocess.run(["pbpaste"], capture_output=True, text=True, check=True)
        return {"text": out.stdout}
    elif system == "Linux":
        try:
            out = subprocess.run(["xclip", "-selection", "clipboard", "-o"],
                                  capture_output=True, text=True, check=True)
            return {"text": out.stdout}
        except FileNotFoundError:
            raise RuntimeError("xclip not installed (sudo apt install xclip)")
    elif system == "Windows":
        import pyperclip  # pip install pyperclip --break-system-packages
        return {"text": pyperclip.paste()}
    raise RuntimeError(f"unsupported platform: {system}")


def action_set_clipboard(params):
    text = params.get("text", "")
    system = platform.system()
    if system == "Darwin":
        subprocess.run(["pbcopy"], input=text, text=True, check=True)
    elif system == "Linux":
        subprocess.run(["xclip", "-selection", "clipboard"], input=text, text=True, check=True)
    elif system == "Windows":
        import pyperclip
        pyperclip.copy(text)
    else:
        raise RuntimeError(f"unsupported platform: {system}")
    return {"set": True}


ACTIONS = {
    "ping": action_ping,
    "run_shell": action_run_shell,
    "screenshot": action_screenshot,
    "read_file": action_read_file,
    "write_file": action_write_file,
    "list_dir": action_list_dir,
    "get_clipboard": action_get_clipboard,
    "set_clipboard": action_set_clipboard,
}


# ---------------------------------------------------------------------------
# Connection / message handling
# ---------------------------------------------------------------------------

async def handle_message(transport, msg):
    if msg.get("type") == "registered":
        log(f"registered as '{msg['name']}'. peers currently online: {msg.get('peers')}")
        return

    if msg.get("type") == "error":
        log("central command error:", msg)
        return

    action_name = msg.get("action")
    if not action_name:
        return  # not an action request, ignore

    handler = ACTIONS.get(action_name)
    reply = {
        "type": "action_result",
        "id": msg["id"],
        "action": action_name,
    }

    if not handler:
        reply["status"] = "error"
        reply["error"] = f"unknown action '{action_name}'"
    else:
        try:
            log(f"executing '{action_name}' (id={msg['id']}) params={msg.get('params')}")
            result = handler(msg.get("params", {}) or {})
            reply["status"] = "ok"
            reply["result"] = result
        except Exception as e:
            log(f"action '{action_name}' failed:", traceback.format_exc())
            reply["status"] = "error"
            reply["error"] = str(e)

    await transport.send(reply)


async def run():
    while True:
        try:
            transport = await negotiate_transport(local_url=LOCAL_URL, remote_url=REMOTE_URL)
        except RuntimeError as e:
            log(f"no transport available ({e}), retrying in 5s...")
            await asyncio.sleep(5)
            continue

        log(f"connected via {transport.method} as '{AGENT_NAME}'")
        try:
            await transport.send({"type": "register", "name": AGENT_NAME, "token": CENTRAL_TOKEN})
            async for msg in transport:
                await handle_message(transport, msg)
        except websockets.ConnectionClosed:
            log("connection closed, renegotiating transport in 2s...")
            await asyncio.sleep(2)
            continue


if __name__ == "__main__":
    asyncio.run(run())
"""
Simulates a device (e.g. your laptop's own UI, or the phone app) sending a
prompt directly to central command and waiting for the result - this is
the piece that didn't exist before ("only the orchestrator's own terminal
could originate requests"). Any connected device can now do this.
"""
import asyncio
import json
import sys
import uuid
import websockets

CENTRAL_URL = "ws://localhost:8765"
TOKEN = "dev-secret-change-me"


async def send_prompt(device_name: str, prompt_text: str):
    async with websockets.connect(CENTRAL_URL) as ws:
        await ws.send(json.dumps({"type": "register", "name": device_name, "token": TOKEN}))
        reg_reply = json.loads(await ws.recv())
        print(f"[{device_name}] {reg_reply}")

        req_id = str(uuid.uuid4())
        await ws.send(json.dumps({"type": "prompt", "id": req_id, "text": prompt_text}))

        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") == "prompt_result":
                print(f"\n[{device_name}] PROMPT: {prompt_text!r}")
                print(json.dumps(msg, indent=2)[:2000])
                return


if __name__ == "__main__":
    device = sys.argv[1] if len(sys.argv) > 1 else "laptop-ui"
    prompt = sys.argv[2] if len(sys.argv) > 2 else "ping laptop"
    asyncio.run(send_prompt(device, prompt))
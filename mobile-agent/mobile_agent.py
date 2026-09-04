"""
Agentic Bridge - Mock Phone Agent

Stands in for the real React Native mobile app so central command's
multi-device planning/dispatch logic can be built and tested before the
real app exists. Connects and behaves exactly like a real phone agent
would (same registration, same message contract) - it just returns fake
data instead of touching real hardware.

Once the real RN app exists, this file becomes a reference for what
message shapes the app needs to reply with; it isn't meant to be kept
around as "the phone" forever.

Run:
    pip install websockets zeroconf --break-system-packages
    CENTRAL_TOKEN=dev-secret-change-me AGENT_NAME=phone python3 phone_agent.py
"""

import asyncio
import base64
import json
import os
import sys
import traceback
from datetime import datetime, timezone

import websockets

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
from transport import negotiate_transport

CENTRAL_TOKEN = os.environ.get("CENTRAL_TOKEN", "dev-secret-change-me")
AGENT_NAME = os.environ.get("AGENT_NAME", "phone")
LOCAL_URL = os.environ.get("CENTRAL_LOCAL_URL")
REMOTE_URL = os.environ.get("CENTRAL_REMOTE_URL")

# A tiny 1x1 red pixel PNG, base64-encoded - stands in for "a photo" so the
# result shape (mime_type + data_base64) matches what a real camera action
# would return.
FAKE_PHOTO_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def log(*args):
    print(f"[phone-agent(mock) {datetime.now(timezone.utc).isoformat()}]", *args, file=sys.stderr)


# ---------------------------------------------------------------------------
# Mock actions - same shape a real phone action would return, fake data
# ---------------------------------------------------------------------------

def action_ping(params):
    return {"pong": True, "platform": "mock-android"}


def action_take_photo(params):
    return {"mime_type": "image/png", "data_base64": FAKE_PHOTO_PNG_B64, "note": "mock photo, not a real camera"}


def action_get_location(params):
    return {"latitude": 32.1877, "longitude": 74.1945, "accuracy_m": 12, "note": "mock location (Gujranwala)"}


def action_get_clipboard(params):
    return {"text": "(mock) this is fake clipboard text from the phone"}


def action_set_clipboard(params):
    return {"set": True, "note": f"(mock) pretended to set clipboard to: {params.get('text', '')!r}"}


def action_list_notifications(params):
    return {"notifications": [
        {"app": "Messages", "title": "(mock) Ali", "body": "are we still on for later?"},
        {"app": "Calendar", "title": "(mock) Reminder", "body": "Team sync in 30 minutes"},
    ]}


ACTIONS = {
    "ping": action_ping,
    "take_photo": action_take_photo,
    "get_location": action_get_location,
    "get_clipboard": action_get_clipboard,
    "set_clipboard": action_set_clipboard,
    "list_notifications": action_list_notifications,
}


# ---------------------------------------------------------------------------
# Connection / message handling - identical pattern to laptop-agent/agent.py
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
        return

    handler = ACTIONS.get(action_name)
    reply = {"type": "action_result", "id": msg["id"], "action": action_name}

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
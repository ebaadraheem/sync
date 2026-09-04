"""
Agentic Bridge - Transport negotiation

On startup, an agent doesn't just connect to one fixed address - it tries
transports in priority order and uses whichever one actually works:

    1. Local network  - fastest, most capable, works when both devices
                         share a Wi-Fi network. Tried first.
    2. Bluetooth       - works with zero network infrastructure at all,
                         but short range and lower throughput. Tried when
                         local network isn't reachable.
    3. Remote relay    - a central-command instance reachable over the
                         public internet (e.g. on a VPS). Works from
                         anywhere, needs internet on both ends. Last
                         resort, since it's the only one with a running
                         cost / third-party dependency (your own server).

Whichever one succeeds, the caller gets back a plain object with
`.send(str)` / `.recv() -> str` / `.close()` - the rest of the agent code
never needs to know which transport won. That uniformity is the whole
point: adding a transport means adding one function here, nothing else in
agent.py changes.

IMPORTANT - Bluetooth status: the local-network and remote branches are
real, tested WebSocket connections. The Bluetooth branch is a documented
placeholder. Testing it for real needs physical Bluetooth radios on both
ends (this can't be verified in a sandbox or via unit tests the way the
other two can). See try_bluetooth() below for what a real implementation
needs. The negotiation *logic* itself - trying in order, falling through
on failure - is fully implemented and tested in test_transport.py using
mocked transport functions, independent of whether Bluetooth hardware
exists.
"""

import asyncio
import os
import sys
import json as _json

import websockets

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from discovery import discover_relay

LOCAL_DISCOVERY_TIMEOUT = float(os.environ.get("RELAY_DISCOVERY_TIMEOUT", "3"))
BLUETOOTH_TIMEOUT = float(os.environ.get("BLUETOOTH_TIMEOUT", "3"))
CONNECT_TIMEOUT = float(os.environ.get("TRANSPORT_CONNECT_TIMEOUT", "3"))


def log(*args):
    print("[transport]", *args, file=sys.stderr)


class WebSocketTransport:
    """Thin wrapper so callers use the same .send/.recv interface
    regardless of which transport actually connected."""

    def __init__(self, ws, method: str, address: str):
        self.ws = ws
        self.method = method     # "local_network" | "bluetooth" | "remote_relay"
        self.address = address   # human-readable, for logging

    async def send(self, obj: dict):
        await self.ws.send(_json.dumps(obj))

    async def recv(self) -> dict:
        raw = await self.ws.recv()
        return _json.loads(raw)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.recv()
        except websockets.ConnectionClosed:
            raise StopAsyncIteration

    async def close(self):
        await self.ws.close()


# ---------------------------------------------------------------------------
# Individual transport attempts. Each either returns a connected transport
# object, or None if that transport isn't usable right now. None of these
# raise for the "not available" case - only for genuine bugs.
# ---------------------------------------------------------------------------

async def try_local_network(explicit_url: str | None):
    """Try an explicit local RELAY_URL if given, else mDNS discovery."""
    url = explicit_url
    if not url:
        log("trying local network (mDNS discovery)...")
        loop = asyncio.get_event_loop()
        url = await loop.run_in_executor(None, discover_relay, LOCAL_DISCOVERY_TIMEOUT)
        if not url:
            log("local network: no relay found via mDNS")
            return None
    else:
        log(f"trying local network (explicit url {url})...")

    try:
        ws = await asyncio.wait_for(websockets.connect(url), timeout=CONNECT_TIMEOUT)
        log(f"local network: connected to {url}")
        return WebSocketTransport(ws, "local_network", url)
    except Exception as e:
        log(f"local network: connect failed ({e})")
        return None


async def try_bluetooth():
    """PLACEHOLDER - not implemented, no hardware to test against in this
    environment. A real implementation would:

    1. Scan for nearby devices advertising the app's Bluetooth service
       (e.g. using `bleak` for cross-platform BLE, or `pybluez` for
       classic RFCOMM on Linux).
    2. Open a socket/GATT connection to the discovered peer.
    3. Wrap that socket in the same send/recv(dict) interface as
       WebSocketTransport above, framing JSON messages with a delimiter
       (e.g. newline) since raw sockets don't have WebSocket's built-in
       message framing.

    Returns None unconditionally today, so the negotiation chain always
    falls through to remote_relay when local network isn't available.
    """
    log("trying bluetooth... (not implemented in this build, skipping)")
    await asyncio.sleep(0)  # keep this a real coroutine
    return None


async def try_remote_relay(url: str | None):
    if not url:
        log("remote relay: no URL configured, skipping")
        return None
    log(f"trying remote relay ({url})...")
    try:
        ws = await asyncio.wait_for(websockets.connect(url), timeout=CONNECT_TIMEOUT)
        log(f"remote relay: connected to {url}")
        return WebSocketTransport(ws, "remote_relay", url)
    except Exception as e:
        log(f"remote relay: connect failed ({e})")
        return None


# ---------------------------------------------------------------------------
# Negotiation - the piece that ties the three attempts together.
# Overridable via `attempts=` for testing without real network/hardware.
# ---------------------------------------------------------------------------

async def negotiate_transport(
    local_url: str | None = None,
    remote_url: str | None = None,
    attempts=None,
) -> WebSocketTransport:
    """Try local network, then bluetooth, then remote relay, in that
    order. Returns the first successful transport. Raises RuntimeError if
    none succeed.

    `attempts` lets tests inject fake attempt functions instead of the
    real network/Bluetooth code - see test_transport.py.
    """
    if attempts is None:
        attempts = [
            ("local_network", lambda: try_local_network(local_url)),
            ("bluetooth", try_bluetooth),
            ("remote_relay", lambda: try_remote_relay(remote_url)),
        ]

    for name, attempt_fn in attempts:
        result = await attempt_fn()
        if result is not None:
            log(f"negotiation complete: using '{name}'")
            return result

    raise RuntimeError(
        "no transport available - local network, bluetooth, and remote relay all failed"
    )
"""
Agentic Bridge - Local network discovery

Finds the relay server via mDNS/Bonjour instead of requiring a hardcoded
IP address. This is what makes "local wifi mode" actually convenient:
your laptop's IP can change across reboots/networks and agents still find
it, as long as they're on the same LAN.

Falls back cleanly to an explicit RELAY_URL if discovery fails or is
disabled — this is how "local wifi" and "remote wifi" (VPS relay) modes
coexist without agents needing separate code paths.

Usage:
    url = resolve_relay_url()   # tries discovery, falls back to env var
"""

import os
import socket
import time

from zeroconf import ServiceBrowser, Zeroconf

SERVICE_TYPE = "_agentic-bridge._tcp.local."
DISCOVERY_TIMEOUT = float(os.environ.get("RELAY_DISCOVERY_TIMEOUT", "3"))


def log(*args):
    print("[discovery]", *args)


class _Listener:
    def __init__(self):
        self.found_url = None

    def add_service(self, zeroconf, service_type, name):
        info = zeroconf.get_service_info(service_type, name)
        if not info:
            return
        # info.addresses is a list of packed IPv4/IPv6 bytes; take the first
        addr = socket.inet_ntoa(info.addresses[0])
        self.found_url = f"ws://{addr}:{info.port}"
        log(f"found relay '{name}' at {self.found_url}")

    def update_service(self, *args, **kwargs):
        pass

    def remove_service(self, *args, **kwargs):
        pass


def discover_relay(timeout=DISCOVERY_TIMEOUT):
    """Browse the local network for an advertised relay. Returns a
    ws://host:port URL, or None if nothing was found within `timeout`
    seconds."""
    zeroconf = Zeroconf()
    listener = _Listener()
    browser = ServiceBrowser(zeroconf, SERVICE_TYPE, listener)

    try:
        deadline = time.time() + timeout
        while time.time() < deadline and listener.found_url is None:
            time.sleep(0.1)
        return listener.found_url
    finally:
        zeroconf.close()


def resolve_relay_url():
    """Decide which relay URL to connect to, in priority order:

    1. RELAY_URL env var, if explicitly set — always wins (e.g. you want
       to force remote/VPS mode, or you're pointing at a specific IP).
    2. mDNS discovery on the local network — "local wifi mode".
    3. ws://localhost:8765 — last-resort default for same-machine testing.
    """
    explicit = os.environ.get("RELAY_URL")
    if explicit:
        log(f"using explicit RELAY_URL={explicit}")
        return explicit

    log(f"no RELAY_URL set, searching local network ({DISCOVERY_TIMEOUT}s)...")
    found = discover_relay()
    if found:
        return found

    log("no relay found via mDNS, falling back to ws://localhost:8765")
    return "ws://localhost:8765"


if __name__ == "__main__":
    # Quick manual test: python3 discovery.py
    print(resolve_relay_url())
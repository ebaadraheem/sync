"""
Tests for negotiate_transport()'s fallback logic, using fake attempt
functions instead of real network/Bluetooth calls. This proves the
*decision-making* (try in order, stop at first success, fail loudly if
all fail) is correct, independent of whether local network, Bluetooth,
or a remote relay are actually reachable in whatever environment this
runs in.

Run:
    python3 test_transport.py
"""

import asyncio

from transport import negotiate_transport


class FakeTransport:
    def __init__(self, method):
        self.method = method


def make_attempt(name, succeeds):
    """Returns a fake attempt function that records whether it was called,
    and either returns a FakeTransport or None."""
    calls = {"count": 0}

    async def attempt():
        calls["count"] += 1
        return FakeTransport(name) if succeeds else None

    attempt.calls = calls
    return attempt


async def test_uses_first_available():
    """If local network succeeds, bluetooth and remote should never even
    be tried."""
    local = make_attempt("local_network", succeeds=True)
    bluetooth = make_attempt("bluetooth", succeeds=True)
    remote = make_attempt("remote_relay", succeeds=True)

    result = await negotiate_transport(attempts=[
        ("local_network", local), ("bluetooth", bluetooth), ("remote_relay", remote),
    ])

    assert result.method == "local_network", f"expected local_network, got {result.method}"
    assert local.calls["count"] == 1, "local should have been tried"
    assert bluetooth.calls["count"] == 0, "bluetooth should NOT have been tried - local already succeeded"
    assert remote.calls["count"] == 0, "remote should NOT have been tried - local already succeeded"
    print("PASS: test_uses_first_available")


async def test_falls_through_to_bluetooth():
    """If local network fails but bluetooth succeeds, bluetooth should be
    used and remote should never be tried."""
    local = make_attempt("local_network", succeeds=False)
    bluetooth = make_attempt("bluetooth", succeeds=True)
    remote = make_attempt("remote_relay", succeeds=True)

    result = await negotiate_transport(attempts=[
        ("local_network", local), ("bluetooth", bluetooth), ("remote_relay", remote),
    ])

    assert result.method == "bluetooth"
    assert local.calls["count"] == 1
    assert bluetooth.calls["count"] == 1
    assert remote.calls["count"] == 0, "remote should NOT have been tried - bluetooth already succeeded"
    print("PASS: test_falls_through_to_bluetooth")


async def test_falls_through_to_remote():
    """If local network and bluetooth both fail, remote relay should be
    used as the last resort."""
    local = make_attempt("local_network", succeeds=False)
    bluetooth = make_attempt("bluetooth", succeeds=False)
    remote = make_attempt("remote_relay", succeeds=True)

    result = await negotiate_transport(attempts=[
        ("local_network", local), ("bluetooth", bluetooth), ("remote_relay", remote),
    ])

    assert result.method == "remote_relay"
    assert local.calls["count"] == 1
    assert bluetooth.calls["count"] == 1
    assert remote.calls["count"] == 1
    print("PASS: test_falls_through_to_remote")


async def test_raises_when_all_fail():
    """If every transport fails, negotiation should raise rather than
    silently returning nothing."""
    local = make_attempt("local_network", succeeds=False)
    bluetooth = make_attempt("bluetooth", succeeds=False)
    remote = make_attempt("remote_relay", succeeds=False)

    try:
        await negotiate_transport(attempts=[
            ("local_network", local), ("bluetooth", bluetooth), ("remote_relay", remote),
        ])
        assert False, "expected RuntimeError, negotiation returned normally"
    except RuntimeError as e:
        assert "no transport available" in str(e)
        print("PASS: test_raises_when_all_fail")


async def main():
    await test_uses_first_available()
    await test_falls_through_to_bluetooth()
    await test_falls_through_to_remote()
    await test_raises_when_all_fail()
    print("\nAll negotiation-logic tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
# Agentic Bridge

Laptop <-> mobile agentic system. Either device can send a prompt to
Central Command, which decides which device/action to call, dispatches
it, and returns the result to whichever device asked.

```
[Laptop Agent] <---\
                     \
                [Central Command] <--- prompts can originate from
                     /                  ANY connected device, not just
[Phone Agent]  <----/                   a separate terminal
```

**Status: Central Command, laptop agent, and a mock phone agent are built
and tested end-to-end, including a device sending a prompt and getting a
cross-device result back.** The real React Native phone app is the next
piece - `mobile-agent/phone_agent.py` is a stand-in that returns fake data
in the same shape a real phone action would.

## What's here

- `central-command/central_command.py` - **the merged server + brain.**
  Hosts the WebSocket server agents connect to, tracks who's connected,
  and plans/dispatches actions when any device sends a `"type": "prompt"`
  message. This replaces the earlier two-process relay+orchestrator setup
  - there was no real reason to split "route messages" and "decide what
  to do" across two processes/languages.
- `laptop-agent/agent.py` - connects to Central Command as `"laptop"`,
  executes actions (screenshot, shell command, read/write file, list dir,
  clipboard get/set).
- `mobile-agent/phone_agent.py` - **mock** phone agent. Same connection
  pattern as the laptop agent, but returns fake photos/location/clipboard
  data instead of touching real hardware. Exists so multi-device flows
  can be built and tested before the real RN app exists.
- `shared/transport.py` - **transport negotiation.** On startup, each
  agent tries local network, then Bluetooth, then a remote relay, in that
  order, and uses whichever one connects. See "Transport modes" below.
- `shared/discovery.py` - mDNS helper used by the local-network transport
  to find Central Command without a hardcoded IP.
- `shared/test_transport.py` - proves the negotiation fallback order
  (local -> bluetooth -> remote, stop at first success) is correct, using
  fake attempt functions instead of real network/Bluetooth calls.
- `central-command/simulate_device_prompt.py` - a small test client that
  connects as an arbitrary device and sends a prompt, for testing without
  typing into a REPL.

## Run it locally (all on one machine, for testing)

Terminal 1 - Central Command:
```bash
cd central-command
pip install websockets zeroconf --break-system-packages
CENTRAL_TOKEN=dev-secret-change-me CENTRAL_ADVERTISE=0 python3 central_command.py
```
(`CENTRAL_ADVERTISE=0` disables mDNS for a deterministic local test; leave
it on for real local-network use.)

Terminal 2 - laptop agent:
```bash
cd laptop-agent
pip install websockets zeroconf --break-system-packages
CENTRAL_TOKEN=dev-secret-change-me CENTRAL_LOCAL_URL=ws://localhost:8765 AGENT_NAME=laptop python3 agent.py
```

Terminal 3 - mock phone agent:
```bash
cd mobile-agent
pip install websockets zeroconf --break-system-packages
CENTRAL_TOKEN=dev-secret-change-me CENTRAL_LOCAL_URL=ws://localhost:8765 AGENT_NAME=phone python3 phone_agent.py
```

Terminal 4 - send a prompt as if typed on either device:
```bash
cd central-command
python3 simulate_device_prompt.py laptop-ui "take a photo on phone"
python3 simulate_device_prompt.py phone-ui "run: whoami"
python3 simulate_device_prompt.py test-ui "get location on phone"
```

All three of the above were run and confirmed working: a prompt
originating from one simulated device correctly triggers an action on a
*different* device and the result comes back to the device that asked -
not just to Central Command's own console, which is what makes this a
real multi-device system rather than a single-terminal demo.

## Transport modes

`negotiate_transport()` tries, in order:

1. **Local network** - explicit `CENTRAL_LOCAL_URL`, or mDNS discovery if
   unset. Real and tested.
2. **Bluetooth** - for when there's no shared Wi-Fi network at all.
   **Not implemented yet** - `shared/transport.py`'s `try_bluetooth()` is
   a documented placeholder that always returns "unavailable," so the
   chain always falls through to remote relay. Real Bluetooth needs
   physical radios on both ends to test properly (a sandbox can't verify
   this the way it can verify the WebSocket paths). What a real
   implementation needs is written in that function's docstring: `bleak`
   for cross-platform BLE, or `pybluez` for classic RFCOMM, plus manual
   JSON message framing since raw sockets don't have WebSocket's built-in
   framing.
3. **Remote relay** - explicit `CENTRAL_REMOTE_URL` pointing at a Central
   Command instance on a VPS, for when devices aren't on the same
   network. Real and tested (same WebSocket code path as local network,
   just a different URL).

The negotiation *logic itself* (try in order, stop at first success, fail
loudly if everything fails) is fully tested in `shared/test_transport.py`
independent of whether Bluetooth hardware or a live network are present -
run it with `python3 shared/test_transport.py`.

Whichever transport wins, `agent.py` and `phone_agent.py` never see a
difference - they just get an object with `.send()`/`.recv()` either way.
Adding Bluetooth for real later means writing `try_bluetooth()` properly;
nothing else changes.

## Testing without your real devices

Everything above runs as ordinary local processes - no phone or laptop
was touched to test any of this. A few ways to keep testing safely as
this grows:

- **What we just did** - run Central Command + laptop agent + mock phone
  agent as separate processes on one machine. Proves message flow and
  multi-device dispatch without touching real hardware.
- **Docker Compose** - put each of the three in its own container for
  closer-to-real network isolation, still fully disposable.
- **Android emulator** - once the real RN app exists, an emulator can run
  it and test everything except real Bluetooth radio behavior.
- **A spare/throwaway device or cheap VM** - the first point where you'd
  want to test against something real, once you specifically need to
  verify actual camera/clipboard/Bluetooth behavior rather than the
  message-passing logic.

## Security notes (read before exposing this beyond localhost)

- `run_shell` executes **arbitrary commands** sent to the laptop agent.
  Fine for personal experimentation behind a token you control; replace
  with an allowlist before this touches anything you care about.
- `CENTRAL_TOKEN` is your only auth. Treat it like a password.
- Put TLS (`wss://`) in front of Central Command before using
  `CENTRAL_REMOTE_URL` over the public internet.

## Next steps

1. **LLM planner** - swap `RulePlanner` in `central_command.py` for one
   backed by the Anthropic API (expose the `ACTIONS` registry as tools),
   for real reasoning instead of keyword matching.
2. **LangGraph for chains** - once single actions work, wrap the planner
   in a LangGraph `StateGraph` so a multi-step ask ("photo from phone,
   save it on laptop") can feed one action's result into the next
   action's params.
3. **Real React Native phone agent** - replace `phone_agent.py`'s mocked
   actions with real ones (camera, location, clipboard, notifications)
   using RN's native modules, running in a foreground service so Android
   doesn't kill the connection. Same message contract, so Central Command
   doesn't change at all.
4. **Real Bluetooth transport** - implement `try_bluetooth()` for real
   once there's a real phone app to test it against.
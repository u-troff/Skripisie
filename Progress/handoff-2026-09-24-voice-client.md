# Handoff — wire the WonderEcho Pro into the voice pipeline (2026-09-24)

Written in a Cowork session after reading `Dashboard/brain/main.py`, `pipeline.py`, `dialogue_session.py`,
`tts.py`, `stt.py`, `mission.py` and `Dashboard/pi_client/turbopi_voice_client.py`. Hand this whole file to
Claude Code. Repo CLAUDE.md rules still apply (brain venv is Python 3.9 — `Optional[...]`, not `X | None`).

## Current state (verified)

- WonderEcho Pro works on the Pi: on-chip wake word "HELLO HIWONDER" emits `AA 55 03 00 FB` on its CH340
  serial port (115200 baud); audio is a separate USB audio interface ("USB PnP Audio Device").
- Mac side is already complete: `/ws/dialogue` accepts `{"type":"start"}` then `{"type":"audio_chunk","audio":<b64 WAV>}`,
  replies with `speak` events carrying Piper WAV audio (`pipeline._speak` → `tts.synthesize_speech`).
- `pi_client/turbopi_voice_client.py` already does VAD capture → base64 WAV → `/ws/dialogue` → plays `speak` audio.
- `pi_client/calibrate_mic.py` (new) measures noise vs speech RMS and prints a suggested `SILENCE_THRESHOLD`.
  Measured value on this unit: **SILENCE_THRESHOLD = 0.0109** (fill in after running it on the Pi).

## Tasks — all in `Dashboard/pi_client/turbopi_voice_client.py` unless stated

### A. Never send silence (bug — corrupts RQ2 data)

`record_until_silence` returns the full 15 s buffer even when `speaking_started` is False. Whisper then
hallucinates text ("Thank you.") that the dialogue treats as a real answer. Return an empty array when
speech never started; callers already skip `len(samples) == 0`.

### B. Wake-word gate + turn-taking

Current client listens continuously, so fan/motor/background noise becomes commands.

- Add `pyserial`; `wait_for_wake(port)` blocks (run via `asyncio.to_thread`) until `AA 55 03 00 FB` appears
  in a rolling buffer from the serial port at 115200.
- New CLI arg `--wake-port` (default `/dev/ttyUSB0`; recommend the `/dev/serial/by-id/...` path — must NOT
  collide with the port `ros_robot_controller` uses).
- Flow in `capture_loop`:
  1. If the brain's last `speak` had `phase` in `{"clarifying","awaiting_confirmation"}` → listen for the
     reply immediately (no wake word).
  2. Otherwise → wait for wake word, then queue `{"type":"start","language":...}` (new session per command).
  3. `await asyncio.sleep(0.5)` before recording (speaker tail / wake chime).
  4. Record; if empty → log "heard nothing" and go back to wake mode.
  5. Send the `audio_chunk`, then block on an `asyncio.Event` (`turn_done`) that `receive_loop` sets after
     it has *played* a `speak` event. Only then loop.
- Remove the unconditional `start` frame in `run_once` (capture loop now sends one per wake).
- Correct the module docstring: the CH340 wake word is detected on-chip and is fully local; only Hiwonder's
  ROS `large_models` package is cloud-bound. Using the serial wake flag does not conflict with RQ1.

### C. Mission hand-off (currently nothing happens after "yes")

The Pi's `/ws/dialogue` session is independent of the dashboard's, so no-one sends `begin` on `/ws/execution`.

- On `{"type":"execute","session_id":...}` → `asyncio.create_task(self.run_mission(session_id))`.
- `run_mission`: connect `ws://<mac>:<port>/ws/execution`, send `{"type":"begin","session_id":...}`, print
  events; play `speak` events that carry `audio` (see D); on `awaiting_revision` record one utterance
  (same VAD + audio_lock) and send `{"type":"revision_audio","audio":<b64 WAV>}`; exit on
  `mission_ended` / `aborted` / `error`.
- While a mission is running, the dialogue capture loop should stay in wake-word mode (it will naturally,
  since the last `speak` — "Confirmed. Going now." — has phase `executing`).

### D. Spoken output during missions — `Dashboard/brain/mission.py`

`mission._speak` (line ~33) builds `speak` events with no `audio`, unlike `pipeline._speak`. Add Piper audio
the same way (`base64(tts.synthesize_speech(text))`, soft-fail to text-only with a logged warning). It's
called from async code via `await emit(_speak(...))`; synthesis is ~sub-second, but prefer making it
non-blocking (`await asyncio.to_thread(...)`) if the change stays small. Keep the event shape unchanged —
`ui/src/types.ts` already types mission `speak` events.

### E. Threshold configurable without editing code

Add `--silence-threshold` CLI arg (default from env `SILENCE_THRESHOLD`, else 0.01) and pass it into
`record_until_silence`, replacing the module constant. Log the value in use at startup.

## Acceptance test (first run with `ROVER=sim` or `virtual` in `.env`, NOT `pi`)

1. Say "Hello Hiwonder", then a clear command → plan readback is spoken → say "yes" → client prints
   `mission_started` … `mission_ended`, and "Done." is spoken.
2. Wake + vague command ("go look for it") → clarifying question is spoken → answer WITHOUT the wake word
   → dialogue continues.
3. After a question, stay silent → client logs "heard nothing", returns to wake mode, brain receives nothing.
4. Talk normally without the wake word while idle → nothing is sent.
5. Only after 1–4 pass: switch `.env` to `ROVER=pi` with the rover on blocks for the first physical run.

## Out of scope

- Dashboard visibility of Pi-originated sessions (separate WebSockets; needs a broadcast later).
- Camera frame forwarding to `/ws/execution` (known gap, tracked separately).


to run:╰─ python3 ~/turbopi_voice_client.py --mac-host 172.20.10.2 --language en 
  --wake-port /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0  --silence-threshold 0.0109

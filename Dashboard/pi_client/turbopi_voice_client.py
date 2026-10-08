#!/usr/bin/env python3
"""Pi-side voice I/O for the TurboPi voice pipeline.

Runs on the Pi's HOST OS directly — NOT inside the `turbopi` Docker
container, NOT part of Dashboard/brain/venv. All models (STT, planner, VLM,
TTS) stay on the Mac; this script only captures mic audio, ships it to
brain/ over a plain WebSocket, and plays back whatever comes back. It does
not touch ROS2, rosbridge, or cmd_vel at all — that's rover_pi.py's job, a
completely separate connection.

Hardware: the WonderEcho Pro board enumerates as two independent USB
interfaces (confirmed via live SSH — arecord -l / dmesg, not assumed):
  - A CH340 USB-serial device — the on-chip (CI1302) wake-word detector.
    Saying "HELLO HIWONDER" makes it emit AA 55 03 00 FB at 115200 baud.
    This detection happens *on the board itself and is fully local* — no
    network, no cloud, no model on the Mac involved. Using it as a
    push-to-talk gate therefore does not conflict with RQ1's local-only
    premise; what RQ1 rules out is Hiwonder's ROS `large_models` package,
    which is the cloud-bound part (OpenAI/Aliyun ASR+TTS). We use only the
    wake flag, never the board's closed ~30-phrase command vocabulary — the
    actual command is captured as audio and transcribed by our own local
    Whisper on the Mac.
  - "USB PnP Audio Device" (JMTek) — a standard full-duplex USB Audio class
    device. This script talks to it directly via sounddevice/ALSA, the same
    way any other USB mic/speaker would work. No ROS2, no Docker involved.

Turn-taking: idle -> wait for wake word -> record one utterance -> send ->
wait until the reply has finished playing. If that reply was a clarifying
question or a confirmation prompt, the next utterance is captured
immediately without needing the wake word again; otherwise it returns to
wake-word mode.

Setup on the Pi (system Python, outside any venv/Docker):
    sudo apt install portaudio19-dev
    pip install sounddevice numpy websockets pyserial

Run, pointing at wherever brain/ is currently listening (the Mac's IP on
whatever network both devices share — confirmed working on the iPhone
hotspot; update if that changes):

    python3 turbopi_voice_client.py --mac-host 192.168.x.x

Tune the VAD for the room first with calibrate_mic.py, then pass the value
it suggests via --silence-threshold (or the SILENCE_THRESHOLD env var).

Ctrl+C to stop. Reconnects with backoff if the Mac-side server isn't up yet
or drops mid-session.
"""

import argparse
import asyncio
import base64
import io
import json
import os
import wave
from typing import Optional

import numpy as np
import sounddevice as sd
import websockets

# VAD constants from Dashboard/brain/audio_capture.py — proven values from
# that earlier local-mic prototype, reused rather than re-tuned. The silence
# threshold is the exception: it depends on the room and the rover's own fan
# noise, so it is a CLI/env knob (see calibrate_mic.py) rather than a
# constant.
# TARGET_SAMPLE_RATE is what stt.py/Piper expect over the wire, NOT what the
# hardware is asked to run at — the WonderEcho Pro's USB audio chip only
# opens a raw ALSA "hw:" stream at its own native rate (typically 44100 or
# 48000) and rejects anything else with a PortAudio "Invalid sample rate"
# error. The device's real rate is queried at startup and passed around as
# capture_rate; record_until_silence() runs at that rate, and the result is
# resampled to TARGET_SAMPLE_RATE before being sent. Playback works the same
# way in reverse.
TARGET_SAMPLE_RATE = 16000
DEFAULT_SILENCE_THRESHOLD = 0.01
SILENCE_DURATION = 1.0
CHUNK_DURATION = 0.1
MAX_UTTERANCE_S = 15.0

# Emitted by the WonderEcho Pro's CH340 interface when its on-chip wake word
# fires. Same frame Hiwonder's own xf_mic_asr_offline node looks for.
WAKE_FRAME = bytes.fromhex("aa550300fb")
WAKE_BAUD = 115200

# Phases where the brain is waiting on a direct answer, so the next
# utterance should be captured without making the user say the wake word
# again. Values match dialogue_session.Phase.
CONVERSATIONAL_PHASES = {"clarifying", "awaiting_confirmation"}

# Matches how Dashboard/brain/main.py's /pi/camera/snapshot endpoint talks
# to web_video_server from the Mac side — here we're already on the Pi, so
# it's just localhost, no proxy needed.
CAMERA_SNAPSHOT_URL = "http://localhost:8080/snapshot?topic=/image_raw"

RECONNECT_BACKOFF_S = [1, 2, 5, 10, 15]


def find_device(name_substr: str) -> int:
    """Resolve the WonderEcho Pro's audio interface by name, not index —
    ALSA card numbers and PortAudio device indices don't reliably match, and
    silently falling back to whatever the Pi's default device is would be a
    quiet failure, not a loud one."""
    devices = sd.query_devices()
    for idx, dev in enumerate(devices):
        if name_substr.lower() in dev["name"].lower():
            return idx
    names = "\n".join(f"  [{i}] {d['name']}" for i, d in enumerate(devices))
    raise RuntimeError(
        f"no audio device matching {name_substr!r} found. Available devices:\n{names}"
    )


def wait_for_wake(serial_port) -> None:
    """Block until the board reports its wake word. Runs in a worker thread.

    Reads into a small rolling buffer rather than matching frame-by-frame:
    the 5-byte frame can be split across reads, and other status bytes
    (sleep, command codes we ignore) share the same stream.
    """
    buffer = bytearray()
    serial_port.reset_input_buffer()
    while True:
        chunk = serial_port.read(64)
        if not chunk:
            continue
        buffer.extend(chunk)
        if WAKE_FRAME in buffer:
            buffer.clear()
            return
        # Keep only enough tail to catch a frame split across two reads.
        if len(buffer) > 256:
            del buffer[:-len(WAKE_FRAME)]


def record_until_silence(device: int, capture_rate: int,
                         silence_threshold: float) -> np.ndarray:
    """Blocking mic capture — same algorithm as audio_capture.py's function,
    parameterised on device and rate because this must run at whatever
    native rate the hardware actually supports, not TARGET_SAMPLE_RATE.

    Returns an EMPTY array if speech never started. Returning the buffer
    anyway would ship ~15s of room tone to Whisper, which reliably
    hallucinates filler ("Thank you.") that the dialogue would then treat as
    a real answer — that corrupts both the conversation and RQ2's counts.
    """
    chunk_samples = int(CHUNK_DURATION * capture_rate)
    silence_chunks_needed = int(SILENCE_DURATION / CHUNK_DURATION)
    buffer, silent_chunks, speaking_started = [], 0, False
    with sd.InputStream(samplerate=capture_rate, channels=1, dtype="float32",
                         device=device) as stream:
        total_samples, max_samples = 0, int(MAX_UTTERANCE_S * capture_rate)
        while total_samples < max_samples:
            chunk, _ = stream.read(chunk_samples)
            chunk = chunk.flatten()
            buffer.append(chunk)
            total_samples += len(chunk)

            rms = np.sqrt(np.mean(chunk ** 2))
            if rms > silence_threshold:
                speaking_started = True
                silent_chunks = 0
            elif speaking_started:
                silent_chunks += 1
                if silent_chunks >= silence_chunks_needed:
                    break

    if not speaking_started:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(buffer) if buffer else np.zeros(0, dtype=np.float32)


def resample(samples: np.ndarray, orig_rate: int, target_rate: int) -> np.ndarray:
    """Linear-interpolation resample — good enough for speech, and avoids
    pulling in scipy just for this one conversion. Used both directions:
    mic capture (device rate -> 16kHz for STT) and playback (Piper's 22050Hz
    -> device rate)."""
    if orig_rate == target_rate or len(samples) == 0:
        return samples
    duration = len(samples) / orig_rate
    target_len = max(1, int(round(duration * target_rate)))
    orig_idx = np.arange(len(samples))
    target_idx = np.linspace(0, len(samples) - 1, num=target_len)
    return np.interp(target_idx, orig_idx, samples).astype(np.float32)


def pcm_to_wav_bytes(samples: np.ndarray) -> bytes:
    """Wrap raw float32 PCM in a WAV container before it goes over the wire.

    brain/'s stt.transcribe_audio() branches on isinstance(audio, np.ndarray)
    vs. bytes; a WebSocket JSON frame can only carry the latter (base64), and
    that bytes branch assumes an FFmpeg-decodable container — exactly what
    the browser's webm recordings already are. Headerless PCM would fail to
    decode there, so the container gets added here instead of touching
    stt.py's existing contract.
    """
    int16 = np.clip(samples * 32767, -32768, 32767).astype(np.int16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(TARGET_SAMPLE_RATE)
        wav_file.writeframes(int16.tobytes())
    return buffer.getvalue()


def grab_camera_snapshot() -> Optional[bytes]:
    """Best-effort still frame for the ambiguity check — optional context,
    never worth failing a voice turn over."""
    try:
        import urllib.request
        with urllib.request.urlopen(CAMERA_SNAPSHOT_URL, timeout=2) as resp:
            return resp.read()
    except Exception:
        return None


class VoiceClient:
    def __init__(self, mac_host: str, mac_port: int, language: str, device: int,
                 capture_rate: int, serial_port, silence_threshold: float):
        self.mac_host = mac_host
        self.mac_port = mac_port
        self.dialogue_uri = f"ws://{mac_host}:{mac_port}/ws/dialogue"
        self.execution_uri = f"ws://{mac_host}:{mac_port}/ws/execution"
        self.language = language
        self.device = device
        self.capture_rate = capture_rate
        self.serial_port = serial_port
        self.silence_threshold = silence_threshold
        self.session_id: Optional[str] = None
        # Phase carried by the last spoken reply — decides whether the next
        # utterance needs the wake word or is a direct answer.
        self.last_phase: Optional[str] = None
        # Set once a reply has finished playing, so the capture loop knows
        # the turn is over and it may listen again.
        self.turn_done = asyncio.Event()
        # Capture and playback share one physical device; without this lock
        # a spoken reply playing through the speaker would bleed into the
        # mic and could get picked up by the VAD loop as a new "utterance"
        # of the rover talking to itself. Serialising the two avoids that
        # entirely rather than trying to do echo cancellation.
        self.audio_lock = asyncio.Lock()

    # -- audio ---------------------------------------------------------------
    async def play_audio(self, audio_bytes: bytes) -> None:
        def _play():
            buf = io.BytesIO(audio_bytes)
            with wave.open(buf, "rb") as wav_file:
                data = wav_file.readframes(wav_file.getnframes())
                samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                # Piper's WAV comes back at its own rate (22050Hz for
                # en_US-lessac-medium) — same hardware restriction as
                # capture applies here, so resample to what the device
                # actually accepts before handing it to PortAudio.
                samples = resample(samples, wav_file.getframerate(), self.capture_rate)
                sd.play(samples, samplerate=self.capture_rate, device=self.device)
                sd.wait()
        async with self.audio_lock:
            # Blocking sounddevice calls run in a worker thread, not the
            # event loop, so receive_loop can keep reading frames while
            # playback is in progress.
            await asyncio.to_thread(_play)

    async def record_utterance(self) -> np.ndarray:
        async with self.audio_lock:
            samples = await asyncio.to_thread(
                record_until_silence, self.device, self.capture_rate,
                self.silence_threshold,
            )
        if len(samples) == 0:
            return samples
        return resample(samples, self.capture_rate, TARGET_SAMPLE_RATE)

    def audio_frame(self, kind: str, samples: np.ndarray) -> dict:
        return {"type": kind,
                "audio": base64.b64encode(pcm_to_wav_bytes(samples)).decode("ascii")}

    # -- dialogue ------------------------------------------------------------
    async def capture_loop(self, send_queue: "asyncio.Queue[dict]") -> None:
        while True:
            if self.last_phase in CONVERSATIONAL_PHASES:
                print("[voice] listening for your answer…")
            else:
                print("[voice] idle — say the wake word to start")
                await asyncio.to_thread(wait_for_wake, self.serial_port)
                print("[voice] wake word heard")
                self.session_id = None
                await send_queue.put({"type": "start", "language": self.language})

            # Let the wake chime / speaker tail die away before opening the
            # mic, or the VAD triggers on the board's own acknowledgement.
            await asyncio.sleep(0.5)

            samples = await self.record_utterance()
            if len(samples) == 0:
                print("[voice] heard nothing — back to wake mode")
                self.last_phase = None
                continue

            self.turn_done.clear()
            frame = self.audio_frame("audio_chunk", samples)
            snapshot = grab_camera_snapshot()
            if snapshot:
                frame["image"] = base64.b64encode(snapshot).decode("ascii")
            await send_queue.put(frame)

            # Block until the reply has actually been spoken, so the next
            # recording can't start over the top of it.
            await self.turn_done.wait()

    async def receive_loop(self, ws) -> None:
        async for raw in ws:
            event = json.loads(raw)
            kind = event.get("type")

            if kind == "session":
                self.session_id = event.get("session_id")
                print(f"[voice] session {self.session_id} ({event.get('phase')})")

            elif kind == "speak":
                print(f"[voice] speak: {event.get('text')!r}")
                self.last_phase = event.get("phase")
                audio_b64 = event.get("audio")
                if audio_b64:
                    await self.play_audio(base64.b64decode(audio_b64))
                else:
                    print("[voice] (no audio — TTS likely failed server-side)")
                self.turn_done.set()

            elif kind == "execute":
                # The dialogue is done and confirmed; nobody else is going to
                # open /ws/execution for a Pi-originated session, so do it.
                print(f"[voice] confirmed — starting mission {event.get('session_id')}")
                asyncio.create_task(self.run_mission(event.get("session_id")))

            elif kind in ("error", "cancelled"):
                print(f"[voice] {kind}: {event.get('message') or event.get('reason')}")
                # Release the capture loop, or it waits on a turn that will
                # never be spoken.
                self.last_phase = None
                self.turn_done.set()

            else:
                print(f"[voice] {kind}")

    async def send_loop(self, ws, send_queue: "asyncio.Queue[dict]") -> None:
        while True:
            frame = await send_queue.get()
            await ws.send(json.dumps(frame))

    # -- mission -------------------------------------------------------------
    async def run_mission(self, session_id: Optional[str]) -> None:
        """Drive one mission on its own /ws/execution connection.

        The dialogue socket and the execution socket are separate endpoints
        with separate per-connection state on the brain side, so the mission
        only starts if someone sends `begin` — which, for a Pi-originated
        session, is us.
        """
        if not session_id:
            print("[voice] execute event had no session_id, ignoring")
            return
        print(f"[voice] connecting to {self.execution_uri}")
        try:
            async with websockets.connect(self.execution_uri, max_size=None) as ws:
                await ws.send(json.dumps({"type": "begin", "session_id": session_id}))
                # mission.py emits awaiting_revision and then immediately
                # speaks the question, so record only once the question has
                # finished playing rather than the moment the event lands.
                revision_pending = False

                async for raw in ws:
                    event = json.loads(raw)
                    kind = event.get("type")

                    if kind == "speak":
                        print(f"[voice] speak: {event.get('text')!r}")
                        audio_b64 = event.get("audio")
                        if audio_b64:
                            await self.play_audio(base64.b64decode(audio_b64))
                        if revision_pending:
                            revision_pending = False
                            await self._answer_revision(ws)

                    elif kind in ("awaiting_revision", "awaiting_guidance"):
                        # Same reply path for both: the brain routes revision_audio
                        # to guidance while its phase is awaiting_guidance.
                        revision_pending = True
                        print("[voice] mission paused — waiting on your answer")

                    elif kind in ("mission_ended", "aborted", "error"):
                        print(f"[voice] {kind}: {event.get('message') or ''}".rstrip(": "))
                        return

                    elif kind in ("step_started", "step_done", "mission_started",
                                  "observation", "revision", "plan_revised", "halted"):
                        print(f"[voice] {kind}")

                    else:
                        print(f"[voice] {kind}")
        except Exception as exc:
            print(f"[voice] mission connection failed: {exc}")
        finally:
            # Whatever happened, the dialogue side should go back to
            # requiring a wake word for the next command.
            self.last_phase = None

    async def _answer_revision(self, ws) -> None:
        print("[voice] listening for your answer…")
        await asyncio.sleep(0.5)
        samples = await self.record_utterance()
        if len(samples) == 0:
            print("[voice] heard nothing — mission stays paused")
            return
        await ws.send(json.dumps(self.audio_frame("revision_audio", samples)))

    # -- connection ----------------------------------------------------------
    async def run_once(self) -> None:
        print(f"[voice] connecting to {self.dialogue_uri}")
        # max_size=None: "speak" frames carry the whole TTS reply as base64 WAV,
        # and a long one (e.g. reading out a plan) exceeds the library's 1 MiB
        # default -> close code 1009 "message too big".
        async with websockets.connect(self.dialogue_uri, max_size=None) as ws:
            # No unconditional "start" here: capture_loop sends one per wake
            # word, so each spoken command gets its own session.
            send_queue: "asyncio.Queue[dict]" = asyncio.Queue()
            await asyncio.gather(
                self.capture_loop(send_queue),
                self.send_loop(ws, send_queue),
                self.receive_loop(ws),
            )

    async def run_forever(self) -> None:
        attempt = 0
        while True:
            try:
                await self.run_once()
            except (websockets.ConnectionClosed, OSError) as exc:
                delay = RECONNECT_BACKOFF_S[min(attempt, len(RECONNECT_BACKOFF_S) - 1)]
                print(f"[voice] connection lost ({exc}); retrying in {delay}s")
                await asyncio.sleep(delay)
                attempt += 1
                self.last_phase = None
            else:
                break  # run_once only returns cleanly if the tasks somehow finish


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--mac-host", required=True,
                         help="IP of the Mac currently running brain/ (e.g. its hotspot address)")
    parser.add_argument("--mac-port", type=int, default=8000)
    parser.add_argument("--language", default="en")
    parser.add_argument("--device-name", default="USB PnP",
                         help="substring matching the WonderEcho Pro's audio device name")
    parser.add_argument("--wake-port", default="/dev/ttyUSB0",
                         help="CH340 serial port carrying the wake-word frame. Prefer a "
                              "stable /dev/serial/by-id/... path, and make sure it is NOT "
                              "the port ros_robot_controller uses.")
    parser.add_argument("--silence-threshold", type=float,
                         default=float(os.environ.get("SILENCE_THRESHOLD",
                                                      DEFAULT_SILENCE_THRESHOLD)),
                         help="VAD level; run calibrate_mic.py to find the right value "
                              "for this room and fan state")
    args = parser.parse_args()

    try:
        import serial
    except ImportError:
        raise SystemExit("pyserial is required for the wake word: pip install pyserial")

    device = find_device(args.device_name)
    device_info = sd.query_devices(device)
    # Ask the hardware what it actually supports rather than assuming
    # 16000Hz — the "Invalid sample rate" PortAudio error this replaces
    # comes from exactly that assumption being wrong on this chip.
    capture_rate = int(device_info["default_samplerate"])
    print(f"[voice] using audio device [{device}] {device_info['name']} @ {capture_rate}Hz "
          f"(resampling to {TARGET_SAMPLE_RATE}Hz for STT/TTS)")
    print(f"[voice] silence threshold {args.silence_threshold:.4f}")

    try:
        serial_port = serial.Serial(args.wake_port, WAKE_BAUD, timeout=0.2)
    except serial.SerialException as exc:
        raise SystemExit(f"could not open wake-word port {args.wake_port!r}: {exc}")
    print(f"[voice] wake word on {args.wake_port} @ {WAKE_BAUD} baud")

    client = VoiceClient(args.mac_host, args.mac_port, args.language, device,
                         capture_rate, serial_port, args.silence_threshold)
    try:
        asyncio.run(client.run_forever())
    except KeyboardInterrupt:
        print("\n[voice] stopped")
    finally:
        serial_port.close()


if __name__ == "__main__":
    main()

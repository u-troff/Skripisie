#!/usr/bin/env python3
"""Measure the WonderEcho Pro's noise floor vs speech level and suggest
SILENCE_THRESHOLD for turbopi_voice_client.py.

Run on the Pi HOST (not in Docker), with the rover as it will be during a
real run (fan state, ROS bringup running, same room):

    python3 calibrate_mic.py            # device name defaults to "USB PnP"
    python3 calibrate_mic.py "WonderEcho"
"""
import sys

import numpy as np
import sounddevice as sd

NAME = sys.argv[1] if len(sys.argv) > 1 else "USB PnP"
CHUNK_S = 0.1  # same chunk size the client's VAD uses


def find_input(name: str) -> int:
    for i, d in enumerate(sd.query_devices()):
        if name.lower() in d["name"].lower() and d["max_input_channels"] > 0:
            return i
    listing = "\n".join(f"  [{i}] {d['name']}" for i, d in enumerate(sd.query_devices()))
    raise SystemExit(f"No input device matching {name!r}. Available:\n{listing}")


DEV = find_input(NAME)
RATE = int(sd.query_devices(DEV)["default_samplerate"])
print(f"Using [{DEV}] {sd.query_devices(DEV)['name']} @ {RATE} Hz")


def measure(prompt: str, seconds: float) -> np.ndarray:
    input(f"\n{prompt}\nPress Enter to start ({seconds:.0f} s)...")
    n = int(CHUNK_S * RATE)
    levels = []
    with sd.InputStream(samplerate=RATE, channels=1, dtype="float32", device=DEV) as stream:
        for _ in range(int(seconds / CHUNK_S)):
            chunk, _ = stream.read(n)
            rms = float(np.sqrt(np.mean(chunk ** 2)))
            levels.append(rms)
            print(f"  rms {rms:.4f} " + "#" * min(60, int(rms * 2000)), flush=True)
    return np.array(levels)


noise = measure("1/2  ROOM NOISE - stay silent.", 5)
speech = measure("2/2  SPEECH - from ~1 m away, say a normal command, e.g.\n"
                 "     'drive forward, then turn left and look for the red box'.", 5)

noise_hi = float(np.percentile(noise, 95))      # loudest normal background
speech_lvl = float(np.percentile(speech, 75))   # 75th pct skips the gaps between words
suggested = float(np.sqrt(noise_hi * speech_lvl))  # geometric midpoint between the two

print("\n----- result -----")
print(f"noise floor (95th pct): {noise_hi:.4f}")
print(f"speech level (75th pct): {speech_lvl:.4f}")
print(f"SNR: {speech_lvl / max(noise_hi, 1e-6):.1f}x")
print(f"current SILENCE_THRESHOLD: 0.0100")
print(f"SUGGESTED SILENCE_THRESHOLD: {suggested:.4f}")
if speech_lvl < 3 * noise_hi:
    print("\nWARNING: speech is less than 3x the noise floor - VAD will be unreliable.\n"
          "Turn the fan off (pinctrl FAN_PWM op dh), move closer, or cut background noise,\n"
          "then run this again.")

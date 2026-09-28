# Reference: The Official Raspberry Pi Beginner's Guide (5th Ed.)

Source: `BeginnersGuide-5thEd-Eng_v3.pdf` (Raspberry Pi Press, 290 pages), supplied by Utroff via
`pi_transfers/` on 2026-09-14. This is the **general** Raspberry Pi Foundation guide — not
TurboPi/Hiwonder-specific. It sits alongside `claude/turbopi-connection-guide.md` (Hiwonder/TurboPi
facts) as background for anything that's plain Raspberry Pi OS / Pi 5 behaviour rather than
Hiwonder's own layer on top. Read in full via `pdftotext -layout`; key chapters excerpted below.

Full chapter list (for context, not all excerpted): Ch1 Get to know your Raspberry Pi (models) ·
Ch2 Getting started (hardware setup) · Ch3 Using your Raspberry Pi (desktop tour) · Ch4 Scratch 3 ·
Ch5 Python (turtle/text adventure intro) · **Ch6 Physical computing (GPIO) w/ Scratch+Python** ·
Ch7 Sense HAT · **Ch8 Camera Modules** · Ch9 Pico/Pico W · App A Installing an OS · App B
Installing/uninstalling software · **App C The command-line interface** · App D Further reading ·
**App E Raspberry Pi Configuration Tool (raspi-config)** · App F Raspberry Pi specifications.

---

## 1. Camera testing — `libcamera-still` / `libcamera-vid` (Ch 8)

This is the plain-OS way to sanity-check the camera hardware *without* going through any Hiwonder
demo script — good for isolating "is the sensor/ribbon cable/driver okay" from "is Hiwonder's
color-detection code okay." Pi 5 uses the newer `libcamera` stack (not the legacy `raspistill`).

Note: Camera Module ribbon cable orientation on **Pi 5 specifically** — GPIO header to the right,
HDMI to the left, silver/gold contacts to the right, blue plastic to the left, into the lower
`CAM/DISP 0` port. (Pi 4 and earlier are mirrored the other way.)

```bash
# Still photo — 5s live preview window, then saves to CWD (home folder by default)
libcamera-still -o test.jpg

# If the preview is upside-down / sideways, rotate (cable normally exits the bottom edge)
libcamera-still --rotation 180 -o test.jpg      # cable exits top
libcamera-still --rotation 90  -o test.jpg      # cable exits right
libcamera-still --rotation 270 -o test.jpg      # cable exits left

# Video, 10 seconds, raw bitstream (.h264 has no timing info by itself)
libcamera-vid -t 10000 -o test.h264

# Video WITH timestamps (needed for smooth/correct playback elsewhere)
libcamera-vid -t 10000 --save-pts timestamps.txt -o test-time.h264
mkvmerge --timecodes 0:timestamps.txt test-time.h264 -o test-time.mkv

# Time-lapse: N stills at an interval, numbered sequentially
mkdir timelapse && cd timelapse
libcamera-still --width 1920 --height 1080 -t 100000 --timelapse 10000 -o %05d.jpg
ffmpeg -r 0.5 -i %05d.jpg -r 15 animation.mp4   # stitch into a video afterwards
```

Useful flags on both `libcamera-still` and `libcamera-vid` (append to either command):

| Flag | Purpose |
|---|---|
| `--autofocus-mode continuous\|manual\|auto` | Camera Module 3 only; default continuous |
| `--autofocus-range normal\|macro\|full` | widen/narrow AF search range |
| `--lens-position N` | manual focus in dioptres (0.0 = infinity, 2 = 0.5 m) — needs `--autofocus-mode manual` |
| `--width` / `--height` | resolution |
| `--rotation 0\|90\|180\|270`, `--hflip`, `--vflip` | orientation |
| `--sharpness` / `--contrast` / `--brightness` / `--saturation` | image tuning, default 1.0 (or 0.0 for brightness) |
| `--ev -10..10` | exposure compensation |
| `--awb auto\|incandescent\|tungsten\|fluorescent\|indoor\|daylight\|cloudy` | white balance |

`libcamera-still`-only: `-q 0-100` (JPEG quality, default 93), `--datetime`/`--timestamp`
(auto-named output instead of `-o`), `-k` (capture on ENTER keypress instead of a timer — set
`-t 0`).

**Practical takeaway for the Skripsie:** if a Hiwonder demo script's camera feed looks wrong
(black frame, wrong colors, crash), running a bare `libcamera-still -o test.jpg` first isolates
whether it's a hardware/driver problem (would fail here too) versus a problem in Hiwonder's
`Camera.py`/OpenCV wrapper (would work here, fail there).

---

## 2. Command-line interface basics (App C)

Prompt anatomy: `username@raspberrypi:~ $` -> `~` = current working directory (home dir shorthand),
trailing `$` = unprivileged user (needs `sudo` to elevate).

```bash
cd Desktop        # relative path, case-sensitive (capital D matters)
cd ..              # up one level (parent directory)
cd ~ / cd          # back to home directory, from anywhere
cd /home/<user>    # absolute path to home directory

touch Test         # create empty file (or bump its mtime if it exists)
cp Test Test2       # copy
mv Test Test2       # move / rename
rm Test2            # delete — NOT sent to Wastebasket, unlike GUI file manager delete. Careful.

ls                  # list directory contents
ls -larth           # long format, all (incl. hidden), reverse order, sort by time, human sizes

raspi-config         # errors — needs root
sudo raspi-config    # correct way to run it

exit                 # end the terminal/CLI session
```

**TTYs**: `CTRL+ALT+F2` switches to a separate login prompt (tty2) independent of the desktop —
useful if the desktop/GUI hangs. `CTRL+ALT+F7` gets back to the desktop. Always `exit` a TTY
session before switching away (anyone at the keyboard can otherwise resume your login).

---

## 3. GPIO header basics (Ch 6)

40-pin header, top edge of the board. Pin categories: `3V3` / `5V` (always-on power rails),
`GND` (ground/return), `GPIO <2-27>` (general-purpose, programmable), `ID EEPROM` (reserved for
HAT auto-config, don't use directly).

**Warning that matters for hardware debugging:** never bridge two pins together unless a project
explicitly says to — that's a short circuit and can permanently kill the Pi.

Common physical-computing parts: breadboard, jumper wires (M2F to go from breadboard to GPIO
header, F2F to link components directly, M2M for breadboard-internal links), momentary switch,
LED (avoid ones rated 5V/12V — GPIO is 3.3V logic), resistor (~330 ohm typical for protecting an
LED), active piezo buzzer (get "active," not "passive" — simpler to drive). Motors need a
separate motor driver/control board — can't go straight to GPIO. This matches why TurboPi's
motors go through the HiwonderSDK/STM32 expansion board rather than bare GPIO pins.

---

## 4. `raspi-config` — Interfaces tab (App E)

This is the tool (GUI: Preferences -> Raspberry Pi Configuration; CLI: `sudo raspi-config`) that
turns hardware buses on/off at the OS level. Relevant entries if a Pi-side script can't talk to a
sensor/module at all (as opposed to a code bug):

| Interface | What it's for |
|---|---|
| SSH | remote CLI access from another machine — already in use for this project |
| VNC | remote desktop viewing — separate from Pi Connect's screen share, an older/local-network alternative |
| SPI | Serial Peripheral Interface — some GPIO add-ons need this on |
| **I2C** | Inter-Integrated Circuit — **this is what `HiwonderSDK/Board.py` talks to the STM32 motor-control board over**; if motor commands silently do nothing, checking this is enabled is a first troubleshooting step |
| Serial Port / Serial Console | GPIO serial — Serial Console is a CLI over the serial pins, separate from Serial Port itself |
| 1-Wire | for certain temperature/ID sensors |
| Remote GPIO | lets another computer on the network drive this Pi's GPIO pins via the `gpiozero` library over the network — not the same as SSH |

Other tabs of note: **System -> Boot** (Desktop vs CLI boot target — relevant for a headless
build that doesn't need to load the desktop at all), **Display -> Headless Resolution** (virtual
resolution used when no monitor is attached — matters for anything that needs a virtual display
size, e.g. some camera/X11 tooling), **Performance -> Overlay File System** (locks the filesystem
to a RAM-backed overlay so changes don't persist across reboot — worth knowing exists, but
leave off; it would silently discard any script edits made while it's on).

---

## 5. Installing/reflashing the OS (App A)

Only relevant if the Pi's SD card ever needs a clean reinstall (not expected mid-project, but
worth having if something goes catastrophically wrong).

- Tool: **Raspberry Pi Imager** (rptl.io/imager), macOS/Windows/Linux.
- Network-boot install (no separate computer needed) is **not supported on Pi 5** as of this
  guide's printing — Ethernet is required either way, Wi-Fi install isn't supported.
- Imager flow: Choose Device -> Choose OS (pick **64-bit** for Pi 5) -> Choose Storage -> gear icon
  for OS customisation (set username/password/Wi-Fi/SSH-enabled *before* first boot — this is the
  headless-setup path, avoids ever needing a monitor+keyboard) -> write & verify.
- Never remove the card or power off mid-write — restart the flash from scratch if interrupted.

---

## 6. Pi 5 hardware specs (App F), for quick reference

- SoC: Broadcom BCM2712, 4x Cortex-A76 @ 2.4GHz, VideoCore VII GPU @ 800MHz
- RAM: 4GB or 8GB LPDDR4X @ 4267MHz (shared CPU/GPU)
- Storage: microSD up to 512GB; PCIe 3.0 x1 lane available via HAT for NVMe SSD / ML accelerators
- Networking: Gigabit Ethernet, 802.11ac dual-band Wi-Fi, Bluetooth 5.0 + BLE
- USB: 2x USB 2.0, 2x USB 3.0
- Power: 5V via USB-C (**power only on Pi 5** — video needs micro-HDMI, already noted in the
  TurboPi connection guide)

---

## Where this fits vs. the TurboPi connection guide

`claude/turbopi-connection-guide.md` (this project) = Hiwonder-specific facts (AP/STA Wi-Fi
config, WonderPi app, Pi Connect setup, real `wifi_conf.py` contents, the folder layout of
Hiwonder's own demo code). This document = generic Raspberry Pi OS / Pi 5 facts that Hiwonder's
docs assume you already know. When debugging something Pi-side, check here first for "is this
just how Raspberry Pi OS works" before assuming it's a TurboPi-specific quirk.

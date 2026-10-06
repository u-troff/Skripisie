# Test plan B — status lights (RGB)

Spec §B. Files: `rover.py` (no-op default + `rgb_enabled` wiring), `rover_pi.py` (publisher + patterns), `mission.py` (`_light`), `mission_session.py` (`light_states`), `main.py` (dialogue lights), `report.py` (`light_states`).
Kill switch: `RGB_ENABLED=0`.

## B0. Offline (no rover) — 5 min

☐ With `ROVER=virtual` (or `sim`): start the backend and run one virtual mission (or `tools\virtual_sweep.py --conditions local_e4b --depth-max 3 --repeats 1`). Mission completes exactly as before.
☐ Open the run log: `light_states` is present and lists `executing` then `arrived` (the log works even with no lights, because `_light` records before it calls the rover).

## B1. Hardware bring-up (ask Utroff first; wheels off the ground)

Unknown until tested on the unit: **LED indices.** The code sends indices `1,2` to `ros_robot_controller/set_rgb` and `0,1` to `sonar_controller/set_rgb` (`RGB_BOARD_INDICES`, `RGB_SONAR_INDICES` in `.env`). `line_follow_corner.py` uses `0,1` on the sonar topic; the board indices are from memory of the stock code and **must be confirmed**.
☐ Start the Pi container, set `ROVER=pi`, `RGB_ENABLED=1`, start the backend.
☐ From a Python shell in `Dashboard/brain/`:

```
from rover import get_rover
r = get_rover()
for s in ["listening","thinking","confirm","executing","searching","awaiting_guidance","arrived","blocked"]:
    print(s); r.status_light(s); input("Enter for next ")
```

For each state tick what you **see** (both the board LEDs and the sonar ring):

| State             | Expected                          | Board LEDs ☐ | Sonar ring ☐ |
| ----------------- | --------------------------------- | ------------- | ------------- |
| listening         | blue steady                       | yes           | yes           |
| thinking          | red ↔ green, 0.5 s each          | yes           | yes           |
| confirm           | amber steady                      | yes           | yes           |
| executing         | green steady                      | yes           | yes           |
| searching         | cyan steady                       | yes           | yes           |
| awaiting_guidance | magenta, 1 s on / 1 s off         | yes           | yes           |
| arrived           | 3 green flashes then steady green | yes           | yes           |
| blocked           | red steady                        | yes           | yes           |

If a ring or the board does not light: adjust the indices in `.env` (try `0,1` / `1,2` / `0,1,2`). If colours look wrong (e.g. amber looks yellow), adjust the tuples in `_LIGHTS` in `rover_pi.py`; the spec says to tune on the real unit.
☐ Switching state mid-pattern: call `thinking`, then `executing` after 1 s. The blinking must stop within ~0.1 s and show steady green (no stray red flash).

## B2. In a mission (free-roam, ask first)

☐ Say a command with the dashboard. Observe the sequence: `listening` (session start) → `thinking` (STT/ambiguity/plan/verify) → `confirm` (plan read back) → `thinking` (while your "yes" is processed) → `executing` (the `execute` event) → `searching` (during the gimbal sweep) → `executing` (after lock, during hops) → `arrived`.
☐ Force a block (remove the target): light goes `searching` → `blocked` (or `awaiting_guidance` once D is applied).
☐ The run log `light_states` matches what you saw, with sensible `t_rel_s`.

## B3. Failure isolation

☐ `RGB_ENABLED=0`, run a mission: no errors, `light_states` still logged, nothing lights.
☐ `RGB_ENABLED=1` but stop the rosbridge topic side (e.g. unplug network for a second mid-mission, or point `RGB_BOARD_INDICES` at nonsense): mission continues; `brain.log` has a warning `status light failed`, no crash, no added latency to hops (check `hop_moved_s` against a baseline run).
☐ Dialogue path with the rover offline (`ROVER_PI_HOST` unreachable): the dialogue still works; the first light attempt logs one warning and later ones are skipped silently (`_light_failed` latch), so there is no 10 s connect stall per utterance.

## Answer to open question G1 (verify, then record)

`main.py`'s `/ws/dialogue` does not hold a rover, but `rover.get_rover()` is a process-wide singleton (`rover._cache`), so calling it from the dialogue path returns the **same** controller the mission uses. No second rosbridge connection is opened. ☐ Confirm in `brain.log`: exactly one `[pi] connecting to rosbridge` line per backend start.

## Results to record

| Item                              | Value              |
| --------------------------------- | ------------------ |
| Board indices that worked         | worked as intended |
| Sonar indices that worked         | worked as intende  |
| Colour tweaks made                | none               |
| Any state not visible             | nope               |
| Light-call latency impact on hops |                    |

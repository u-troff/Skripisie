# Skripsie status & plan — snapshot 2026-09-16 (updated 2026-09-20: source-code review + first `ROVER=pi` implementation)

Grounded in a direct read of the repo at `~/Desktop/Skripsie` this session, plus the signed PE448 2026 module framework. Full interactive version: published as the "Skripsie Mission Log" artifact (claude.ai). This file is a mirror of the claude.ai Project doc of the same name, kept in the repo so a local Claude Code session has it without needing the Project.

## Deadlines (PE448 2026 framework, Semester 2)
- **30 Oct 2026, 23:59** — project report hand-in (STEMLearn). Penalty 5%/half-day late.
- **6–13 Nov 2026** — oral presentations & internal examination.
- **18 Nov 2026** — project open day, poster + external moderator interviews (mandatory).

## What's actually built (corrects earlier, stale notes)
- Both architecture specs are **fully implemented**, not "awaiting implementation":
  - Swappable planner/VLM backend — `brain/providers/` (base, factory, ollama_provider, cloud_provider).
  - Multi-turn clarifying dialogue — `dialogue_session.py`, state machine CLARIFYING→PLANNING→VERIFYING→AWAITING_CONFIRMATION→EXECUTING→REPORTING over `/ws/dialogue`.
  - Voice confirmation gate — `confirmation.py`, two-stage keyword+model, EN/AF phrases, fails safe.
- Beyond either spec: a full mid-mission perception/revision loop (`mission.py`, `revision.py`) with a hand-coded (non-model) NO_CHANGE/REROUTE/MATERIAL/BLOCKED classifier. Exercised only in `sim`/`virtual` modes so far.
- A second, newer spec (`Progress/spec-virtual-rover-and-room-mapping.md`, drafted 2026-09-11) added a `VirtualRover` + hand-authored `RoomMap` (`rover.py`, `room_map.py`) — 2D pose integration, deliberate drift modelling, hallucination logging for RQ2, JSONL trace logs. Implemented and working (`logs/virtual_run_20260913-223346.jsonl`). Component B of that spec (camera calibration + floor-plane homography) is explicitly optional and **not built**.
- Frontend is **Vite + React + TypeScript + zustand**. Three panels (OneShot/Dialogue/Execution), a recorder hook, TTS via browser `SpeechSynthesis` explicitly coded as a stand-in for Piper.

## Bench sanity — hardware bring-up (2026-09-20 update)

TurboPi is the Hiwonder **Advanced kit**: everything runs inside a Docker container (`turbopi`) on ROS2 Humble, not the raw `HiwonderSDK.Board` SDK. Confirmed this session:

- **Root cause of the initial bring-up failures found and fixed**: `start_node.sh` (the systemd-triggered boot script) `docker exec`s straight into `bringup.launch.py` without first restarting the container, so any processes left running from a prior session collide with the new launch on ports 8080 (`web_video_server`)/9090 (`rosbridge_websocket`) and on the serial device (`ros_robot_controller`). Symptom included what looked like a distinct `usb_cam_node_exe` crash (`terminate called after throwing an instance of 'char*'`) — that turned out to be the *same* stale-process contention on `/dev/video0`, not a separate camera-driver bug.
- **Fix, confirmed working**: `docker restart turbopi && sleep 8 && docker exec -u ubuntu -w /home/ubuntu turbopi /bin/zsh -c "source ~/.zshrc && ros2 launch bringup bringup.launch.py"`. Full clean bring-up reproduced with this sequence — no port conflicts, no serial exception, camera initializes past the (benign) colorspace-conversion warning without crashing.
- **Still to apply**: bake this restart-before-launch sequence into `start_node.sh` itself so the boot-time path (not just manual runs) gets it — not yet done.
- **Full confirmed node graph from `bringup.launch.py`** (verified directly against the launch file source): `web_video_server`, `rosbridge_websocket` + `rosapi_node`, `mecanum` (the `controller` package's `/cmd_vel` listener), `usb_cam_node_exe` (from `peripherals`), and `start_app.launch.py` (from the `app` package) — which is itself the bundler that brings up all the demo nodes: `gesture_control_node`, `tracking` (object_tracking), `line_following`, `sonar_controller`, `avoidance_node`, `qrcode`. `startup_check` runs 10s after everything else via a `TimerAction`. Note: `bringup.launch.py` also *defines* a `sonar_controller_launch` include but it is commented out of the actual returned launch list — sonar_controller reaches the graph via `start_app` instead, not directly.
- **Correction — no arm/gripper attached (confirmed 2026-09-20 by Utroff)**: an earlier note in this doc claimed "this unit has an arm/gripper" based on `servo_controller` exposing `/arm_controller/follow_joint_trajectory` and `/gripper_controller/follow_joint_trajectory` action servers. **This was wrong.** There is no physical arm or gripper on this build — just the mecanum base, camera, and a 2-servo pan/tilt camera gimbal. Those action servers, and the `/servo_controller` topic (`servo_controller_msgs/ServosPosition`, 0 publishers), are vestigial: they exist only because `bringup` launches Hiwonder's shared `servo_controller` node written for kit variants that do have an arm. Safe to ignore entirely — this matches what `CLAUDE.md`'s "no manipulator" line already said, so no change needed there.
- **Motor control confirmed working end-to-end (2026-09-20):** `ros2 topic pub --once /cmd_vel geometry_msgs/msg/Twist '{linear: {x: 0.3}}'` drove the rover physically forward. Full path — `Twist` → `mecanum` node (`driver/controller/controller/mecanum.py`, real 4-wheel inverse kinematics) → `MotorsSpeedControl` on `/ros_robot_controller/set_motor_speeds` → `ros_robot_controller` → serial → STM32 → wheels — is proven and source-confirmed.
- **Camera frame rate — resolved (2026-09-20):** the ~3Hz → 15Hz → ~20Hz progression across tests was **not** a CPU-contention bug from the concurrent demo nodes (the original theory). Reading `peripherals/config/usb_cam_param.yaml` confirmed the real cause: the config *tries* to fix `autoexposure: false` / `exposure: 100`, but the driver logs `unknown control 'exposure_auto'` — this camera/driver combination silently rejects the fixed-exposure request, so the camera's own auto-exposure firmware governs frame timing, and it hunts/extends exposure time in low light. Confirmed directly: removing the lens cap (better lighting) took it from 3Hz to 15Hz to ~20Hz with no code changes. **Decision: accept ~20Hz as sufficient**, consistent with CLAUDE.md's framing of camera/navigation as instrumentation rather than the research contribution. No further V4L2 control-name chasing planned.

## Source code review completed (2026-09-20)

A local folder (`pi_transfers/ros2_ws_src`) containing the extracted Hiwonder ROS2 workspace source enabled direct code review instead of black-box `ros2 node info`/`ros2 interface show` reverse-engineering. Key findings, all folded into `Progress/turbopi-ros2-programming-guide.md`:

- **`line_following` is fully remote-controllable, not button-gated.** Earlier concern (zero subscribers in `ros2 node info`) is resolved: the node only subscribes to `/image_raw` inside its `~enter` service callback — dormant by design until triggered. Full service interface: `~enter` (Trigger), `~exit` (Trigger), `~set_running` (SetBool), `~set_target_color` (SetPoint, GUI-only), `~set_large_model_target_color` (SetString, the useful one), `~get_target_color`, `~set_threshold`. **Gotcha:** two independent 5-second auto-exit timeouts exist — a heartbeat mechanism (needs periodic pings or the node self-exits) and a lost-target timeout (only active on the large-model-triggered path). **Not yet wired into `rover_pi.py`.**
- **Voice/LLM/TTS pipeline (`large_models` package) is entirely cloud-dependent, confirming there is no local-TTS shortcut anywhere in the stock image.** `config.py` wires everything to OpenAI (`gpt-4o-mini`, `tts-1`, `whisper-1`) or Aliyun/dashscope, with blank API keys by default. **The Piper-vs-browser-stand-in decision below remains genuinely open, no shortcut found.**
- **Camera gimbal confirmed:** 2 PWM servos, ids 1 (tilt) and 2 (pan), controlled via `SetPWMServoState` on `/ros_robot_controller/pwm_servo/set_state`. Used by `rover_pi.py`'s (currently placeholder, unaimed) gimbal sweep.
- **`Dashboard/brain/rover_pi.py` created (2026-09-20):** a first real `PiRoverController` (`ROVER=pi`) implementing `move`/`approach`/`turn` (timed `cmd_vel` pulses), `observe`/`scan` (placeholder gimbal sweep), `stop`/`report` (no-op), and `halt()`. See the programming guide §8 for exactly what it does and doesn't cover yet — notably, `approach` has no real target-grounding (identical to `move` for now), line-following isn't wired in, and the frame-forwarder script (web_video_server → `/ws/execution`) doesn't exist yet, so the perception/revision loop has nothing feeding it on real hardware until that's built.

## The one real gap: real-hardware drive path
`rover.get_rover()` now implements `sim`, `virtual`, **and `pi`** (see above) — but `pi` is a first draft: motion only, no perception feed, no line-following, `approach` unresolved. **Architecture decided (2026-09-17/20):** drive via ROS2 `cmd_vel` (`geometry_msgs/Twist`), reached from the laptop-side `brain/` over `rosbridge_websocket` (JSON WebSocket via `roslibpy`) — no need for a separate Pi-side rclpy client, and no need to drop to raw `HiwonderSDK.Board` calls (that SDK generation doesn't apply to this kit; see `Progress/turbopi-ros2-programming-guide.md`).

**Staged approach:**
1. **Bench sanity — done.** Camera, motors, and full bringup all confirmed working; frame-rate variability understood and accepted (lighting-driven, ~20Hz is fine).
2. **`ROVER=pi` first draft — done**, motion-only. Line-following wiring, gimbal aiming, and the frame-forwarder are the next slice.
3. **Standalone line-following prototype** — reuse the stock `line_following` node via its now-understood service interface, rather than building from scratch, handling the heartbeat/lost-target auto-exit gotchas.
4. **Close the perception loop** — write the frame-forwarder (`web_video_server` → `/ws/execution`) so `mission.py`'s revision logic has something real to react to, before attempting a full voice-driven physical mission.

## Report status
- `literature.tex` (~23KB) and `introduction.tex` (~9.7KB) are substantial.
- `design.tex`, `implementation.tex`, `evaluation.tex`, `conclusion.tex` are all ~700–900 byte stubs — not started.
- Most recent LaTeX build (15 Sept) produced a 0-byte `thesis.bbl` after an `introduction.tex` edit without a full `latexmk` recompile — likely just needs a clean rebuild; unverified.
- Only one virtual-rover run logged so far — RQ2's compositional-depth claim needs a deliberate sweep (varying chained-instruction counts, multiple seeds), not one ad-hoc run. Can run entirely on `ROVER=virtual`, no hardware needed.
- Afrikaans STT accuracy is still untested.
- GA9 still needs a project-specific draft.

## Six-week shape (see artifact for the full week-by-week breakdown)
1. **Wk1 (16–22 Sep):** bench sanity (done) → `ROVER=pi` first draft (done, motion-only) → line-following prototype (reusing stock node via services) + frame-forwarder; virtual RQ2 sweep in parallel; clean LaTeX rebuild.
2. **Wk2 (23–29 Sep):** close the perception loop; first full voice→drive→report loop on the simplest command; decide Piper vs. browser-TTS stand-in (still open — no shortcut in stock image); start Design chapter.
3. **Wk3 (30 Sep–6 Oct):** harden the integration (line lost/regained, ultrasonic obstacle stop, multi-waypoint routes); Implementation chapter.
4. **Wk4 (7–13 Oct):** run the full experiment battery (real hardware if ready, else virtual); generate trace figures via `tools/plot_run.py`; GA9 draft.
5. **Wk5 (14–20 Oct):** Evaluation + Conclusion chapters; consistency pass on Introduction/Literature.
6. **Wk6 (21–27 Oct):** full compile/proofread pass; poster + oral outline first drafts.

## Open decisions flagged for Utroff (not resolved here)
- How much of the remaining time goes to the physical demo vs. the write-up, given CLAUDE.md already treats navigation as instrumentation rather than the research contribution.
- Piper (on-Pi TTS) vs. keeping the browser stand-in — open since the August spec, and now confirmed there is no local-TTS code anywhere in the stock image to borrow from either way.
- Whether a second virtual room fixture is worth building for the "this generalises" argument in Evaluation.
- What `approach` should actually mean on real hardware given there's no landmark/node table like `VirtualRover`'s `RoomMap` — treat it as permanently identical to `move` (and lean on `line_following`/fiducials instead), or try to build real grounding? Leaning toward the former given "navigation is instrumentation," but worth deciding explicitly rather than leaving `rover_pi.py`'s current behaviour as an accident.

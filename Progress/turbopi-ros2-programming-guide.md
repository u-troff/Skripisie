---
Reference notes on writing/running code on the TurboPi (Advanced kit, ROS2/Docker build) so it actually drives the rover, camera and peripherals. Distilled 2026-09-17 from Hiwonder's official docs (docs.hiwonder.com/projects/TurboPi/en/advanced/), cross-checked against this session's hands-on debugging of the `turbopi` container (bringup.launch.py, rosbridge_websocket, web_video_server, usb_cam). Updated 2026-09-20 with confirmed bench results — full stack now boots clean. Updated again 2026-09-20 (later same day) with findings from a direct read of the actual ROS2 workspace source (`pi_transfers/ros2_ws_src`, connected via a local folder) — this supersedes several earlier "probably"/"almost certainly" hedges with source-confirmed facts. Complements `claude/turbopi-connection-guide.md` (in the claude.ai Project), which covers getting *onto* the robot, not writing code for it. Mirrored into this repo at `Progress/turbopi-ros2-programming-guide.md` so Claude Code has it without needing the claude.ai Project.
---

## 1. Two different SDK generations — which one applies here

Hiwonder ships two generations of docs and they are **not interchangeable**:

- **"Standard" kit** (no Raspberry Pi 5, no ROS2): motion is driven by a proprietary Python SDK — `chassis.set_velocity(velocity, direction, yaw_rate)` — with demo scripts in `/home/pi/TurboPi/MecanumControl/`. No ROS2 involved at all.
- **"Advanced" kit** (Raspberry Pi 5, this project's actual hardware per project memory): everything runs **inside a Docker container on top of ROS2**, and motion is driven by publishing to a ROS2 topic, not calling an SDK function directly.

Given the project is on the Advanced kit and this session already found a `turbopi` Docker container running `bringup.launch.py` with `rosbridge_websocket`/`web_video_server`/`usb_cam_node_exe` nodes, **the ROS2 path below is the one that applies** — the `HiwonderSDK.Board.py` direct-call approach described in the connection guide's Mac section appears to be from older/mixed reference material and is not used by this image (confirmed: nothing in the workspace source imports it).

**No arm or gripper is physically attached to this unit (confirmed by Utroff, 2026-09-20).** The workspace does contain a shared `servo_controller` node exposing `/arm_controller/follow_joint_trajectory` and `/gripper_controller/follow_joint_trajectory` action servers, and a `servo_controller_msgs/ServosPosition` topic — these are vestigial, written for Hiwonder kit variants that do have an arm, and launch anyway as part of the shared `bringup`/`start_app` codebase. Ignore them entirely. The only actuators on this build are: the mecanum base (4 wheels), a 2-servo pan/tilt camera gimbal (PWM servos, not the arm's bus servos — see §4), and the camera/sonar sensors.

## 2. Driving the rover — the key finding for `RoverController`

The mecanum chassis is controlled entirely through a **ROS2 topic**, not a function call. Source-confirmed via `driver/controller/controller/mecanum.py` (the `mecanum` node launched by `controller.launch.py`, included from `bringup.launch.py`):

- **Topic:** `cmd_vel`
- **Message type:** `geometry_msgs/msg/Twist`
- **Queue depth:** 1
- **Downstream path (confirmed):** `mecanum` node computes real 4-wheel inverse kinematics from the Twist, then publishes a `ros_robot_controller_msgs/msg/MotorsSpeedControl` (a list of `MotorSpeedControl{id, speed}`, speed range -100..100) on `/ros_robot_controller/set_motor_speeds`; the `ros_robot_controller` node relays that to the STM32 board over serial.

```python
self.mecanum_pub = self.create_publisher(Twist, 'cmd_vel', 1)

twist = Twist()
twist.linear.x = 0.3   # forward/back — stock examples use ~0.3-0.5 for a visible, controlled move
twist.linear.y = 0.5   # strafe left/right (mecanum wheels — can strafe without turning)
twist.angular.z = 3.0  # turn rate — stock examples use ~3.0-5.0 for a visible turn
self.mecanum_pub.publish(twist)
```

**Important correction on `angular.z` semantics:** it is **not** a literal rad/s value. `mecanum.py`'s kinematics sums the angular term directly with the linear terms before scaling to per-wheel motor speed — so the earlier guessed range ("-10..10, real rad/s") was wrong in spirit even if the rough magnitude was in the right ballpark. In practice, stock code (`large_models/large_models/llm_control_move.py`) uses `angular.z = ±3.0` for a ~3.15s turn and `±5.0` combined with `linear.x = 0.4` for a "drift", confirming the practical useful range is roughly 3-8, not a physical angular-velocity unit. Motor stop is always `self.mecanum_pub.publish(Twist())` (all-zero message), used consistently in stock code after every timed move.

**Confirmed working end-to-end this session:**
```bash
docker exec -u ubuntu -w /home/ubuntu turbopi /bin/zsh -c \
  "source ~/.zshrc && ros2 topic pub --once /cmd_vel geometry_msgs/msg/Twist '{linear: {x: 0.3}}'"
```
physically drove the rover forward. Two integration paths from the laptop-side `brain/` FastAPI process, without writing a separate Pi-side rclpy client:

- **Via `rosbridge_websocket`** (confirmed running on the container, port 9090): the laptop publishes to `cmd_vel` over a plain JSON WebSocket message, no ROS2 install needed laptop-side. This is the path used by `Dashboard/brain/rover_pi.py` (via the `roslibpy` client library) and is the fastest integration path given `brain/` already speaks WebSocket (`/ws/dialogue`, `/ws/execution`).
- **Via rclpy directly**, if the laptop ever runs ROS2 itself and DDS discovery reaches the Pi. Not used; no need given rosbridge works.

## 3. Camera & vision pipeline

- Camera bring-up: `usb_cam.launch.py` in the `peripherals` package, included directly by `bringup.launch.py` (confirmed by reading the launch source — no longer a guess).
- **Frame-rate variability explained (confirmed via `peripherals/config/usb_cam_param.yaml`):** the config sets `autoexposure: false` and a fixed `exposure: 100`, intending to lock exposure time and thereby frame rate. The driver logs `unknown control 'exposure_auto'` at startup — it silently rejects this control on this camera, so auto-exposure firmware stays in charge of exposure/frame timing regardless of the yaml. In low light the camera hunts and extends exposure, capping throughput (observed: 3Hz in near-darkness → 15Hz → ~20Hz as lighting improved, with zero code changes). **Accepted as fine at ~20Hz** given the camera is instrumentation, not the project's research contribution; no yaml/driver fix pursued further.
- Frames arrive as standard `sensor_msgs/msg/Image` messages; example nodes convert via `cv_bridge`:
  ```python
  img = self.bridge.imgmsg_to_cv2(msg, "bgr8")
  ```
- `web_video_server` (confirmed running, default port 8080, launched directly as an `ExecuteProcess` in `bringup.launch.py`) exposes the same camera feed as MJPEG over plain HTTP — the simpler integration point for grabbing frames than subscribing to the raw ROS2 topic. **Not yet wired into `brain/`'s `/ws/execution` frame contract** — `main.py`'s `/ws/execution` endpoint expects the Pi side to open the socket and push `{"type": "frame", "image": <b64>}` messages itself; nothing currently does that for the real robot (VirtualRover's test client is the only thing exercising that path so far). A small script that polls `web_video_server`'s snapshot endpoint and forwards frames to `/ws/execution` is still needed — separate piece of work from `rover_pi.py`.
- Official example programs live in `/home/ubuntu/ros2_ws/src/example/example/` — `color_warning.py`, `color_recognize.py`, `color_position_recognition.py` are worth reading as templates: they show the full pattern (subscribe to image topic → `cv_bridge` → `cv2.GaussianBlur` → `cv2.cvtColor` to LAB → `cv2.inRange` threshold → morphology → `cv2.findContours`).
- **`line_following` (`app/app/line_following.py`) confirmed as the reference implementation, and it is remote-controllable, not button-gated** — see §5a below, the key finding from this session's source review.

## 4. Peripherals — gimbal, buzzer, RGB (no arm — see §1)

All controlled via ROS2 topics/messages, not direct GPIO:

- **Camera gimbal (pan/tilt), confirmed schema.** Message `ros_robot_controller_msgs/msg/SetPWMServoState`, confirmed via `ros2 interface show`:
  ```
  SetPWMServoState:
    duration: float64            # seconds to reach position, top-level field
    state: PWMServoState[]
  PWMServoState:
    id: uint16[]                 # servo id(s), e.g. [1] or [2]
    position: uint16[]           # target PWM position, e.g. 1500 = center
    offset: int16[]              # trim offset, usually unused/[0]
  ```
  Topic: `/ros_robot_controller/pwm_servo/set_state`. **Confirmed servo IDs from stock code** (`large_models/large_models/llm_control_move.py`'s `pwm_controller()` helper): **id 1 = tilt** (up/down — used for the "nod" action, sweeping ~1200-1800 around a center of ~1500), **id 2 = pan** (left/right — used for "shake_head", same range/center). Helper pattern:
  ```python
  def pwm_controller(self, *position_data):
      msg = SetPWMServoState()
      msg.duration = 0.2
      msg.state = [PWMServoState(id=[pos[0]], position=[int(pos[1])]) for pos in position_data]
      self.pwm_pub.publish(msg)
  # e.g. self.pwm_controller([1, 1800])  # tilt up
  ```
  This is what `Dashboard/brain/rover_pi.py`'s gimbal sweep for `observe`/`scan` steps uses.
- **Buzzer:** `BuzzerState` message — `freq`, `on_time`, `off_time`, `repeat` fields.
- **RGB status lights:** `RGBStates` message — 0–255 per channel, published on both `ros_robot_controller/set_rgb` and (separately) `sonar_controller/set_rgb` (the sonar unit has its own RGB ring). Stock code (`llm_control_move.py`'s `sonar_rgb_controller`) publishes to both simultaneously for a uniform light-up effect. Could double as mission-status indicators (e.g. `AWAITING_CONFIRMATION`/`BLOCKED` feedback) without adding new hardware. Not wired up yet.

## 5. ROS2 package mechanics (general reference)

Standard ROS2 Humble workflow, confirmed pre-installed on the image (no manual ROS2 install needed):
```bash
mkdir -p ~/hiwonder_ws/src
cd ~/hiwonder_ws
colcon build
source ./install/setup.bash        # add to .zshrc/.bashrc to persist
ros2 pkg create <name> --build-type ament_python --dependencies rclpy
ros2 run <package_name> <node_name>
ros2 topic list / info / echo <topic>
ros2 node list / info <node>
```
Useful for writing a custom node from scratch (e.g. inside the container itself), though the current integration approach avoids needing that — see §2.

**Confirmed workspace package list** (from `pi_transfers/ros2_ws_src`, the extracted `ros2_ws/src`): `app`, `bringup`, `driver/{controller, ros_robot_controller, servo_controller}` (+ msgs packages), `example`, `interfaces` (+ msgs), `large_models` (+ msgs), `peripherals`, `simulations/turbopi_description`, `xf_mic_asr_offline` (+ msgs — the WonderEcho Pro mic package, deferred per Utroff's own instruction to figure out voice integration later).

### 5a. `line_following` — confirmed remote-controllable service interface (2026-09-20)

**This was the key open question from earlier bench testing** (`ros2 node info /line_following` showed zero subscribers, raising a concern it might be physically-button-triggered and incompatible with a remote/voice-driven architecture). **Resolved by reading the source (`app/app/line_following.py`, class `LineFollowingNode`): it is dormant by design, not button-gated.** The `/image_raw` subscription is only created inside the `~enter` service callback — that's why it showed no subscribers before being triggered. Full service interface, all plain ROS2 services (Trigger/SetBool/SetString-family), callable over rosbridge exactly like a topic publish:

- `~enter` (`std_srvs/Trigger`) — activates the node, subscribes to the camera feed, starts the heartbeat.
- `~exit` (`std_srvs/Trigger`) — deactivates.
- `~set_running` (`std_srvs/SetBool`) — start/stop actually driving (vs. just watching).
- `~set_target_color` (`SetPoint`) — GUI/mouse-click color pick, not useful remotely.
- `~set_large_model_target_color` (`large_models_msgs/SetString`) — **the useful one**: set target line color by name (`red`/`green`/`blue`/`black` per the stock LLM prompt in `llm_visual_patrol.py`).
- `~get_target_color`, `~set_threshold`, `~init_finish` — auxiliary/introspection.

**Two auto-exit timeouts to design around:**
1. A heartbeat mechanism (`Heart(self, name + '/heartbeat', 5, on_timeout)`) — the node self-exits if not pinged within 5 seconds of `~enter`. Any external controller keeping the node alive across a longer mission needs to service this (stock code's own heartbeat pattern, or just re-enter as needed).
2. A "lost target" 5-second timeout — only active when triggered via the large-model path (`exit_funcation` flag set); auto-exits if no line has been detected for 5+ seconds. Not active if driven via `~set_running` directly without going through the large-model color-select flow.

**Vision logic** (`LineFollower.__call__`, for reference if a bespoke follower is ever needed instead): RGB→LAB conversion, Gaussian blur, `cv2.inRange` threshold (manually-picked color or a pre-calibrated LAB range from a yaml file, path via `yaml_handle.lab_file_path` — **not yet located/read**, needed before trusting any particular color name works out of the box), erode/dilate, `cv2.findContours`, weighted centroid across 3 horizontal ROI strips → `deflection_angle` via `atan`. PID gains `(3.1, 0.0, 0.0)`; forward speed hardcoded `linear.x = 0.3`; steering `angular.z` clamped to `±8.0`.

**Recommendation:** reuse this stock node via its service interface for the skripsie's line-following prototype rather than writing one from scratch, now that the interface and gotchas are understood — cheaper than reimplementing the vision pipeline, and it already speaks the exact `cmd_vel` convention the rest of the stack uses. **Not yet wired into `rover_pi.py`** — currently only `move`/`turn`/`observe`/`scan`/`stop`/`report` are implemented; line-following start/stop is a follow-up.

## 6. Voice/LLM/TTS demo pipeline (`large_models`) — cloud-only, no local shortcut

Read for completeness and to settle an open question, **not** part of the skripsie's own architecture (which has its own `brain/` pipeline) — kept here as reference:

- `large_models/large_models/config.py`: wires all LLM/VLLM/TTS/ASR calls to either OpenAI (English: `gpt-4o-mini`, `tts-1`, `whisper-1`) or Aliyun/dashscope (Chinese: `qwen3-max`, `sambert-zhinan-v1`), selected by the `ASR_LANGUAGE` env var. All API keys are **blank by default**.
- `tts_node.py`: confirms **no Piper or other local/offline TTS engine exists anywhere in the stock image** — it's `speech.RealTimeTTS` (Aliyun) or `speech.RealTimeOpenAITTS` (OpenAI) only, both cloud calls. **This settles, negatively, the "maybe Piper is already wired up somewhere in the stock image" possibility** — it isn't. The skripsie's own Piper-vs-browser-TTS-stand-in decision (see status doc) remains genuinely open with no shortcut available here.
- `llm_control_move.py` / `llm_visual_patrol.py`: Hiwonder's own reference demos of the pattern LLM output → JSON `{"action": [...], "response": "..."}` → sequenced primitive execution (movement/gimbal/light calls, or triggering `line_following` via the service calls in §5a). Structurally similar to the skripsie's own `brain/` pipeline (LLM → JSON action list → sequenced execution) — useful purely as a **wiring-pattern reference**, not reusable as-is given the cloud dependency.
- Supporting nodes referenced but not yet read: `agent_process.py` (the LLM dispatch node itself), `vocal_detect.py` (wake-word/mic handling), `xf_mic_asr_offline/scripts/wonder_echo_pro_node.py` (WonderEcho Pro mic driver) — lower priority, voice-module integration explicitly deferred by Utroff for later.

## 7. Gaps not resolved by the official docs

- The docs don't confirm whether `HiwonderSDK.Board.py` (raw I2C/Serial calls, described in the connection guide) is still used *underneath* the ROS2 driver node — resolved above: it is not present/imported anywhere in this workspace, so treat the connection guide's SDK section as not applicable to this build.
- `yaml_handle.lab_file_path` (the pre-calibrated LAB color-threshold file `line_following.py` reads) has not yet been located or read — needed before trusting a particular color name (`red`/`green`/`blue`/`black`) is actually calibrated well on this specific unit's camera/lighting.
- `app/launch/start_app.launch.py` (the launch file that actually bundles the demo nodes — `gesture_control_node`, `tracking`, `line_following`, `qrcode`, `avoidance_node`, `sonar_controller`) has not yet been read directly; its node list is inferred from bringup log output, not source-confirmed. Would matter if a trimmed/custom launch (skipping unneeded demo nodes to save CPU) is ever wanted.
- Third-party (non-Hiwonder) ROS2 ports of similar hardware exist and could be useful for comparison if the official example packages turn out to be thin: [wltjr/turbopi_ros](https://github.com/wltjr/turbopi_ros), [KevinEppacher/hiwonder_ros2](https://github.com/KevinEppacher/hiwonder_ros2), [rartino/turbopi](https://github.com/rartino/turbopi) — unverified against this specific image, treat as read-only reference, not something to install.

## 8. `Dashboard/brain/rover_pi.py` — status (2026-09-20)

A real `RoverController` implementation (`PiRoverController`, `ROVER=pi`) now exists in the repo, using `roslibpy` over `rosbridge_websocket` for motion. What it does and doesn't cover:

- `move`/`approach` → timed forward/back `Twist` pulse (config-driven duration/speed, no real distance feedback — same "no magnitude in the plan" situation `VirtualRover` handles with a config constant).
- `turn` → timed `Twist` with `angular.z` set by direction, duration config-driven.
- `observe`/`scan` → a fixed placeholder gimbal sweep (pan left/right/center) via `SetPWMServoState` — **not aimed at anything real yet**, since the real robot has no landmark/node table like `VirtualRover`'s `RoomMap`. Revisit once there's a real perception target to look at.
- `stop`/`report` → no-op.
- `halt()` — publishes a zero `Twist` on the already-open `cmd_vel` connection and sets a local flag. `rover.py`'s ABC docstring says `halt()` "must not require inference, a model, or a network call," and `mission.py` calls it directly from the asyncio event loop (not via `asyncio.to_thread`) on cancellation, so it must never block. `roslibpy.Topic.publish()` on an already-open connection queues the message on the client's own background thread and returns immediately — it never opens a new connection or waits on a round trip. That is the sense in which this satisfies the constraint; it is an interpretation of an ambiguous rule, not a settled one, and is worth a second look if it ever causes a stall.

**Not yet done:**
- `approach` has no real grounding — it's currently identical to `move`. The project's own "navigation is instrumentation" framing (see `CLAUDE.md`) means this may be an acceptable permanent state (reuse `line_following`/a fiducial marker instead of a general "approach" primitive), rather than something to build out further — worth deciding rather than assuming.
- Line-following start/stop (§5a) is not wired into `rover_pi.py` at all yet.
- The frame-forwarder from `web_video_server` to `/ws/execution` (§3) does not exist yet — `run_mission` will execute steps but the perception/revision loop has nothing feeding it on real hardware until that script exists.

## Sources
- [3. Mecanum Wheel Robot Basic Lesson — TurboPi Advanced docs](https://docs.hiwonder.com/projects/TurboPi/en/advanced/docs/3.mecanum_wheel_control.html) — `cmd_vel`/`Twist` API
- [4. ROS+OpenCV Course — TurboPi Advanced docs](https://docs.hiwonder.com/projects/TurboPi/en/advanced/docs/4.ROS+OpenCV_Course.html) — workspace layout, camera/vision pipeline, servo/buzzer/RGB messages
- [1. Read First — TurboPi Advanced docs](https://docs.hiwonder.com/projects/TurboPi/en/advanced/docs/1.getting_ready.html) — chapter TOC, confirms Docker container section exists but content not retrievable
- [8_ROS2_Basic_Course.md (Raspberry Pi 5 Controller wiki, plain-text mirror)](https://wiki.hiwonder.com/projects/Raspberry-Pi-5-Controller/en/latest/_sources/docs/8_ROS2_Basic_Course.md.txt) — general ROS2 package/build/run mechanics, confirms Humble pre-installed
- [8. Large AI Model Courses — TurboPi Advanced docs](https://docs.hiwonder.com/projects/TurboPi/en/advanced/docs/8.Large_AI_Model_Courses.html) — voice/LLM course structure (WonderEcho Pro → Whisper ASR → GPT-4o-mini → TTS), separate from this project's own local-inference pipeline but useful as a structural reference
- Direct source read of `pi_transfers/ros2_ws_src` (this session, 2026-09-20): `bringup/launch/bringup.launch.py`, `peripherals/config/usb_cam_param.yaml`, `driver/controller/controller/mecanum.py`, `app/app/line_following.py`, `large_models/large_models/{config.py, tts_node.py, llm_control_move.py, llm_visual_patrol.py}` — the primary source for every "confirmed" claim added in this update.

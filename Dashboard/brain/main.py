import io
import os
import time
from pathlib import Path
from typing import Optional
import asyncio
import mission as mission_mod
import mission_session
from rover import get_rover
import config,room_map


from pipeline import handle_confirmation_audio,handle_confirmation_text,handle_dialogue_audio,handle_dialogue_text
import av
from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles


import stt
from log_setup import get_logger, setup_logging

log = get_logger("main")
from pipeline import handle_voice_command
import base64
import json

from starlette.concurrency import run_in_threadpool

import dialogue_session
import planner
import scene as scene_mod
from dialogue_session import Phase
from pipeline import handle_confirmation_audio, handle_dialogue_audio
import httpx
from fastapi import Response
import report as report_mod
from providers import usage


GIMBAL_STEP = 100

setup_logging()

def _bool_env(name: str, default: bool) -> bool:
    return config.get(name, "1" if default else "0").strip().lower() not in ("0", "false", "no", "off")

TEXT_COMMANDS_ENABLED = _bool_env("ALLOW_TEXT_COMMANDS", False)
# -- status lights from the dialogue path (spec-supervisor-feedback-2026-10-05.md B) -
_light_failed = False


async def _light(state: str) -> None:
    """Best-effort. get_rover() is the process-wide singleton, so this is the SAME
    controller the mission uses: no second rosbridge connection. After one failure it stops
    trying, so an unreachable Pi cannot add a connect timeout to every utterance."""
    global _light_failed
    if _light_failed:
        return
    try:
        rover = await run_in_threadpool(get_rover)
        setter = getattr(rover, "status_light", None)
        if setter is not None:
            setter(state)
    except Exception:
        _light_failed = True
        log.warning("status light %r unavailable - dialogue lights disabled", state, exc_info=True)


def _light_after(events: list) -> str:
    types = {e.get("type") for e in events}
    if "plan_ready" in types:
        return "confirm"
    if "execute" in types:
        return "executing"
    return "listening"


async def _send_events(websocket, events: list) -> None:
    await _light(_light_after(events))
    for event in events:
        await websocket.send_json(event)



app = FastAPI()
os.makedirs("logs/frames", exist_ok=True)
app.mount("/logs/frames", StaticFiles(directory="logs/frames"), name="frames")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/runtime")
def runtime():
    """What this process is actually configured to run — the header pill in
    App.tsx, and a run's provenance for the report."""
    return {
        "rover": config.get("ROVER", "sim"),
        "planner_profile": planner.profile_info(),
        "planner": report_mod._role_config("planner"),
        "vlm": report_mod._role_config("vlm"),
        "scene_source": config.get("SCENE_SOURCE", "video"),
        "room_map": config.get("ROOM_MAP", "rooms/room_tour1.json"),
        "robot": {
            "length_m": config.get_float("VIRTUAL_ROBOT_LENGTH_M", 0.187),
            "width_m": config.get_float("VIRTUAL_ROBOT_WIDTH_M", 0.162),
            "clearance_m": round(room_map.footprint_clearance_m(), 4),
        },
        "drift": {
            "frac": config.get_float("VIRTUAL_DRIFT_FRAC", 0.0),
            "deg": config.get_float("VIRTUAL_DRIFT_DEG", 0.0),
        },
        "time_scale": config.get_float("VIRTUAL_TIME_SCALE", 1.0),
    }


@app.get("/room")
def room_endpoint():
    """The static room geometry for the live map (§4). Virtual-only — a Pi
    run has no RoomMap to serve, so it gets JSON null (200) rather than a 404
    the browser console would log as a failed request on every page load."""
    rover = get_rover()
    if getattr(rover, "name", None) != "virtual":
        return None
    return rover.room.to_dict()


@app.post("/transcribe")
async def transcribe(audio: UploadFile = File(...), language: str = Form("af")):
    raw = await audio.read()

    # MediaRecorder blobs frequently carry no duration header, so this stays
    # best-effort and the UI renders "—" when it comes back None.
    duration = None
    try:
        with av.open(io.BytesIO(raw), mode="r") as container:
            if container.duration is not None:
                duration = container.duration / 1_000_000
    except Exception:
        pass

    started = time.perf_counter()
    text = stt.transcribe_audio(raw, language=language)

    return {
        "text": text,
        "model": stt.MODEL_NAME,
        "language": language,
        "duration": duration,
        "elapsed": time.perf_counter() - started,
    }


@app.post("/command")
async def command(
    audio: UploadFile = File(...),
    language: str = Form("af"),
    image: Optional[UploadFile] = File(None),
):
    raw = await audio.read()
    # Raw bytes go straight to ollama, which base64-encodes them; no temp file.
    frame = await image.read() if image is not None else None

    started = time.perf_counter()
    result = handle_voice_command(raw, image=frame, language=language)
    result["elapsed"] = time.perf_counter() - started
    result["had_image"] = frame is not None
    return result


@app.websocket("/ws/audio")
async def audio_stream(websocket: WebSocket):
    await websocket.accept()
    buffer = bytearray()
    try:
        while True:
            data = await websocket.receive_bytes()
            if data == b"__END__":
                result = handle_voice_command(bytes(buffer))
                await websocket.send_json(result)
                buffer.clear()
            else:
                buffer.extend(data)
    except WebSocketDisconnect:
        pass

def _decode(value):
    """Audio and frames arrive base64-encoded inside the JSON frame."""
    if not value:
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return base64.b64decode(value)


@app.post("/scene")
async def upload_scene(video: UploadFile = File(...), name: str = Form("")):
    """Room video in, scene digest out.

    Slow on purpose: keyframe extraction is cheap, but every surviving frame
    costs one VLM call. Locally that is roughly 15s a frame, so a five-frame
    clip is well over a minute. It is paid once per room, not per turn — the
    result is also persisted to Dashboard/brain/scenes/ so it survives a
    backend restart and can be picked again via GET /scenes without
    re-uploading or re-running the VLM inventory.
    """
    raw = await video.read()
    built = await run_in_threadpool(scene_mod.build_scene, raw, name)
    return built.to_dict()


@app.get("/scenes")
def list_scenes():
    """Previously catalogued rooms, newest first — for the UI's room picker."""
    return scene_mod.list_saved_scenes()


@app.get("/scene/{scene_id}")
def get_scene(scene_id: str):
    found = scene_mod.store.get(scene_id)
    return found.to_dict() if found else {"error": "unknown scene"}


@app.websocket("/ws/dialogue")
async def dialogue(websocket: WebSocket):
    """Pre-departure phase: clarify → plan → verify → voice confirm.

    Pi -> {"type": "start", "language": "af"}
    Pi -> {"type": "audio_chunk", "audio": <b64>, "image": <b64|null>}
       -> {"type": "confirmation_audio", "audio": <b64>}   (same as audio_chunk;
                                                            the phase decides)
    us -> {"type": "session"} | {"type": "speak"} | {"type": "plan_ready"}
       -> {"type": "execute"} | {"type": "revise"} | {"type": "cancelled"}
       -> {"type": "error"}

    One frame per complete utterance, not a stream: faster-whisper pads every
    clip to 30s, so chunking makes latency worse, not better.
    """
    await websocket.accept()
    session = None
    try:
        while True:
            frame = json.loads(await websocket.receive_text())
            kind = frame.get("type")

            if kind == "start" or session is None:
                # One mission at a time in this process (true for the UI and
                # for tools/virtual_sweep.py), so a new dialogue session is
                # exactly the point to zero the usage ledger for the run
                # about to start — see providers/usage.py.
                usage.reset()
                session = dialogue_session.store.create(
                    session_id=frame.get("session_id"),
                    language=frame.get("language", "af"),
                )
                requested = frame.get("scene_id")
                if requested:
                    found = scene_mod.store.get(requested)
                    if found is None:
                        await websocket.send_json(
                            {"type": "error", "message": f"unknown scene {requested!r}"}
                        )
                    else:
                        session.scene_id = found.scene_id
                        session.scene_text = found.digest()
                        # The VLM still gets something to look at, not just read.
                        session.image = found.representative()
                elif config.get("SCENE_SOURCE", "video").lower() == "room":
                    # Testing mode: no video, no vlm.inventory_frame call. The
                    # planner and ambiguity check see only the fixture
                    # VirtualRover will later resolve against, so a run can't
                    # fail on a mismatch between what a video showed and what
                    # the fixture says is there. session.image stays None —
                    # text-only grounding, on purpose, for this mode.
                    room = room_map.load_room(config.get("ROOM_MAP", "rooms/room_tour1.json"))
                    session.scene_id = "room:" + room.name
                    session.scene_text = room.digest_text()

                await websocket.send_json(
                    {"type": "session", "session_id": session.session_id,
                     "phase": session.phase.value, "scene_id": session.scene_id}
                )
                await _light("listening")
                if kind == "start":
                    continue

            if kind in ("text_command", "confirmation_text"):
                if not TEXT_COMMANDS_ENABLED:
                    await websocket.send_json(
                        {"type": "error",
                         "message": "text commands are disabled — set ALLOW_TEXT_COMMANDS=1"}
                    )
                    continue
                text = str(frame.get("text") or "").strip()
                if not text:
                    await websocket.send_json({"type": "error", "message": "empty text"})
                    continue
                await _light("thinking")
                if session.phase is Phase.AWAITING_CONFIRMATION:
                    events = await run_in_threadpool(handle_confirmation_text, session, text)
                else:
                    events = await run_in_threadpool(
                        handle_dialogue_text, session, text, _decode(frame.get("image"))
                    )
                await _send_events(websocket,events)
                continue


            audio = _decode(frame.get("audio"))
            if not audio:
                await websocket.send_json({"type": "error", "message": "empty audio"})
                continue
            await _light("thinking")
            if session.phase is Phase.AWAITING_CONFIRMATION:
                events = await run_in_threadpool(handle_confirmation_audio, session, audio)
            else:
                events = await run_in_threadpool(
                    handle_dialogue_audio, session, audio, _decode(frame.get("image"))
                )

            await _send_events(websocket,events)
            

    except WebSocketDisconnect:
        pass
    finally:
        if session is not None and session.phase in (Phase.CANCELLED, Phase.EXECUTING):
            dialogue_session.store.drop(session.session_id)

@app.websocket("/ws/execution")
async def execution(websocket: WebSocket):
    """Mid-mission: frames in, revisions out.

    Pi -> {"type": "begin", "session_id": ...}
       -> {"type": "frame", "image": <b64>}
       -> {"type": "revision_audio", "audio": <b64>}
       -> {"type": "abort"}
    """
    await websocket.accept()
    mission = None
    task = None
    rover = get_rover()
    async def emit(event):
        await websocket.send_json(event)

    try:
        while True:
            frame = json.loads(await websocket.receive_text())
            kind = frame.get("type")

            if kind == "begin":
                dialogue = dialogue_session.store.get(frame.get("session_id",""))
                if dialogue is None or dialogue.phase is not Phase.EXECUTING:
                    await emit({"type":"error","message":"no confirmed session to execute"})
                    continue
                mission = mission_session.store.create(dialogue)
                task = asyncio.create_task(mission_mod.run_mission(mission,rover,emit))
                def _log_task_exception(t, _sid=mission.session_id):
                    if t.cancelled():
                        return
                    exc = t.exception()
                    if exc is not None:
                        log.error("[mission %s] run_mission crashed", _sid, exc_info=exc)
                task.add_done_callback(_log_task_exception)
                continue

            if mission is None:
                await emit({"type":"error","message":"no mission found"})
                continue

            if kind == "frame":
                image = _decode(frame.get("image"))
                if image:
                    await mission_mod.ingest_frame(mission,image,emit)

            elif kind =="revision_audio":
                audio = _decode(frame.get("audio"))
                if audio:
                    # Same client message either way; the phase says what the reply is for.
                    if mission.phase is mission_session.MissionPhase.AWAITING_GUIDANCE:
                        await mission_mod.handle_guidance(mission, audio, emit)
                    else:
                        await mission_mod.handle_revision_confirmation(mission,audio,emit)
            elif kind == "guidance_text":
                text = str(frame.get("text") or "").strip()
                if text and TEXT_COMMANDS_ENABLED:
                    await mission_mod.handle_guidance(mission, text, emit)

            elif kind == "abort":
                await mission_mod.abort(mission,rover,emit)

            else:
                await emit({"type":"error","message":f"unknown type {kind!r}"})

    except WebSocketDisconnect:
        pass
    finally:
        if task is not None and not task.done():
            task.cancel()


@app.get("/mission/{session_id}")
def mission_state(session_id:str):
    mission = mission_session.store.get(session_id)
    return mission.snapshot() if mission else {"error":"unknow mission"}


@app.get("/logs")
def list_logs():
    """Summaries of every mission run log, newest first — for the Logs page.
    Reads straight off disk rather than any in-memory store, so a run from a
    previous process restart still shows up."""
    files = sorted(mission_mod.LOGS_DIR.glob("mission_*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    summaries = []
    for path in files:
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        timings = data.get("timings") or {}
        summaries.append({
            "session_id": data.get("session_id") or path.stem[len("mission_"):],
            "command": data.get("command"),
            "outcome": data.get("outcome"),
            "rover": data.get("rover"),
            "model_tag": data.get("model_tag") or report_mod._model_tag(data.get("models") or {}),  # older logs have only `models`
            "plan_was_revised": data.get("plan_was_revised"),
            "target_confirmed": bool(data.get("target_confirmed")),
            "step_count": len(data.get("steps") or []),
            "checks_completed": timings.get("checks_completed"),
            "checks_skipped": timings.get("checks_skipped"),
            "checks_failed": timings.get("checks_failed"),
            "total_s": timings.get("total_s"),
            "revision_count": len(data.get("revisions") or []),
            "mtime": path.stat().st_mtime,
        })
    return summaries


@app.get("/logs/{session_id}")
def get_log(session_id: str):
    """Full report for one run, for the Logs page's detail view."""
    path = mission_mod.LOGS_DIR / f"mission_{session_id}.json"
    if not path.exists():
        return Response(status_code=404, content=b"unknown run")
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return Response(status_code=500, content=b"could not read log")


@app.get("/logs/{session_id}/trace")
def get_log_trace(session_id: str):
    """The virtual rover's recorded movement for one run, for the Logs page's
    map. The mission log names the trace file and which run inside it; a trace
    file holds every run since the process started, so filter to that one."""
    path = mission_mod.LOGS_DIR / f"mission_{session_id}.json"
    if not path.exists():
        return Response(status_code=404, content=b"unknown run")
    try:
        summary = json.loads(path.read_text()).get("rover_summary") or {}
    except (OSError, json.JSONDecodeError):
        return Response(status_code=500, content=b"could not read log")

    trace_name = summary.get("trace")
    run = summary.get("run")
    if not trace_name or run is None:
        return Response(status_code=404, content=b"run has no virtual trace")
    # Basename only: the stored path is absolute and from whichever machine ran it.
    trace_path = mission_mod.LOGS_DIR / Path(str(trace_name).replace("\\", "/")).name
    if not trace_path.exists():
        return Response(status_code=404, content=b"trace file is gone")

    start = None
    steps = []
    try:
        with trace_path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("run") != run:
                    continue
                if record.get("event") == "run_start":
                    start = record
                elif record.get("event") == "step":
                    steps.append({
                        "pose": record.get("pose"),
                        "planned_path": record.get("planned_path"),
                        "status": record.get("status"),
                        "reason": record.get("reason"),
                        "odometer_m": record.get("odometer_m"),
                        "sim_time_s": record.get("sim_time_s"),
                    })
    except OSError:
        return Response(status_code=500, content=b"could not read trace")
    if start is None:
        return Response(status_code=404, content=b"run not found in trace")

    return {"room": start.get("room"), "settings": start.get("settings"),
            "start": start.get("pose"), "steps": steps, "summary": summary}




@app.get("/dialogue/{session_id}")
def dialogue_state(session_id: str):
    """Read-only view for the dashboard while a conversation is in progress."""
    session = dialogue_session.store.get(session_id)
    return session.snapshot() if session else {"error": "unknown session"}

@app.post("/pi/gimbal/{direction}")
def gimbal_control(direction:str):
    rover = get_rover()
    if getattr(rover, "name", None) != "pi":
        return {"error": "gimbal control requires ROVER=pi"}
    if direction == "center":
        return rover.center_gimbal()
    deltas = {
        "up": (0, GIMBAL_STEP), "down": (0, -GIMBAL_STEP),
        "left": (-GIMBAL_STEP, 0), "right": (GIMBAL_STEP, 0),
    }
    if direction not in deltas:
        return {"error": f"unknown direction {direction!r}"}
    pan_delta, tilt_delta = deltas[direction]
    return rover.nudge_gimbal(pan_delta=pan_delta, tilt_delta=tilt_delta)


@app.get("/pi/camera/snapshot")
def camera_snapshot():
    host = config.get("ROVER_PI_HOST", "").strip()
    if not host:
        return Response(status_code=503, content=b"ROVER_PI_HOST not set")
    port = config.get_int("ROVER_PI_CAMERA_PORT", 8080)
    try:
        upstream = httpx.get(f"http://{host}:{port}/snapshot?topic=/image_raw", timeout=2.0)
        upstream.raise_for_status()
    except httpx.HTTPError as exc:
        return Response(status_code=502, content=str(exc).encode())
    return Response(content=upstream.content, media_type="image/jpeg")
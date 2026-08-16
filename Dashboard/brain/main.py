import io
import time
from typing import Optional
import asyncio
import mission as mission_mod
import mission_session
from rover import get_rover

import av
from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

import stt
from log_setup import setup_logging
from pipeline import handle_voice_command
import base64
import json

from starlette.concurrency import run_in_threadpool

import dialogue_session
from dialogue_session import Phase
from pipeline import handle_confirmation_audio, handle_dialogue_audio

setup_logging()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


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
                session = dialogue_session.store.create(
                    session_id=frame.get("session_id"),
                    language=frame.get("language", "af"),
                )
                await websocket.send_json(
                    {"type": "session", "session_id": session.session_id,
                     "phase": session.phase.value}
                )
                if kind == "start":
                    continue

            if kind not in ("audio_chunk", "confirmation_audio", "audio"):
                await websocket.send_json({"type": "error", "message": f"unknown type {kind!r}"})
                continue

            audio = _decode(frame.get("audio"))
            if not audio:
                await websocket.send_json({"type": "error", "message": "empty audio"})
                continue

            if session.phase is Phase.AWAITING_CONFIRMATION:
                events = await run_in_threadpool(handle_confirmation_audio, session, audio)
            else:
                events = await run_in_threadpool(
                    handle_dialogue_audio, session, audio, _decode(frame.get("image"))
                )

            for event in events:
                await websocket.send_json(event)

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
                    await mission_mod.handle_revision_confirmation(mission,audio,emit)
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






@app.get("/dialogue/{session_id}")
def dialogue_state(session_id: str):
    """Read-only view for the dashboard while a conversation is in progress."""
    session = dialogue_session.store.get(session_id)
    return session.snapshot() if session else {"error": "unknown session"}

import io
import time
from typing import Optional

import av
from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

import stt
from log_setup import setup_logging
from pipeline import handle_voice_command

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

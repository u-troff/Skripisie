# Test & Error Log

Running record of tests performed on the Skripsie voice→plan pipeline, and every error
encountered along the way with its cause and resolution.

**Hardware:** Apple M3, 8 cores (4 performance / 4 efficiency), 16 GB RAM, arm64, macOS 25.5.0
**Python:** 3.9.11 (`Dashboard/brain/venv`)
**Session date:** 2026-08-04

---

## 1. Environment

### 1.1 Interpreter inventory

Four Python installations were found on the machine. Most early failures traced back to the wrong
one being selected.

| Interpreter | Version | `fastapi` | `faster_whisper` | `ollama` |
|---|---|---|---|---|
| `Dashboard/brain/venv` | 3.9.11 | yes | yes | yes |
| `Skripsie/.venv` *(deleted)* | 3.9.11 | yes | yes | **no** |
| pyenv global | 3.9.11 | yes | **no** | **no** |
| miniconda base | 3.13.12 | **no** | **no** | **no** |

**Outcome:** consolidated onto `Dashboard/brain/venv`. `Skripsie/.venv` (283 MB) was deleted after
the two environments diverged. `requirements.txt` was regenerated and now captures `ollama==0.6.2`
and `sounddevice==0.5.5`, which had never been recorded.

### 1.2 Audio format support

`faster-whisper` decodes through PyAV (FFmpeg bindings) via `av.open()`, not a fixed format list.

- **408** container formats available in the installed PyAV 15.1.0.
- Confirmed present: `mp3`, `wav`, `flac`, `aiff`, `caf`, `webm`, `matroska`, `ogg`, `opus`,
  `mp4`/`m4a`/`aac`/`3gp`, `wma` (via `asf`), `amr`.
- **File extension is irrelevant** — the container is detected from magic bytes. `decode_audio()`
  accepts `Union[str, BinaryIO]`, so an in-memory `BytesIO` works and no temp file is needed.

---

## 2. Speech-to-text model

### 2.1 Model selection

`digiphyte/fluister-turbo` was verified against the HuggingFace API before adoption (the URL was
supplied with a `utm_source=chatgpt.com` parameter, so existence was checked rather than assumed).

| Property | Value |
|---|---|
| Repo | `digiphyte/fluister-turbo` (HTTP 200, public) |
| Base model | `openai/whisper-large-v3-turbo` (fine-tune) |
| Format | CTranslate2 int8 — loads directly, **no conversion step** |
| Languages | Afrikaans + SA English, incl. code-switching |
| Licence | MIT |
| On-disk size | **781 MB** |
| Publisher-reported WER | 0.086 af / 0.017 en (NCHLT) — *not independently verified* |

Note: the HF API reports `usedStorage` of 1.63 GB, which counts all revisions. The actual current
weights are 781 MB.

### 2.2 Measured performance

Test audio generated with macOS `say`; transcription was character-exact in every run.

| Test | Result |
|---|---|
| First download + load | 82.1 s |
| Cold load from disk cache | **0.7 s** |
| Transcribe 2.9 s audio | 4.48 s |
| Transcribe 14.2 s audio | 5.28 s |
| `beam_size` 1 vs 5 | no measurable difference |
| `cpu_threads` 4 vs 8 | 5.32 s → 4.92 s |

**Key finding — latency is flat with clip length.** Whisper pads every input to a fixed 30-second
window, so the encoder pass dominates and a 2.9 s clip costs almost as much as a 14.2 s one
(0.65× vs 2.69× realtime).

*Design consequence:* send one complete utterance per request. Streaming short chunks multiplies
the fixed cost. The browser recorder was written with no `timeslice` argument for this reason —
and because only the first MediaRecorder chunk carries the container header.

`beam_size=5` was kept since it is free; `cpu_threads=8` was adopted.

**Not tested:** Afrikaans. macOS `say` has no Afrikaans voice, so every measurement above used
synthetic English. The `language="af"` default and the model's core claim remain unverified on
this machine.

---

## 3. Error log

### 3.1 `ModuleNotFoundError: faster_whisper` — editor squiggle

**Symptom:** red underline on the import; package demonstrably installed.
**Cause:** the IDE type checker queried `/Users/utroff/miniconda3/lib/python3.13/site-packages`.
The diagnostic wording (`Site package path queried from interpreter`) identified the checker as
**not Pylance**, so `python.analysis.extraPaths` had no effect.
**Fix:** `Dashboard/brain/ty.toml` and `pyrightconfig.json` pin the checker to `./venv`.

### 3.2 `ModuleNotFoundError: faster_whisper` — at runtime

**Symptom:** uvicorn crashed on boot; traceback showed `pyenv/versions/3.9.11/.../uvicorn/`.
**Cause:** uvicorn was launched from the pyenv global interpreter, which lacks `faster_whisper`.
Terminal venv activation does not affect which interpreter a bare `uvicorn` resolves to.
**Fix:** always launch with an explicit interpreter. `--reload` spawns its worker from
`sys.executable`, so this keeps the subprocess in the venv:

```
Dashboard/brain/venv/bin/python -m uvicorn main:app --reload
```

### 3.3 `TypeError: unsupported operand type(s) for |` — PEP 604 on Python 3.9

**Symptom:** server would not import. Raised at `def check_ambiguity(... image_path: str|None ...)`.
**Cause:** `X | Y` union syntax is Python 3.10+. This environment is 3.9.11.
**Fix:** `Optional[str]` throughout (`vlm.py`, `pipeline.py`).

`from __future__ import annotations` is **not** a general substitute — FastAPI/Pydantic evaluate
route annotations at runtime and will still fail on 3.9.

### 3.4 `KeyError: 'ambiguous'`

**Cause:** two faults in one prompt. The JSON template instructed the model to emit `"ambigous"`
(misspelled) and was missing the colon after the key, while `pipeline.py` read
`ambiguity["ambiguous"]`.
**Fix:** corrected the template; all model-supplied keys are now read with `.get()` and defaults,
since a 3B model omits keys often enough that direct indexing is unsafe.

### 3.5 Frontend/backend contract mismatch

| | UI sent | Backend expected |
|---|---|---|
| field | `audio` | `file` |
| extra | `language` | not accepted |
| response key | read `text` | returned `transcript` |

**Result:** HTTP 422. **Fix:** aligned on `audio` + `language` → `text`.

### 3.6 Assorted defects found by inspection

- `/commmand` route — three `m`s.
- WebSocket handler never appended to its buffer, never broke the loop, and discarded the result.
- `audio_capture.py` passed `audio.tobytes()` — raw PCM has no container header for FFmpeg.
  Fixed by passing the numpy array directly; `transcribe_audio` now accepts `bytes` **or**
  `np.ndarray`.
- Duplicate imports: `tempfile` ×2, `UploadFile` ×2, `ollama` ×2.
- `av.open()` without `mode="r"` types as `OutputContainer`; `container.duration` is `None` for
  containers with no duration header — **common for MediaRecorder WebM**. Now guarded.

---

## 4. Notable: context-window overflow

The most instructive failure of the session.

### Symptom

```
ollama._types.ResponseError: {"error":{"code":400,
  "message":"request (4228 tokens) exceeds the available context size (4096 tokens)",
  "type":"exceed_context_size_error","n_prompt_tokens":4228,"n_ctx":4096}}
```

Raised inside `verify_plan()` → `ollama.chat()`, i.e. at the **last** stage of the pipeline.

### Cause

Two compounding factors:

1. **Ollama's default context window is 4096 tokens.**
2. **`qwen2.5vl` converts image resolution directly into vision tokens.** An uncapped photo
   consumes most of the window before any text is added. `verify_plan` then appends the entire
   serialised plan JSON on top, pushing the request to 4228 tokens.

The ambiguity check survived because its prompt is short; verification failed because it carries
the image *and* the full plan.

### Fix — three parts

1. **Raised the context window** to 8192 on both Ollama calls via
   `options={"num_ctx": ...}`; tunable through `VLM_NUM_CTX` / `PLANNER_NUM_CTX`.
2. **Capped images at 768 px on the long edge** in the browser (canvas re-encode, JPEG q0.85)
   before upload. This is the durable fix — raising `num_ctx` alone still fails for a
   full-resolution phone photo, and it costs no extra Python dependency.
3. **Caught `ollama.ResponseError`** in `vlm.py` and `planner.py`. A failed model call now logs
   and returns a safe default rather than raising a 500.

Point 3 matters disproportionately: the crash occurred *after* STT and planning had already
completed ~20 s of work, all of which was discarded. The pipeline now retains the plan and
records the failure instead.

### Status

**Resolved — confirmed working by the operator.**

---

## 5. Logging

Added `log_setup.py`. Two sinks:

- **Console (INFO)** — stage-by-stage trace: model, duration, step count, each planned step,
  and every decision branch.
- **File (DEBUG)** — `Dashboard/brain/logs/brain.log`, adding full prompts and raw model output.

Replanning after a failed verification is logged explicitly, so a doubled run time is
attributable rather than mysterious.

---

## 6. Outstanding / untested

| Item | Status |
|---|---|
| Afrikaans transcription accuracy | **never tested** — no Afrikaans voice available for synthetic audio |
| Publisher WER figures (0.086 / 0.017) | unverified; re-measure on own audio before citing |
| Combined memory footprint | three models ≈ 7 GB on a 16 GB machine; eviction between planner and verifier steps is plausible |
| `num_ctx=8192` memory cost | doubles KV cache for both models; if stalls appear, try `VLM_NUM_CTX=6144` |
| WebSocket path `/ws/audio` | repaired but never exercised — the UI does not use it |
| Proper nouns / SA place names | known gap per the model card |

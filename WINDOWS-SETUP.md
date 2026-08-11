# Skripsie — Windows setup & project brief

Hand this file to Claude on the Windows machine. It contains everything needed to get the project
running from a clean install, plus the project-specific facts that are easy to get wrong.

The repo is `git@github.com:u-troff/Skripisie.git` (note: the remote spells it "Skripisie").

---

## 0. Read this before you start

Two things in this repo are **not** in git and must be recreated locally:

- `Dashboard/brain/venv` — gitignored. Create it yourself (step 3).
- `CLAUDE.md` — gitignored. That file is the macOS working-instructions file; **this document
  replaces it on Windows.**

`.claude/` and `.vscode/` are also gitignored, so there is no shared editor config.

---

## 1. What this project is

A local voice-commanded rover "brain". Everything runs **offline on CPU** — no cloud API calls.

```
Dashboard/
├── brain/          FastAPI backend (Python 3.9)
│   ├── main.py           HTTP + WebSocket API
│   ├── pipeline.py       STT → ambiguity check → plan → verify
│   ├── stt.py            faster-whisper transcription
│   ├── planner.py        Ollama planner
│   ├── vlm.py            Ollama vision model (ambiguity + plan verification)
│   ├── audio_capture.py  standalone mic capture (sounddevice), CLI-testable
│   ├── log_setup.py       logging to console + brain/logs/brain.log
│   └── requirements.txt
└── ui/             React 18 + Vite dev dashboard
```

**The pipeline** (`pipeline.py::handle_voice_command`):

1. `stt.transcribe_audio` — audio bytes → text.
2. `vlm.check_ambiguity(text, image)` — if ambiguous, stop and return a clarifying question.
3. `planner.generate_plan(text)` — JSON list of steps.
4. `vlm.verify_plan(plan, image)` — if the verifier rejects, replan once with the concerns appended.

Both VLM calls degrade to `{}` on failure and callers use `.get()` with defaults, so a model error
never takes the request down. `verified` defaults to `True` when the verifier gives no answer.

**API surface** (`main.py`):

| Route | Method | Notes |
|---|---|---|
| `/health` | GET | `{"status": "ok"}` |
| `/transcribe` | POST | multipart: `audio`, `language` (default `af`). Returns text + timings |
| `/command` | POST | multipart: `audio`, `language`, optional `image`. Full pipeline |
| `/ws/audio` | WS | accumulates bytes, runs pipeline on the `__END__` sentinel |

CORS is hardcoded to `http://localhost:3000`, and Vite proxies `/api/*` → `http://localhost:8000`
with the `/api` prefix stripped, so the browser only ever sees one origin.

---

## 2. Install the toolchain

Run these in an **Administrator PowerShell**. `winget` ships with Windows 10 1809+ / Windows 11.

```powershell
winget install --id Git.Git                 -e --source winget
winget install --id Python.Python.3.9       -e --source winget
winget install --id OpenJS.NodeJS.LTS       -e --source winget
winget install --id Ollama.Ollama           -e --source winget
```

Close and reopen the terminal afterwards so `PATH` refreshes, then verify:

```powershell
git --version
py -3.9 --version      # expect 3.9.x
node --version         # v20 or v22 LTS is fine
npm --version
ollama --version
```

Notes:

- **Use `py -3.9`, not `python`.** Windows ships an App Execution Alias that hijacks bare `python`
  and opens the Microsoft Store. The `py` launcher avoids it entirely.
- If `winget` can't find `Python.Python.3.9`, get it from python.org and tick *"Add python.exe to
  PATH"* during install.
- **FFmpeg is not required.** `faster-whisper` 1.2.x decodes through PyAV (`av==15.1.0`), which
  bundles its own FFmpeg libraries in the wheel. Install a system FFmpeg only if you want the CLI
  for your own debugging.
- Git for Windows: accept the default `core.autocrlf=true`. All source in this repo is LF-safe.

---

## 3. Clone and create the Python environment

**Python must be 3.9.** The macOS side runs 3.9.11 and the same source has to import on both
machines — see the "Python 3.9 constraint" section below. Do not create a 3.11/3.12 env for this
repo even though the packages would install fine.

```powershell
cd $HOME\Desktop
git clone git@github.com:u-troff/Skripisie.git Skripsie
cd Skripsie\Dashboard\brain

py -3.9 -m venv venv
.\venv\Scripts\python.exe -m pip install --upgrade pip
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

`requirements.txt` is a full pinned freeze (~45 packages). The heavy ones are `ctranslate2==4.8.1`,
`onnxruntime==1.19.2`, `numpy==2.0.2`, `av==15.1.0`, `tokenizers==0.22.2` — all ship cp39
`win_amd64` wheels, so nothing compiles from source. Expect a few hundred MB and a couple of
minutes.

Sanity check the import chain (this is a read-only import, it downloads nothing):

```powershell
.\venv\Scripts\python.exe -c "import faster_whisper, ollama, sounddevice, av, fastapi; print('ok')"
```

`sounddevice==0.5.5` bundles PortAudio in its Windows wheel — no separate install. It is only used
by `audio_capture.py`, which is a standalone CLI helper; the FastAPI server never imports it, so a
missing microphone will not break the server.

### The venv is the environment

Always invoke the interpreter explicitly rather than relying on an activated shell, because
`uvicorn --reload` spawns a subprocess that can otherwise escape the venv:

```powershell
.\venv\Scripts\python.exe -m uvicorn main:app --reload
```

`ty.toml` and `pyrightconfig.json` in `Dashboard/brain/` pin the type checker to `./venv` with
`pythonVersion = 3.9`. They use a relative path, so they work unchanged on Windows.

---

## 4. Install Ollama models

Ollama on Windows installs a background service and starts on login — you do **not** need to run
`ollama serve` manually. Confirm it is up with `ollama list`.

Pull both models:

```powershell
ollama pull gemma4:e2b       # ~7.2 GB — planner
ollama pull qwen2.5vl:3b     # ~3.2 GB — VLM (ambiguity check + plan verification)
```

> ### ⚠️ `gemma4:e2b` is correct — do not "fix" it to `gemma3n:e2b`
> Gemma 4 shipped after the May 2026 model training cutoff, so it *looks* like a typo and it is
> not. The same caution applies to any model tag that seems not to exist: run `ollama list` or
> check the Ollama library rather than assuming. This has been "corrected" wrongly before.

Expected output of `ollama list`:

```
NAME            ID              SIZE      MODIFIED
qwen2.5vl:3b    fb90415cde1e    3.2 GB    ...
gemma4:e2b      7fbdbf8f5e45    7.2 GB    ...
```

**Disk:** models default to `%USERPROFILE%\.ollama\models`. To put them on another drive, set a
machine-level `OLLAMA_MODELS` environment variable *before* pulling, then restart the Ollama
service:

```powershell
[Environment]::SetEnvironmentVariable("OLLAMA_MODELS", "D:\ollama\models", "Machine")
```

**RAM:** a full `/command` run touches all three models (~7 GB of weights plus the Whisper model)
in one request. On a 16 GB machine expect a slow first call and possible Ollama eviction/reload
between the planner and verifier steps. That is normal, not a bug. If Ollama has a GPU on this
machine it will use it automatically and the whole thing will be much faster than the Mac.

---

## 5. Speech-to-text model

`stt.py` loads `digiphyte/fluister-turbo` — an Afrikaans + SA English fine-tune of
whisper-large-v3-turbo, pre-converted to CTranslate2 so there is no conversion step.

- Downloaded automatically from Hugging Face on **first transcription**, not at import.
- Cached in `%USERPROFILE%\.cache\huggingface\hub\`. Roughly 1.5 GB.
- Runs fully locally on CPU (`device="cpu"`, `compute_type="int8"`, `cpu_threads=8`) — only the
  one-time weight download touches the network.

Override without editing code:

```powershell
$env:WHISPER_MODEL   = "digiphyte/fluister-turbo"
$env:WHISPER_LANGUAGE = "en"
```

**Latency:** Whisper pads every clip to a fixed 30-second window, so a run costs ~5s regardless of
clip length. Short clips are disproportionately expensive. Send one complete utterance — do not
stream small chunks.

**The two language defaults disagree — know both:** `stt.LANGUAGE` falls
back to `"en"`, but the `/transcribe` and `/command` form fields default to `"af"`, and the UI
sends `af` unless changed. The form value wins for any request coming from the dashboard.

---

## 6. Frontend

```powershell
cd $HOME\Desktop\Skripsie\Dashboard\ui
npm install
npm run dev
```

React 18.3 + Vite 5.4, dev server on **port 3000** (set in `vite.config.js`, and the backend's CORS
allowlist depends on it — don't let Vite pick a different port silently).

Browser notes for Windows:

- Chrome/Edge support `audio/webm;codecs=opus`, which is first in the `MIME_CANDIDATES` list in
  `App.jsx`. The mp4/ogg fallbacks exist for Safari on macOS and won't be hit here.
- `getUserMedia` requires a secure context. `http://localhost:3000` counts as secure; reaching the
  dev server by LAN IP does not, and the mic will silently fail.
- Images are downscaled client-side to a 768px long edge before upload (`MAX_IMAGE_EDGE`) because
  qwen2.5vl turns resolution into vision tokens and a phone photo alone can blow the context
  window.

---

## 7. Running the whole thing

Two terminals:

```powershell
# Terminal 1 — backend
cd $HOME\Desktop\Skripsie\Dashboard\brain
.\venv\Scripts\python.exe -m uvicorn main:app --reload

# Terminal 2 — frontend
cd $HOME\Desktop\Skripsie\Dashboard\ui
npm run dev
```

Open <http://localhost:3000>. The dashboard polls `/api/health` on load; "checking" that never
resolves means the backend isn't up or isn't on 8000.

**Logs:** `log_setup.py` writes INFO to the console and DEBUG (full prompts and raw model output)
to `Dashboard/brain/logs/brain.log`. That directory is gitignored and created on demand. When a
model returns unparseable JSON, the raw text is in the log file, not the console — check there
first.

---

## 8. Environment variables

All optional; these are the defaults baked into the code.

| Variable | Default | Used by |
|---|---|---|
| `PLANNER_MODEL` | `gemma4:e2b` | `planner.py` |
| `PLANNER_NUM_CTX` | `8192` | `planner.py` |
| `VLM_MODEL` | `qwen2.5vl:3b` | `vlm.py` |
| `VLM_NUM_CTX` | `8192` | `vlm.py` |
| `WHISPER_MODEL` | `digiphyte/fluister-turbo` | `stt.py` |
| `WHISPER_LANGUAGE` | `en` | `stt.py` |
| `OLLAMA_MODELS` | `%USERPROFILE%\.ollama\models` | Ollama service |

Both `num_ctx` values are raised from Ollama's 4096 default deliberately: an image expands into a
large number of vision tokens, and image + serialised plan overflows 4096 easily.

---

## 9. Python 3.9 constraint — the one that bites

Python here is **3.9**, so PEP 604 unions raise `TypeError` at import:

```python
def f(x: str | None): ...      # ✗ TypeError on 3.9
def f(x: Optional[str]): ...   # ✓
```

`from __future__ import annotations` is **not** a general fix — FastAPI and Pydantic evaluate route
annotations at runtime and will still fail. Use `typing.Optional` / `Union` throughout. The
existing code already does this consistently (`main.py`, `stt.py`, `vlm.py`, `pipeline.py`); match
it.

This holds even though nothing on Windows forces 3.9 by itself — the same files are imported by the
macOS 3.9.11 environment, and a 3.10+ syntax change here breaks that machine.

---

## 10. Troubleshooting

| Symptom | Cause |
|---|---|
| `python` opens the Microsoft Store | Windows App Execution Alias. Use `py -3.9`, or turn the alias off in *Settings → Apps → Advanced app settings → App execution aliases*. |
| Editor shows import errors but code runs | The checker is pointed at the wrong interpreter. Select `Dashboard\brain\venv\Scripts\python.exe` as the workspace interpreter. |
| `ollama.ResponseError: model not found` | Model not pulled, or the tag was "corrected". Run `ollama list` and compare against §4. |
| Connection refused on port 11434 | Ollama service not running. Launch the Ollama app once; it registers the startup service. |
| First `/command` takes minutes | Cold model loads (~10 GB across three models) plus the one-time Whisper download. Subsequent calls are much faster. |
| Plan comes back empty with `"planner returned unparseable output"` | The planner emitted non-JSON. The raw output is in `logs\brain.log` at DEBUG. |
| Mic button does nothing | Not a secure context (accessed by LAN IP instead of `localhost`), or Windows mic privacy is blocking the browser — *Settings → Privacy & security → Microphone*. |
| Vite starts on 3001 | Port 3000 was taken. Free it — the backend CORS allowlist only permits 3000. |

---

## 11. Working style expected in this project

Carried over from the macOS `CLAUDE.md`, minus the parts that are macOS-specific:

- **Talk it through; prefer explaining over executing.** Do not run benchmarks, test scripts,
  downloads, or servers unprompted — say what would happen and what the expected result is.
- **Do not create or edit files unless explicitly asked.** Give code in the chat as a text block,
  with the file name and roughly where it goes, and enough surrounding context to place it.
- Read-only inspection (reading files, checking what is installed) is fine when it's needed to
  answer accurately.
- If something genuinely cannot be answered without running it, say so and ask first.

The one exception on this machine is first-time setup: sections 2–6 above are install steps and are
meant to be run.

# Architecture Spec: Swappable Planner/VLM Backend + Multi-Turn Pre-Departure Dialogue

**Project:** PE448 Skripsie — Voice-Commanded Indoor Rover (Utroff, supervised by Theart RP)
**Drafted:** 2026-08-07, via conversation with Claude, for implementation by Claude Code
**Scope:** Two additive changes to the existing `brain/` (FastAPI) + `dashboard/` (Next.js) pipeline:

1. A swappable cloud/local inference backend for the **planner (Gemma)** and **VLM (ambiguity checker)** roles, to benchmark speed/quality against OpenAI and DeepSeek.
2. Replacing the current single-question ambiguity check with a genuine multi-turn clarifying dialogue, gated by an explicit **voice-only** confirmation before the rover executes.

STT (faster-whisper) stays local-only — it is **not** part of the swappable backend.

---

## 0. Design principle this doesn't override

RQ1 of the project is whether a fully local, cloud-free pipeline can do this job. The cloud provider option added here exists **only as a benchmarking mode** — flip it on to generate the local-vs-cloud comparison data for the report, then flip it back. The default / demoed configuration stays 100% local (Ollama). Don't let cloud support quietly become the default runtime path.

---

## 1. Component A — Swappable Planner/VLM Backend

### 1.1 Provider abstraction

New package: `brain/providers/`

```
brain/providers/
  __init__.py
  base.py             # InferenceProvider ABC
  ollama_provider.py  # wraps existing Ollama calls
  cloud_provider.py   # shared OpenAI-compatible client (OpenAI + DeepSeek)
  factory.py          # reads config, returns the right provider per role
```

`base.py`:

```python
from abc import ABC, abstractmethod
from typing import Optional

class InferenceProvider(ABC):
    @abstractmethod
    async def complete(
        self,
        messages: list[dict],
        image_path: Optional[str] = None,
        json_mode: bool = False,
    ) -> str:
        """Return the model's text response.
        image_path is only ever set for VLM calls."""
```

Both `brain/vlm.py` and `brain/planner.py` get refactored to call `provider.complete(...)` instead of talking to Ollama directly. That's the only change their callers need — `pipeline.py` orchestration stays the same.

### 1.2 `OllamaProvider`

Wraps what you already have — no behavior change, just moved behind the interface so it's interchangeable with the cloud provider.

### 1.3 `OpenAICompatibleProvider`

DeepSeek's hosted API uses the same request/response schema as OpenAI's `/chat/completions` endpoint (base URL `https://api.deepseek.com`), so one class covers both providers, parameterized by `base_url`, `api_key`, and `model`:

```python
class OpenAICompatibleProvider(InferenceProvider):
    def __init__(self, base_url: str, api_key: str, model: str):
        ...
    async def complete(self, messages, image_path=None, json_mode=False):
        # POST {base_url}/chat/completions, Bearer auth
        # json_mode -> response_format={"type": "json_object"}
        # image_path -> base64 image block in message content (OpenAI vision schema)
```

**Constraint to build around:** DeepSeek's hosted API is currently text-only — it does not accept image input on its documented models, and its message schema requires plain text content. That means DeepSeek can stand in for the **planner** (already text-only) but **not** for the **VLM** role. For cloud VLM testing, OpenAI is your only option (`gpt-4o-mini` or `gpt-4o`, both vision-capable).

DeepSeek's model naming is actively in flux as of writing — the legacy `deepseek-chat` / `deepseek-reasoner` names are being phased out in favor of `deepseek-v4-flash` / `deepseek-v4-pro`. Don't hardcode a tag in this spec — check DeepSeek's docs for the current name when you wire this up.

### 1.4 Config

Add to `.env`:

```
PLANNER_PROVIDER=ollama        # ollama | openai | deepseek
VLM_PROVIDER=ollama            # ollama | openai   (deepseek invalid here — no vision)

PLANNER_MODEL_OLLAMA=gemma3:4b
PLANNER_MODEL_OPENAI=gpt-4o-mini
PLANNER_MODEL_DEEPSEEK=deepseek-chat     # verify current tag before use

VLM_MODEL_OLLAMA=qwen2.5vl:3b
VLM_MODEL_OPENAI=gpt-4o-mini

OPENAI_API_KEY=
DEEPSEEK_API_KEY=
```

`factory.py` reads these and returns the right `InferenceProvider` for `"planner"` and `"vlm"` independently — so you can run the planner on DeepSeek while the VLM stays local, for granular A/B testing.

### 1.5 Smaller local VLM candidates

Since you want to test below Qwen2.5-VL's 3B floor, current options worth benchmarking:

- **Moondream 2 (1.8B)** — smallest, fastest. You've already hit the known Ollama bug where a plain-text `ollama run moondream` call returns numeric vectors instead of text; the existing workaround (moondream's own Python client) applies here too.
- **SmolVLM2 (2.2B)** — Apache 2.0, purpose-built for exactly this edge-deployment case.
- **PaliGemma 2 (3B)** — similar size to your current model, OCR-strong, useful as a same-size comparison point rather than a smaller one.
- **Gemma vision-capable variants** — the Ollama library now lists vision tags under the Gemma family. Worth checking since you're already using Gemma for planning; one model family for both roles is a clean story for your modularity argument.

These are all local/Ollama options, independent of the cloud provider work above — they slot into `VLM_MODEL_OLLAMA`.

### 1.6 Benchmark logging

Extend your existing CSV benchmark script (Ollama + Python, not the LM Studio gut-check) with columns: `provider`, `model`, `role` (planner/vlm). Keep the cold/warm latency split for Ollama rows — it doesn't apply to cloud rows, so leave those cells blank. Optionally log estimated cost per call for cloud rows — cheap to add now, useful for the report later.

---

## 2. Component B — Multi-Turn Pre-Departure Dialogue (replaces the single-question flow)

### 2.1 What changes

**Today:** one ambiguity check → at most one clarifying question → move to planning.
**New:** keep looping clarifying questions until the VLM is confident the command is unambiguous, then read back a plan summary and require explicit voice confirmation before the rover moves. The no-mid-mission-intervention principle still holds — this only changes what happens *before* departure.

### 2.2 Recommended transport: move this phase to WebSocket too

Today HTTP handles the one-off command/ambiguity/plan exchange. A multi-round conversation doesn't fit a one-shot request/response well — recommend giving the pre-departure phase its own Pi-initiated WebSocket, structurally similar to the one you already have for execution monitoring:

```
Pi → backend: {"type": "audio_chunk", "session_id": ..., "audio": <bytes/base64>}
backend → Pi: {"type": "speak", "text": "..."}            # clarifying question OR confirmation prompt
backend → Pi: {"type": "plan_ready", "plan": {...}}        # clarification loop has exited
Pi → backend: {"type": "confirmation_audio", "audio": ...}
backend → Pi: {"type": "execute"} | {"type": "revise"}
```

If you'd rather keep this on HTTP with the backend holding session state and the Pi polling, that works too — either way the backend needs a `session_id`-keyed state store. WebSocket is flagged as the cleaner fit for a genuine back-and-forth, but this is your call, not a hard requirement.

### 2.3 Session state

A simple in-memory dict keyed by `session_id` is enough for a single-rover research demo — no need for Redis or a database. Track: original command, full turn history (question/answer pairs), current phase, and the plan once generated.

```
CLARIFYING            → loop until VLM reports unambiguous, or max turns hit
PLANNING              → plan generated by Gemma
VERIFYING             → VLM checks the plan
AWAITING_CONFIRMATION → plan read back, waiting on voice confirm
EXECUTING             → handed off to existing execution WebSocket
REPORTING             → existing structured report flow
```

### 2.4 Max-turn safety cap

Uncapped clarification loops are a real risk. Add a config value (`MAX_CLARIFYING_TURNS`, suggest starting at 5). If the cap is hit before the VLM is confident, don't silently guess — read back your best-guess interpretation as part of the confirmation prompt ("I'm not fully sure, but here's my best interpretation — confirm or cancel") so the residual uncertainty surfaces to the human instead of getting buried. This also doubles as data for RQ2 (how much vagueness the system tolerates before breaking down): log how often the cap gets hit and on what kind of commands.

### 2.5 Final confirmation — voice only

Since this is voice-only and the last safety check before a physical rover moves, recommend a two-stage check rather than trusting a single model call:

1. **Keyword pass first** — fast, deterministic, no model latency or hallucination risk on the safety-critical gate. Whitelist things like "yes", "confirm(ed)", "go", "execute", "proceed", "do it".
2. **Fallback to lightweight model classification** only if the keyword pass doesn't match — classify the STT transcript as CONFIRM / REJECT / UNCLEAR. On REJECT or a spoken change request, loop back to CLARIFYING with the objection appended to turn history. On UNCLEAR, re-prompt once or twice, then fail safe (abort, don't execute) rather than guess.

### 2.6 Open item: text-to-speech

Nothing in the current pipeline generates the spoken side of "clarifying questions back through the robot's speaker" yet — this loop needs a TTS engine to actually work. Given RQ1's local-only framing, a local engine like **Piper** fits better than a cloud TTS API. Worth deciding before Claude Code starts on this, since it determines what `{"type": "speak", ...}` actually does on the Pi side.

---

## 3. Files to create / modify

**New:**
- `brain/providers/base.py`
- `brain/providers/ollama_provider.py`
- `brain/providers/cloud_provider.py`
- `brain/providers/factory.py`
- `brain/dialogue_session.py` — session state + turn loop logic
- `brain/confirmation.py` — keyword + fallback classification for the confirm gate

**Modified:**
- `brain/vlm.py` — call through `InferenceProvider`; extend ambiguity check into the loop
- `brain/planner.py` — call through `InferenceProvider`
- `brain/pipeline.py` — orchestrate CLARIFYING → PLANNING → VERIFYING → AWAITING_CONFIRMATION → EXECUTING
- `brain/main.py` — add the new pre-departure WebSocket endpoint (or session HTTP, per §2.2)
- `.env` — provider/model config as in §1.4
- benchmark CSV script — add `provider` / `model` / `role` columns

---

## 4. Open items still needing a decision

- TTS engine for the clarifying questions (§2.6)
- WebSocket vs. HTTP+polling for the pre-departure phase (§2.2) — recommendation given, not decided
- Exact `MAX_CLARIFYING_TURNS` value
- Whether to keep DeepSeek in scope given it only covers the planner, not the VLM — or treat this as OpenAI + Ollama for the VLM comparison and add DeepSeek on the planner side only

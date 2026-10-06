import copy
import time
from typing import Any, Dict, List, Optional

import ollama

from .base import Completion, ImageSource, InferenceProvider, ProviderError

_RUNNER_RETRIES = 2


class OllamaProvider(InferenceProvider):
    """Wraps the calls planner.py and vlm.py used to make directly.

    No behaviour change — same model, same num_ctx, same format="json".
    """

    name = "ollama"
    supports_vision = True

    def __init__(
        self,
        model: str,
        num_ctx: int = 8192,
        host: Optional[str] = None,
        repeat_penalty: Optional[float] = None,
        num_predict: Optional[int] = None,
        keep_alive: Optional[str] = None,
        think: Optional[bool] = None,
    ):
        super().__init__(model)
        self.num_ctx = num_ctx
        self.repeat_penalty = repeat_penalty
        self.num_predict = num_predict
        # Blank = Ollama's own default (5 min). Set (e.g. "30m") so a single
        # model serving both the planner and VLM roles stays resident
        # between calls instead of reloading.
        self.keep_alive = keep_alive
        # None = don't send the field (non-thinking models reject it). False
        # stops gemma4 spending num_predict on reasoning before the JSON.
        self.think = think
        # Client(host=None) resolves to 127.0.0.1:11434, matching the old
        # module-level ollama.chat() calls.
        self._client = ollama.Client(host=host or None)

    def complete(
        self,
        messages: List[Dict[str, Any]],
        image: Optional[ImageSource] = None,
        json_mode: bool = False,
    ) -> Completion:
        payload = copy.deepcopy(messages)
        if image is not None:
            for message in reversed(payload):
                if message.get("role") == "user":
                    message["images"] = [image]
                    break

        options: Dict[str, Any] = {"num_ctx": self.num_ctx}
        if self.repeat_penalty is not None:
            options["repeat_penalty"] = self.repeat_penalty
        if self.num_predict is not None:
            options["num_predict"] = self.num_predict

        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": payload,
            "options": options,
        }
        if json_mode:
            kwargs["format"] = "json"
        if self.think is not None:
            kwargs["think"] = self.think
        if self.keep_alive:
            kwargs["keep_alive"] = self.keep_alive

        started = time.perf_counter()
        # gemma4:e4b's runner intermittently dies during CUDA init on a cold
        # load (2026-10-04: same request fails, then succeeds seconds later), so
        # a runner crash is retried before it is allowed to abort a mission. The
        # same goes for Ollama killing a looping generation ("token repeat limit
        # reached", seen on gemma4:e2b): sampling is random, so a retry usually passes.
        for attempt in range(_RUNNER_RETRIES + 1):
            try:
                response = self._client.chat(**kwargs)
                break
            except Exception as exc:  # ResponseError, ConnectionError, httpx errors
                if attempt < _RUNNER_RETRIES and ("llama-server" in str(exc)
                                                  or "token repeat limit" in str(exc)):
                    time.sleep(2)
                    continue
                raise ProviderError(f"ollama {self.model}: {exc}") from exc
        elapsed = time.perf_counter() - started

        try:
            text = response["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise ProviderError(f"ollama {self.model}: malformed response") from exc

        return Completion(
            text=text,
            provider=self.name,
            model=self.model,
            latency_s=elapsed,
            prompt_tokens=getattr(response, "prompt_eval_count", None),
            completion_tokens=getattr(response, "eval_count", None),
        )

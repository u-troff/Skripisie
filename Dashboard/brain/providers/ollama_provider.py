import copy
import time
from typing import Any, Dict, List, Optional

import ollama

from .base import Completion, ImageSource, InferenceProvider, ProviderError


class OllamaProvider(InferenceProvider):
    """Wraps the calls planner.py and vlm.py used to make directly.

    No behaviour change — same model, same num_ctx, same format="json".
    """

    name = "ollama"
    supports_vision = True

    def __init__(self, model: str, num_ctx: int = 8192, host: Optional[str] = None):
        super().__init__(model)
        self.num_ctx = num_ctx
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

        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": payload,
            "options": {"num_ctx": self.num_ctx},
        }
        if json_mode:
            kwargs["format"] = "json"

        started = time.perf_counter()
        try:
            response = self._client.chat(**kwargs)
        except Exception as exc:  # ResponseError, ConnectionError, httpx errors
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

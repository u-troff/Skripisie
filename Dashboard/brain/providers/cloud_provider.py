import base64
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from .base import Completion, ImageSource, InferenceProvider, ProviderError

_RETRYABLE = {408, 409, 429, 500, 502, 503, 504}


def _data_uri(image: ImageSource) -> str:
    if isinstance(image, str):
        if image.startswith(("http://", "https://", "data:")):
            return image
        raw = Path(image).read_bytes()
    else:
        raw = image
    mime = "image/png" if raw[:4] == b"\x89PNG" else "image/jpeg"
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))


class OpenAICompatibleProvider(InferenceProvider):
    """One class for OpenAI and DeepSeek — same /chat/completions schema.

    DeepSeek's hosted models are text-only, so the factory constructs it with
    supports_vision=False and refuses to hand it to the vlm role.
    """

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str,
        name: str = "openai",
        supports_vision: bool = True,
        timeout: float = 60.0,
        max_retries: int = 2,
        cost_per_1m_input: Optional[float] = None,
        cost_per_1m_output: Optional[float] = None,
    ):
        super().__init__(model)
        if not api_key:
            raise ProviderError(f"{name}: API key is empty — set it in .env")
        self.name = name
        self.supports_vision = supports_vision
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self._cost_in = cost_per_1m_input
        self._cost_out = cost_per_1m_output
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": "Bearer " + api_key,
                "Content-Type": "application/json",
            },
        )

    def complete(
        self,
        messages: List[Dict[str, Any]],
        image: Optional[ImageSource] = None,
        json_mode: bool = False,
    ) -> Completion:
        if image is not None and not self.supports_vision:
            raise ProviderError(f"{self.name}: {self.model} does not accept images")

        payload_messages = [dict(message) for message in messages]
        if image is not None:
            for message in reversed(payload_messages):
                if message.get("role") == "user":
                    message["content"] = [
                        {"type": "text", "text": message.get("content", "")},
                        {"type": "image_url", "image_url": {"url": _data_uri(image)}},
                    ]
                    break

        body: Dict[str, Any] = {
            "model": self.model,
            "messages": payload_messages,
            "temperature": 0,
        }
        if json_mode:
            # Every prompt in this codebase contains the word JSON, which
            # OpenAI requires when json_object is requested.
            body["response_format"] = {"type": "json_object"}

        url = self.base_url + "/chat/completions"
        started = time.perf_counter()
        last_error = ""

        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.post(url, json=body)
            except httpx.HTTPError as exc:
                last_error = str(exc)
            else:
                if response.status_code == 200:
                    return self._parse(response.json(), time.perf_counter() - started)
                last_error = "HTTP %d: %s" % (response.status_code, response.text[:300])
                if response.status_code not in _RETRYABLE:
                    break
            if attempt < self.max_retries:
                time.sleep(2 ** attempt)

        raise ProviderError(f"{self.name} {self.model}: {last_error}")

    def _parse(self, data: Dict[str, Any], elapsed: float) -> Completion:
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"{self.name}: malformed response") from exc

        usage = data.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")

        cost = None
        if self._cost_in is not None and self._cost_out is not None:
            cost = (
                (prompt_tokens or 0) * self._cost_in
                + (completion_tokens or 0) * self._cost_out
            ) / 1_000_000

        return Completion(
            text=text,
            provider=self.name,
            model=self.model,
            latency_s=elapsed,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
        )

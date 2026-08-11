from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

# Uploads arrive as bytes and never touch disk; a path is accepted for
# scripted benchmark runs that read frames off the VLM test set.
ImageSource = Union[str, bytes]


class ProviderError(RuntimeError):
    """A backend refused or failed a call.

    planner.py and vlm.py catch this and degrade gracefully — the same
    contract they had when they caught ollama.ResponseError directly.
    """


@dataclass
class Completion:
    text: str
    provider: str
    model: str
    latency_s: float
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    cost_usd: Optional[float] = None


class InferenceProvider(ABC):
    """One inference backend for one role (planner or vlm)."""

    name = "base"
    supports_vision = True

    def __init__(self, model: str):
        self.model = model

    @abstractmethod
    def complete(
        self,
        messages: List[Dict[str, Any]],
        image: Optional[ImageSource] = None,
        json_mode: bool = False,
    ) -> Completion:
        """Return the model's text response.
        image is only ever set for VLM calls."""


def user_message(text: str) -> Dict[str, Any]:
    return {"role": "user", "content": text}


def log_completion(log, role: str, stage: str, completion: Completion) -> None:
    """One grep-able line per model call — this is the benchmark feed.

        grep '\\[bench\\]' logs/brain.log

    Cold/warm split is not recorded here; that stays the benchmark script's
    job, and the field is meaningless for cloud rows anyway.
    """
    log.info(
        "[bench] role=%s stage=%s provider=%s model=%s latency=%.3f "
        "prompt_tokens=%s completion_tokens=%s cost_usd=%s",
        role,
        stage,
        completion.provider,
        completion.model,
        completion.latency_s,
        completion.prompt_tokens if completion.prompt_tokens is not None else "",
        completion.completion_tokens if completion.completion_tokens is not None else "",
        f"{completion.cost_usd:.6f}" if completion.cost_usd is not None else "",
    )

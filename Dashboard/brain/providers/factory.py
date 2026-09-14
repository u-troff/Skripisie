from typing import Dict, Tuple

import config
from typing import Optional
from log_setup import get_logger

from .base import InferenceProvider, ProviderError
from .cloud_provider import OpenAICompatibleProvider
from .ollama_provider import OllamaProvider

log = get_logger("providers")

ROLES = ("planner", "vlm")
VISION_CAPABLE = {"ollama", "openai"}

# Old single-name env vars, honoured so an existing shell/launch config keeps
# working after this refactor.
_LEGACY_MODEL_VAR = {"planner": "PLANNER_MODEL", "vlm": "VLM_MODEL"}
_DEFAULT_MODEL = {
    ("planner", "ollama"): "gemma4:e2b",
    ("planner", "openai"): "gpt-4o-mini",
    ("planner", "deepseek"): "deepseek-chat",  # verify the current tag
    ("vlm", "ollama"): "qwen2.5vl:3b",
    ("vlm", "openai"): "gpt-4o-mini",
}
_DEFAULT_NUM_CTX = {"planner": 8192, "vlm": 8192}
_DEFAULT_REPEAT_PENALTY = 1.3
# Only the VLM's per-frame cataloguing prompt has shown degenerate looping
# (a near-zero-temperature model repeating the same object block instead of
# ever finishing); the planner is left uncapped since plan length legitimately
# varies with the task.
_DEFAULT_NUM_PREDICT = {"vlm": 768}

_cache: Dict[Tuple[str, str, str], InferenceProvider] = {}


def _resolve(role: str) -> Tuple[str, str]:
    provider = config.get(role.upper() + "_PROVIDER", "ollama").lower()
    model = config.get(
        "%s_MODEL_%s" % (role.upper(), provider.upper()),
        config.get(_LEGACY_MODEL_VAR[role], _DEFAULT_MODEL.get((role, provider), "")),
    )
    return provider, model

def _optional_float(name: str) -> Optional[float]:
    """Blank means "do not send this field at all" — prices change and are not
    worth hardcoding, and temperature is model-dependent."""
    value = config.get(name)
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _optional_int(name: str, default: Optional[int] = None) -> Optional[int]:
    value = config.get(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default



def get_provider(role: str) -> InferenceProvider:
    if role not in ROLES:
        raise ProviderError(f"unknown role {role!r}")

    provider_name, model = _resolve(role)
    if not model:
        raise ProviderError(f"{role}: no model configured for provider {provider_name!r}")
    if role == "vlm" and provider_name not in VISION_CAPABLE:
        raise ProviderError(
            f"VLM_PROVIDER={provider_name} has no vision support — use ollama or openai"
        )

    key = (role, provider_name, model)
    if key in _cache:
        return _cache[key]

    if provider_name == "ollama":
        instance: InferenceProvider = OllamaProvider(
            model=model,
            num_ctx=config.get_int(role.upper() + "_NUM_CTX", _DEFAULT_NUM_CTX[role]),
            host=config.get("OLLAMA_HOST"),
            repeat_penalty=config.get_float("OLLAMA_REPEAT_PENALTY", _DEFAULT_REPEAT_PENALTY),
            num_predict=_optional_int(role.upper() + "_NUM_PREDICT", _DEFAULT_NUM_PREDICT.get(role)),
        )
    elif provider_name in ("openai", "deepseek"):
        prefix = provider_name.upper()
        instance = OpenAICompatibleProvider(
            model=model,
            base_url=config.get(
                prefix + "_BASE_URL",
                "https://api.openai.com/v1"
                if provider_name == "openai"
                else "https://api.deepseek.com/v1",
            ),
            api_key=config.get(prefix + "_API_KEY"),
            name=provider_name,
            supports_vision=provider_name in VISION_CAPABLE,
            timeout=config.get_float("CLOUD_TIMEOUT_S", 60.0),
            cost_per_1m_input=_optional_float(prefix+"_COST_PER_1M_INPUT"),
            cost_per_1m_output=_optional_float(prefix+"_COST_PER_1M_OUTPUT"),
        )
    else:
        raise ProviderError(f"unknown provider {provider_name!r} for role {role}")

    log.info("[factory] %s -> %s/%s", role, provider_name, model)
    _cache[key] = instance
    return instance


def reset_cache() -> None:
    """Drop cached providers so a benchmark run can flip env vars in-process."""
    _cache.clear()

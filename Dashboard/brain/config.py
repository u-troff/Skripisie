"""Config loading for the brain.

Reads Dashboard/brain/.env once at import, then exposes typed getters.
Real environment variables win over the file, so a one-off benchmark run is:

    PLANNER_PROVIDER=deepseek venv/bin/python -m uvicorn main:app --reload

python-dotenv is deliberately not used: it would be a new dependency in an
environment otherwise pinned by requirements.txt.
"""

import os
from pathlib import Path

ENV_FILE = Path(__file__).parent / ".env"


def load_env(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        quoted = len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'"
        if quoted:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #")[0].strip()
        # setdefault, not assignment: a real env var must survive this.
        os.environ.setdefault(key.strip(), value)


load_env()


def get(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value if value else default


def get_int(name: str, default: int) -> int:
    try:
        return int(get(name, str(default)))
    except ValueError:
        return default


def get_float(name: str, default: float) -> float:
    try:
        return float(get(name, str(default)))
    except ValueError:
        return default

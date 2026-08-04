import logging
import sys
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"
LOG_FILE = LOG_DIR / "brain.log"

# Console stays readable; the file keeps full prompts and raw model output.
CONSOLE_FORMAT = "%(asctime)s %(levelname)-5s %(name)-14s %(message)s"
FILE_FORMAT = "%(asctime)s %(levelname)-5s %(name)-14s %(message)s"


def setup_logging() -> None:
    root = logging.getLogger("brain")
    if root.handlers:
        return

    LOG_DIR.mkdir(exist_ok=True)
    root.setLevel(logging.DEBUG)
    root.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(CONSOLE_FORMAT, "%H:%M:%S"))
    root.addHandler(console)

    to_file = logging.FileHandler(LOG_FILE, encoding="utf-8")
    to_file.setLevel(logging.DEBUG)
    to_file.setFormatter(logging.Formatter(FILE_FORMAT, "%Y-%m-%d %H:%M:%S"))
    root.addHandler(to_file)

    root.info("logging to %s", LOG_FILE)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger("brain." + name)

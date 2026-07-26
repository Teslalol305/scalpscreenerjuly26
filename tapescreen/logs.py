"""Structured jsonl logging: INFO to console (terse) + jsonl file (full).

Use: setup_logging(cfg.log_path, feed_debug=cfg.feed_debug) once at startup.
Depends on: stdlib logging only.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

_STD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "taskName"}


class JsonlFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():  # `extra=` fields
            if k not in _STD_ATTRS:
                doc[k] = v
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return json.dumps(doc, default=str)


def setup_logging(log_dir: str | Path, feed_debug: bool = False) -> Path:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if feed_debug else logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(console)

    d = Path(log_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"tapescreen-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setLevel(logging.DEBUG if feed_debug else logging.INFO)
    fh.setFormatter(JsonlFormatter())
    root.addHandler(fh)

    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    return path

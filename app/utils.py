from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import re
import sys
from pathlib import Path
from urllib.parse import quote_plus


def ensure_dirs(*paths: Path) -> None:
    for path in paths:
        if path.suffix:
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path.mkdir(parents=True, exist_ok=True)


def setup_logging(log_file: Path) -> None:
    ensure_dirs(log_file)
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"['’]", "", value)
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def quote_query(value: str) -> str:
    return quote_plus(value.strip())


def clean_line(value: object | None) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def write_json_env_file(value: str | None, path: Path) -> None:
    """Materialize a JSON env var into a file on disk.

    Always rewrites the file when the env value differs from what's on disk so
    operators can rotate cookies via env var without manual file deletion.
    """
    if not value:
        return
    parsed = json.loads(value)
    new_payload = json.dumps(parsed, ensure_ascii=False)
    ensure_dirs(path)
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if hashlib.sha256(existing.encode("utf-8")).digest() == hashlib.sha256(
            new_payload.encode("utf-8")
        ).digest():
            return
    path.write_text(new_payload, encoding="utf-8")

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    if value is None or value.strip() == "":
        return default
    return int(value)


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_chat_id: int
    telegram_admin_id: int
    telegram_admin_username: str | None
    database_url: str | None
    database_path: Path
    brands_file: Path
    poll_interval_seconds: int
    goofish_enabled: bool
    goofish_storage_state: Path
    goofish_storage_state_json: str | None
    goofish_headless: bool
    grailed_enabled: bool
    grailed_storage_state: Path
    grailed_storage_state_json: str | None
    grailed_email: str | None
    grailed_password: str | None
    grailed_headless: bool
    grailed_proxy_url: str | None
    goofish_proxy_url: str | None
    parser_timeout_ms: int
    max_items_per_source: int
    max_sends_per_run: int
    log_file: Path

    @property
    def project_root(self) -> Path:
        return Path.cwd()

    @property
    def uses_postgres(self) -> bool:
        return bool(self.database_url)


def load_settings() -> Settings:
    load_dotenv(".env", override=False)
    load_dotenv(".env.local", override=True)

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

    return Settings(
        telegram_bot_token=token,
        telegram_chat_id=_int(os.getenv("TELEGRAM_CHAT_ID"), 0),
        telegram_admin_id=_int(os.getenv("TELEGRAM_ADMIN_ID"), 0),
        telegram_admin_username=os.getenv("TELEGRAM_ADMIN_USERNAME") or None,
        database_url=os.getenv("DATABASE_URL") or None,
        database_path=Path(os.getenv("DATABASE_PATH", "data/app.sqlite3")),
        brands_file=Path(os.getenv("BRANDS_FILE", "brands.yaml")),
        poll_interval_seconds=_int(os.getenv("POLL_INTERVAL_SECONDS"), 60),
        goofish_enabled=_bool(os.getenv("GOOFISH_ENABLED"), True),
        goofish_storage_state=Path(os.getenv("GOOFISH_STORAGE_STATE", "state/goofish.json")),
        goofish_storage_state_json=os.getenv("GOOFISH_STORAGE_STATE_JSON") or None,
        goofish_headless=_bool(os.getenv("GOOFISH_HEADLESS"), True),
        grailed_enabled=_bool(os.getenv("GRAILED_ENABLED"), True),
        grailed_storage_state=Path(os.getenv("GRAILED_STORAGE_STATE", "state/grailed.json")),
        grailed_storage_state_json=os.getenv("GRAILED_STORAGE_STATE_JSON") or None,
        grailed_email=os.getenv("GRAILED_EMAIL") or None,
        grailed_password=os.getenv("GRAILED_PASSWORD") or None,
        grailed_headless=_bool(os.getenv("GRAILED_HEADLESS"), True),
        grailed_proxy_url=os.getenv("GRAILED_PROXY_URL") or os.getenv("PROXY_URL") or None,
        goofish_proxy_url=os.getenv("GOOFISH_PROXY_URL") or os.getenv("PROXY_URL") or None,
        parser_timeout_ms=_int(os.getenv("PARSER_TIMEOUT_MS"), 30000),
        max_items_per_source=_int(os.getenv("MAX_ITEMS_PER_SOURCE"), 12),
        max_sends_per_run=_int(os.getenv("MAX_SENDS_PER_RUN"), 8),
        log_file=Path(os.getenv("LOG_FILE", "logs/app.log")),
    )

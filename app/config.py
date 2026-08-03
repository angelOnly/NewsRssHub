"""Configuration loading for the NewsRSSHub application.

The legacy ``config.yml`` is kept compatible so existing installations do not
break. Environment variables always take precedence and are the intended way
to pass credentials in Docker.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT_DIR / "data"

# Docker Compose supplies env_file itself; loading here also makes local
# ``uvicorn app.web:app`` honour a private .env file without touching config.yml.
load_dotenv(ROOT_DIR / ".env", override=False)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected a mapping in {path}")
    return loaded


def _as_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_database_path(raw_path: str, data_dir: Path) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate
    # Old versions put rss_news.db in the project root. Keep the file name but
    # move new data into a dedicated persistent directory.
    return data_dir / candidate.name


@dataclass(frozen=True)
class Settings:
    root_dir: Path
    source_dir: Path
    data_dir: Path
    database_path: Path
    request_timeout: int
    log_level: str
    rsshub_base_url: str
    rsshub_exclude_paths: tuple[str, ...]
    llm_enabled: bool
    openai_api_key: str | None
    openai_base_url: str
    openai_model_name: str
    credential_encryption_key: str | None
    timezone: str

    @property
    def profile_path(self) -> Path:
        return self.source_dir / "user_profile.yml"

    @property
    def feeds_path(self) -> Path:
        return self.source_dir / "feeds.yml"

    @property
    def taxonomy_path(self) -> Path:
        return self.source_dir / "taxonomy.yml"


def build_settings() -> Settings:
    config = _load_yaml(ROOT_DIR / "config.yml")
    app_config = config.get("app", {})
    database_config = config.get("database", {})
    rsshub_config = config.get("rsshub", {})
    llm_config = config.get("llm", {})

    source_dir = ROOT_DIR / os.getenv("APP_SOURCE_DIR", app_config.get("source_dir", "sources"))
    data_dir = Path(os.getenv("APP_DATA_DIR", str(DEFAULT_DATA_DIR)))
    if not data_dir.is_absolute():
        data_dir = ROOT_DIR / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    database_raw = os.getenv("APP_DATABASE_PATH", database_config.get("path", "rss_news.db"))
    database_path = _resolve_database_path(database_raw, data_dir)

    legacy_api_key = config.get("OPENAI_API_KEY")
    api_key = os.getenv("OPENAI_API_KEY") or legacy_api_key
    base_url = os.getenv("OPENAI_BASE_URL") or config.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    model_name = os.getenv("OPENAI_MODEL_NAME") or config.get("OPENAI_MODEL_NAME") or "gpt-4.1-mini"
    enabled_default = llm_config.get("enabled", bool(api_key))

    return Settings(
        root_dir=ROOT_DIR,
        source_dir=source_dir,
        data_dir=data_dir,
        database_path=database_path,
        request_timeout=int(os.getenv("APP_REQUEST_TIMEOUT", app_config.get("request_timeout", 30))),
        log_level=os.getenv("APP_LOG_LEVEL", app_config.get("log_level", "INFO")),
        rsshub_base_url=(os.getenv("RSSHUB_BASE_URL") or rsshub_config.get("base_url") or "https://rsshub.app").rstrip("/"),
        rsshub_exclude_paths=tuple(rsshub_config.get("exclude_paths", [])),
        llm_enabled=_as_bool(os.getenv("LLM_ENABLED"), _as_bool(enabled_default, bool(api_key))),
        openai_api_key=api_key,
        openai_base_url=base_url.rstrip("/"),
        openai_model_name=model_name,
        credential_encryption_key=os.getenv("CREDENTIAL_ENCRYPTION_KEY") or None,
        timezone=os.getenv("APP_TIMEZONE", "Asia/Shanghai"),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return build_settings()


def load_user_profile(settings: Settings | None = None) -> dict[str, Any]:
    return _load_yaml((settings or get_settings()).profile_path)


def load_taxonomy(settings: Settings | None = None) -> dict[str, Any]:
    return _load_yaml((settings or get_settings()).taxonomy_path)

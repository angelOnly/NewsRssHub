"""Configuration loading for the NewsRSSHub application.

``config.yml`` is the sole runtime configuration file for this personal
deployment, including the model connection and encryption key.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT_DIR / "data"

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

    source_dir = ROOT_DIR / str(app_config.get("source_dir", "sources"))
    data_dir = Path(str(app_config.get("data_dir", DEFAULT_DATA_DIR)))
    if not data_dir.is_absolute():
        data_dir = ROOT_DIR / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    database_raw = str(database_config.get("path", "rss_news.db"))
    database_path = _resolve_database_path(database_raw, data_dir)

    api_key = config.get("OPENAI_API_KEY") or None
    base_url = config.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    model_name = config.get("OPENAI_MODEL_NAME") or "gpt-4.1-mini"
    enabled_default = llm_config.get("enabled")
    if enabled_default is None:
        enabled_default = config.get("LLM_ENABLED")

    return Settings(
        root_dir=ROOT_DIR,
        source_dir=source_dir,
        data_dir=data_dir,
        database_path=database_path,
        request_timeout=int(app_config.get("request_timeout", 30)),
        log_level=str(app_config.get("log_level", "INFO")),
        rsshub_base_url=str(rsshub_config.get("base_url") or "https://rsshub.app").rstrip("/"),
        rsshub_exclude_paths=tuple(rsshub_config.get("exclude_paths", [])),
        llm_enabled=_as_bool(enabled_default, bool(api_key)),
        openai_api_key=api_key,
        openai_base_url=base_url.rstrip("/"),
        openai_model_name=model_name,
        credential_encryption_key=config.get("CREDENTIAL_ENCRYPTION_KEY") or None,
        timezone=str(app_config.get("timezone", "Asia/Shanghai")),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return build_settings()


def load_user_profile(settings: Settings | None = None) -> dict[str, Any]:
    return _load_yaml((settings or get_settings()).profile_path)


def load_taxonomy(settings: Settings | None = None) -> dict[str, Any]:
    return _load_yaml((settings or get_settings()).taxonomy_path)

"""NewsRSSHub 与自定义 RSSHub 容器之间的最小共享运行时文件。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterable, Mapping
from urllib.parse import urlparse

from app.config import Settings


class RssHubRuntimeFiles:
    """以原子替换方式维护仅供 RSSHub 只读挂载的运行时文件。"""

    _X_CREDENTIAL_FILENAME = "x-twitter.json"
    _RSS_FEEDS_FILENAME = "rss-feeds.json"

    def __init__(self, settings: Settings) -> None:
        self._directory = settings.data_dir / "rsshub-runtime"

    @property
    def x_credential_path(self) -> Path:
        return self._directory / self._X_CREDENTIAL_FILENAME

    @property
    def rss_feeds_path(self) -> Path:
        return self._directory / self._RSS_FEEDS_FILENAME

    def write_x_credential(self, cookies: Mapping[str, str]) -> None:
        """同步完整 X Cookie，供 RSSHub 原样建立登录会话。"""

        cookie_header = str(cookies.get("cookie_header") or "").strip()
        if not cookie_header:
            raise ValueError("X Cookie 缺少完整 Cookie 字符串，无法同步到 RSSHub。")
        self._write_private_json(
            self.x_credential_path,
            {
                "version": 2,
                "cookie_header": cookie_header,
            },
        )

    def read_x_credential(self) -> dict[str, str]:
        """读取唯一的 X Cookie 存储；调用方负责进一步校验 Cookie 字段。"""

        raw = self.x_credential_path.read_text(encoding="utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("X Cookie 共享文件不是有效 JSON。") from exc
        if not isinstance(payload, dict) or payload.get("version") != 2:
            raise ValueError("X Cookie 共享文件版本无效。")
        cookie_header = payload.get("cookie_header")
        if not isinstance(cookie_header, str) or not cookie_header.strip():
            raise ValueError("X Cookie 共享文件缺少完整 Cookie 字符串。")
        return {"cookie_header": cookie_header.strip()}

    def clear_x_credential(self) -> None:
        """删除当前 X Cookie 文件，例如候选 Cookie 验证失败且没有可恢复文件时。"""

        self.x_credential_path.unlink(missing_ok=True)

    @staticmethod
    def rss_feed_key(feed_url: str) -> str:
        """为一个已受 NewsRSSHub 管理的 RSS 地址生成稳定且不可猜测的路由键。"""

        normalized = RssHubRuntimeFiles.normalize_rss_feed_url(feed_url)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def normalize_rss_feed_url(feed_url: str) -> str:
        """仅接受绝对 HTTP(S) 地址，避免把无效地址写入 RSSHub 白名单。"""

        normalized = str(feed_url or "").strip()
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("RSS 地址必须是完整的 http:// 或 https:// 地址。")
        return normalized

    def write_rss_feed_manifest(self, feed_urls: Iterable[str]) -> dict[str, str]:
        """写入通用 RSS 白名单，RSSHub 只能读取清单中已登记的地址。"""

        feeds: dict[str, dict[str, str]] = {}
        for feed_url in feed_urls:
            normalized = self.normalize_rss_feed_url(feed_url)
            feeds[self.rss_feed_key(normalized)] = {"url": normalized}
        self._write_private_json(
            self.rss_feeds_path,
            {"version": 1, "feeds": dict(sorted(feeds.items()))},
        )
        return {key: value["url"] for key, value in feeds.items()}

    def _write_private_json(self, target: Path, payload: object) -> None:
        """同目录临时文件加原子替换，防止 RSSHub 读到半写入内容。"""

        target.parent.mkdir(parents=True, exist_ok=True)
        self._best_effort_private_mode(target.parent, 0o700)
        temporary_path: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                self._best_effort_private_mode(temporary_path, 0o600)
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, target)
            self._best_effort_private_mode(target, 0o600)
        finally:
            if temporary_path and temporary_path.exists():
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _best_effort_private_mode(path: Path, mode: int) -> None:
        """Windows 测试环境不一定支持 Unix 权限，失败时不影响原子写入。"""

        try:
            path.chmod(mode)
        except OSError:
            pass

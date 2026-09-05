"""YouTube 下载 Cookie 的运行时保存。

Cookie 只会以 yt-dlp 可读取的 Netscape 文件格式保存在数据卷中，既不进入
SQLite，也不会由状态接口或网页重新返回给浏览器。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from app.config import Settings


_COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_YOUTUBE_DOMAIN = ".youtube.com"
_HTTPONLY_PREFIX = "#HttpOnly_"
_COOKIE_BOOLEAN_VALUES = frozenset({"TRUE", "FALSE"})
_COOKIE_EXPIRES = re.compile(r"^(?:|[0-9]+(?:\.[0-9]+)?)$")
# MozillaCookieJar 会把 0 当作已过期；此处只影响本地 Cookie 文件的保留，
# 不会绕过服务端对实际会话有效期的校验。
_COOKIE_PERSISTENT_EXPIRES = "2147483647"
_AUTH_COOKIE_NAMES = frozenset(
    {
        "SID",
        "HSID",
        "SSID",
        "APISID",
        "SAPISID",
        "__Secure-1PSID",
        "__Secure-3PSID",
        "LOGIN_INFO",
    }
)


class YouTubeSessionError(RuntimeError):
    """可安全展示给用户的 Cookie 配置错误，不得包含 Cookie 内容。"""


class YouTubeCredentialMissingError(YouTubeSessionError):
    pass


class YouTubeCredentialConfigurationError(YouTubeSessionError):
    pass


@dataclass(frozen=True, slots=True)
class YouTubeCredentialStatus:
    """只返回配置状态，绝不将 Cookie 内容带出运行时文件。"""

    state: str
    message: str
    configured: bool


def parse_youtube_cookie(value: str) -> dict[str, str]:
    """解析浏览器请求头里的完整 YouTube Cookie 字符串。"""

    raw = value.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    if not raw:
        raise YouTubeCredentialMissingError("请粘贴 youtube.com 请求中的完整 Cookie 字符串。")
    if "\r" in raw or "\n" in raw:
        raise YouTubeCredentialMissingError("Cookie 格式无效，请粘贴单行的 youtube.com Cookie 字符串。")

    values: dict[str, str] = {}
    for raw_part in raw.split(";"):
        part = raw_part.strip()
        if not part:
            continue
        name, separator, cookie_value = part.partition("=")
        name = name.strip()
        cookie_value = cookie_value.strip()
        if (
            not separator
            or not _COOKIE_NAME.fullmatch(name)
            or not cookie_value
            or "\t" in cookie_value
        ):
            raise YouTubeCredentialMissingError(
                "Cookie 格式无效，请从 youtube.com 请求的 Cookie 头重新复制。"
            )
        values[name] = cookie_value

    if not _AUTH_COOKIE_NAMES.intersection(values):
        raise YouTubeCredentialMissingError(
            "完整 YouTube Cookie 中缺少登录会话字段；请从已登录的 youtube.com 请求复制完整 Cookie。"
        )
    return values


class YouTubeSessionService:
    """维护仅由 YouTube 下载器读取的 Cookie 运行时文件。"""

    _COOKIE_FILENAME = "cookies.txt"

    def __init__(self, settings: Settings) -> None:
        self._directory = settings.data_dir / "youtube-runtime"

    @property
    def cookie_file_path(self) -> Path:
        """返回 yt-dlp 专用 Cookie 文件路径，不读取其中的敏感内容。"""

        return self._directory / self._COOKIE_FILENAME

    def status(self) -> YouTubeCredentialStatus:
        """检查运行时文件是否是可供 yt-dlp 使用的完整 Cookie 文件。"""

        try:
            self._read_cookie_names()
        except YouTubeCredentialMissingError:
            return YouTubeCredentialStatus(
                "missing",
                "未保存 YouTube Cookie；公开视频下载仍可直接尝试。",
                False,
            )
        except YouTubeCredentialConfigurationError as exc:
            return YouTubeCredentialStatus("invalid", str(exc), False)
        return YouTubeCredentialStatus(
            "saved",
            "YouTube 下载 Cookie 已保存，尚未验证；请用下方视频链接进行模拟解析验证。",
            True,
        )

    def save_from_web(self, cookie_value: str) -> YouTubeCredentialStatus:
        """校验后原子写入 Cookie；格式错误时不会覆盖已有文件。"""

        cookies = parse_youtube_cookie(cookie_value)
        try:
            self._write_cookie_file(cookies)
        except OSError as exc:
            raise YouTubeCredentialConfigurationError(
                "无法写入 YouTube Cookie 运行时文件，请检查数据目录权限。"
            ) from exc
        return self.status()

    def _read_cookie_names(self) -> set[str]:
        try:
            raw = self.cookie_file_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise YouTubeCredentialMissingError("尚未保存 YouTube Cookie。") from exc
        except (OSError, UnicodeError) as exc:
            raise YouTubeCredentialConfigurationError(
                "已保存的 YouTube Cookie 文件无法读取，请重新保存。"
            ) from exc

        lines = raw.splitlines()
        if not lines or lines[0] != "# Netscape HTTP Cookie File":
            raise YouTubeCredentialConfigurationError(
                "已保存的 YouTube Cookie 文件格式无效，请重新保存。"
            )

        names: set[str] = set()
        for raw_line in lines[1:]:
            # yt-dlp 回写时会用 #HttpOnly_ 标记部分 Cookie；它不是注释，
            # 仍是 Netscape 格式中的有效 Cookie 记录。
            line = (
                raw_line[len(_HTTPONLY_PREFIX) :]
                if raw_line.startswith(_HTTPONLY_PREFIX)
                else raw_line
            )
            if not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) != 7:
                raise YouTubeCredentialConfigurationError(
                    "已保存的 YouTube Cookie 文件格式无效，请重新保存。"
                )
            domain, include_subdomains, path, secure, expires, name, cookie_value = fields
            if (
                not domain
                or include_subdomains not in _COOKIE_BOOLEAN_VALUES
                or not path.startswith("/")
                or secure not in _COOKIE_BOOLEAN_VALUES
                or not _COOKIE_EXPIRES.fullmatch(expires)
                or not _COOKIE_NAME.fullmatch(name)
            ):
                raise YouTubeCredentialConfigurationError(
                    "已保存的 YouTube Cookie 文件格式无效，请重新保存。"
                )
            if self._is_youtube_domain(domain) and cookie_value:
                names.add(name)

        if not _AUTH_COOKIE_NAMES.intersection(names):
            raise YouTubeCredentialConfigurationError(
                "已保存的 YouTube Cookie 缺少登录会话字段，请重新保存。"
            )
        return names

    def _write_cookie_file(self, cookies: dict[str, str]) -> None:
        """以 0600 临时文件和原子替换避免下载器读取到半写入内容。"""

        target = self.cookie_file_path
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
                handle.write("# Netscape HTTP Cookie File\n")
                handle.write("# 由 NewsRSSHub 生成，仅供 yt-dlp 使用。\n")
                for name, cookie_value in cookies.items():
                    handle.write(
                        f"{_YOUTUBE_DOMAIN}\tTRUE\t/\tTRUE\t{_COOKIE_PERSISTENT_EXPIRES}\t{name}\t{cookie_value}\n"
                    )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, target)
            self._best_effort_private_mode(target, 0o600)
        finally:
            if temporary_path and temporary_path.exists():
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _is_youtube_domain(domain: str) -> bool:
        """允许 yt-dlp 写回的 youtube.com 子域 Cookie，忽略其他站点记录。"""

        normalized = domain.lstrip(".").casefold()
        return normalized == "youtube.com" or normalized.endswith(".youtube.com")

    @staticmethod
    def _best_effort_private_mode(path: Path, mode: int) -> None:
        """Windows 测试环境不一定支持 Unix 权限，失败不影响原子写入。"""

        try:
            path.chmod(mode)
        except OSError:
            pass

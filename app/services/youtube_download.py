"""个人部署使用的单条 YouTube 视频下载服务。"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit
from uuid import uuid4

from app.config import Settings


logger = logging.getLogger(__name__)

_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
}


class YouTubeDownloadError(RuntimeError):
    """下载请求可安全返回给调用方的错误。"""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class DownloadedYouTubeVideo:
    """一个已完成下载、尚待 HTTP 响应结束后清理的文件。"""

    path: Path
    task_directory: Path
    video_id: str
    media_type: str
    download_name: str


def normalize_youtube_video_url(raw_url: str) -> tuple[str, str]:
    """只接受单条公开视频地址，并移除播放列表等无关参数。"""

    parsed = urlsplit(raw_url.strip())
    host = (parsed.hostname or "").casefold()
    try:
        port = parsed.port
    except ValueError as exc:
        raise YouTubeDownloadError("YouTube 链接端口无效。", status_code=422) from exc

    if (
        parsed.scheme != "https"
        or host not in _YOUTUBE_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise YouTubeDownloadError(
            "只支持不含账号信息的标准 HTTPS YouTube 单视频链接。",
            status_code=422,
        )

    path = parsed.path.rstrip("/")
    video_id = ""
    if host == "youtu.be":
        video_id = path.lstrip("/").split("/", 1)[0]
    elif path == "/watch":
        video_id = (parse_qs(parsed.query).get("v") or [""])[0]
    else:
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 2 and parts[0] in {"shorts", "live", "embed"}:
            video_id = parts[1]

    if not _VIDEO_ID_RE.fullmatch(video_id):
        raise YouTubeDownloadError("链接中缺少有效的视频 ID。", status_code=422)

    # 只把固定的单视频地址交给下载器，避免 URL 中的播放列表扩大下载范围。
    return f"https://www.youtube.com/watch?v={quote(video_id)}", video_id


class YouTubeDownloadService:
    """调用 yt-dlp 和 FFmpeg 下载一条视频，供个人 Web 服务直接返回文件。"""

    def __init__(self, settings: Settings, *, cookie_file_path: Path | None = None) -> None:
        self._root = settings.data_dir / "youtube-downloads"
        self._root.mkdir(parents=True, exist_ok=True)
        self._timeout_seconds = settings.youtube_download_timeout_seconds
        self._cookie_file_path = cookie_file_path or settings.data_dir / "youtube-runtime" / "cookies.txt"

    def download(self, raw_url: str) -> DownloadedYouTubeVideo:
        """下载一条视频；失败时立即删除本次产生的临时文件。"""

        canonical_url, video_id = normalize_youtube_video_url(raw_url)
        task_directory = self._root / uuid4().hex
        task_directory.mkdir()
        # 每次请求重新判断，设置页刚保存的 Cookie 无需重启 Web 服务即可生效。
        use_cookie = self._has_cookie_file()
        cookie_copy: Path | None = None
        try:
            if use_cookie:
                # yt-dlp 可能在退出时回写 --cookies 指向的文件；传入本任务副本，
                # 避免它改写设置页保存的原始 Cookie。
                cookie_copy = self._copy_cookie_for_process(task_directory)
            completed = subprocess.run(
                self._command(canonical_url, task_directory, cookie_file_path=cookie_copy),
                cwd=task_directory,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout_seconds,
            )
        except YouTubeDownloadError:
            self.remove_task(task_directory)
            raise
        except subprocess.TimeoutExpired as exc:
            self.remove_task(task_directory)
            raise YouTubeDownloadError("视频下载超时，请稍后重试。", status_code=504) from exc
        except OSError as exc:
            self.remove_task(task_directory)
            logger.exception("无法启动 YouTube 下载器")
            raise YouTubeDownloadError("下载服务暂不可用。", status_code=503) from exc
        finally:
            self._remove_cookie_copy(cookie_copy)

        if completed.returncode != 0:
            self.remove_task(task_directory)
            logger.warning(
                "yt-dlp 下载失败：video_id=%s，退出码=%s",
                video_id,
                completed.returncode,
            )
            raise self._download_failure(completed.stderr, used_cookie=use_cookie)

        candidates = self._final_files(completed.stdout, task_directory)
        if len(candidates) != 1:
            self.remove_task(task_directory)
            logger.warning(
                "yt-dlp 未返回唯一最终文件：video_id=%s，数量=%s",
                video_id,
                len(candidates),
            )
            raise YouTubeDownloadError("下载完成但未找到最终视频文件。", status_code=502)

        path = next(iter(candidates))
        extension = path.suffix.lower()
        return DownloadedYouTubeVideo(
            path=path,
            task_directory=task_directory,
            video_id=video_id,
            media_type=_MEDIA_TYPES.get(extension, "application/octet-stream"),
            download_name=f"{video_id}{extension}",
        )

    def validate_saved_cookie(self, raw_url: str) -> str:
        """用指定视频做模拟解析，验证当前 Cookie 而不写入媒体文件。"""

        canonical_url, video_id = normalize_youtube_video_url(raw_url)
        if not self._has_cookie_file():
            raise YouTubeDownloadError(
                "尚未保存 YouTube Cookie，请先保存后再验证。",
                status_code=422,
            )
        task_directory = self._root / f"cookie-check-{uuid4().hex}"
        task_directory.mkdir()
        cookie_copy: Path | None = None
        try:
            cookie_copy = self._copy_cookie_for_process(task_directory)
            completed = subprocess.run(
                self._validation_command(canonical_url, cookie_file_path=cookie_copy),
                cwd=task_directory,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                # 验证不下载媒体，无需沿用完整下载的一小时上限。
                timeout=min(self._timeout_seconds, 90),
            )
        except YouTubeDownloadError:
            raise
        except subprocess.TimeoutExpired as exc:
            raise YouTubeDownloadError("YouTube Cookie 验证超时，请稍后重试。", status_code=504) from exc
        except OSError as exc:
            logger.exception("无法启动 YouTube Cookie 验证器")
            raise YouTubeDownloadError("下载服务暂不可用。", status_code=503) from exc
        finally:
            self._remove_cookie_copy(cookie_copy)
            self.remove_task(task_directory)

        if completed.returncode != 0:
            logger.warning(
                "yt-dlp Cookie 验证失败：video_id=%s，退出码=%s",
                video_id,
                completed.returncode,
            )
            raise self._download_failure(completed.stderr, used_cookie=True)
        return video_id

    def remove_task(self, task_directory: Path) -> None:
        """仅删除下载根目录下的单个任务目录，避免清理越界。"""

        root = self._root.resolve()
        candidate = task_directory.resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            logger.error("拒绝清理下载根目录外的路径：%s", candidate)
            return
        if candidate == root:
            logger.error("拒绝清理下载根目录本身。")
            return
        if candidate.exists():
            shutil.rmtree(candidate, ignore_errors=True)

    def _command(
        self,
        canonical_url: str,
        task_directory: Path,
        *,
        cookie_file_path: Path | None = None,
    ) -> list[str]:
        """固定下载预设；调用方不能注入 yt-dlp 参数或输出模板。"""

        command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--quiet",
            "--no-warnings",
            "--no-playlist",
            "--socket-timeout",
            "30",
            "--format",
            "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
            "--merge-output-format",
            "mp4",
            "--paths",
            str(task_directory),
            "--output",
            "%(id)s.%(ext)s",
            # 读取合并后的机器路径，而不是从普通日志猜测文件名。
            "--print",
            "after_move:filepath",
        ]
        if cookie_file_path is not None:
            # 只传任务内的临时 Cookie 文件路径，绝不把 Cookie 值拼入参数或日志。
            command.extend(["--cookies", str(cookie_file_path)])
        command.append(canonical_url)
        return command

    def _validation_command(self, canonical_url: str, *, cookie_file_path: Path) -> list[str]:
        """验证命令固定为模拟解析，既不生成临时媒体文件也不调用 FFmpeg。"""

        return [
            sys.executable,
            "-m",
            "yt_dlp",
            "--quiet",
            "--no-warnings",
            "--no-playlist",
            "--simulate",
            "--socket-timeout",
            "30",
            # 与下载相同，传入随任务删除的临时副本。
            "--cookies",
            str(cookie_file_path),
            canonical_url,
        ]

    def _copy_cookie_for_process(self, task_directory: Path) -> Path:
        """复制运行时 Cookie，避免 yt-dlp 回写长期保存的配置文件。"""

        cookie_copy = task_directory / ".youtube-cookies.txt"
        try:
            shutil.copyfile(self._cookie_file_path, cookie_copy)
            try:
                cookie_copy.chmod(0o600)
            except OSError:
                # Windows 测试环境不一定支持 Unix 权限；复制本身仍可安全继续。
                pass
        except OSError as exc:
            try:
                cookie_copy.unlink(missing_ok=True)
            except OSError:
                pass
            raise YouTubeDownloadError(
                "已保存的 YouTube Cookie 无法读取，请重新保存后重试。",
                status_code=422,
            ) from exc
        return cookie_copy

    @staticmethod
    def _remove_cookie_copy(cookie_copy: Path | None) -> None:
        """子进程结束即删除任务内的 Cookie 副本，缩短敏感数据保留时间。"""

        if cookie_copy is None:
            return
        try:
            cookie_copy.unlink(missing_ok=True)
        except OSError:
            logger.warning("临时 YouTube Cookie 文件清理失败。")

    def _has_cookie_file(self) -> bool:
        """Cookie 文件由设置页原子替换，下载时只检查其是否可读取。"""

        try:
            return self._cookie_file_path.is_file()
        except OSError:
            return False

    @staticmethod
    def _final_files(output: str, task_directory: Path) -> set[Path]:
        """只接受位于本任务目录中的实际最终文件。"""

        root = task_directory.resolve()
        files: set[Path] = set()
        for line in output.splitlines():
            text = line.strip()
            if not text:
                continue
            reported = Path(text)
            candidate = (root / reported).resolve() if not reported.is_absolute() else reported.resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            if candidate.is_file():
                files.add(candidate)
        return files

    @staticmethod
    def _download_failure(stderr: str, *, used_cookie: bool = False) -> YouTubeDownloadError:
        """把易变的上游错误收敛为稳定、无敏感信息的接口错误。"""

        message = stderr.casefold()
        if "no module named" in message and "yt_dlp" in message:
            return YouTubeDownloadError("下载服务依赖未安装。", status_code=503)
        if "ffmpeg" in message and ("not found" in message or "not installed" in message):
            return YouTubeDownloadError("下载服务缺少 FFmpeg。", status_code=503)
        if any(
            marker in message
            for marker in (
                "sign in to confirm you're not a bot",
                "sign in to confirm you’re not a bot",
                "confirm you're not a bot",
                "confirm you’re not a bot",
            )
        ):
            if used_cookie:
                return YouTubeDownloadError(
                    "当前 YouTube Cookie 已失效或未通过验证，请在“设置与连接”更新后重试。",
                    status_code=422,
                )
            return YouTubeDownloadError(
                "YouTube 要求登录验证。请前往“设置与连接”保存 YouTube Cookie 后重试。",
                status_code=422,
            )
        if any(
            marker in message
            for marker in (
                "private video",
                "video unavailable",
                "not available",
                "sign in",
                "members-only",
            )
        ):
            return YouTubeDownloadError(
                "视频不可访问或当前无法下载，请确认它公开且可用。",
                status_code=422,
            )
        return YouTubeDownloadError("视频下载失败，请稍后重试。", status_code=502)

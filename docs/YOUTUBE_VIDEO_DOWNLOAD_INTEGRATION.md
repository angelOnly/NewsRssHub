# YouTube 视频下载接入指南

> 本文面向需要在其他项目中加入“下载本人拥有、获授权或可合法保存的 YouTube 视频”能力的开发者。推荐采用 yt-dlp、FFmpeg 和异步任务 Worker 的组合。
>
> 请先确认视频版权、授权范围、平台条款和当地法律。不要用本方案绕过 DRM、付费墙、访问控制或地域限制，也不要下载无权保存的内容。

## 1. 本项目现状：自动订阅与手动下载是两条链路

NewsRSSHub 的自动抓取仍然只负责订阅频道更新和生成远端预览，不会自动下载频道中的视频文件。现在额外提供了一个面向个人部署的手动下载接口：POST /api/youtube/download。

现有 YouTube 调用链：

~~~text
用户输入 @频道 / /channel/UC… / 频道 ID
  -> YouTubeSourcePlugin 规范化并解析为稳定频道 ID
  -> RSSHub /youtube/channel/{频道 ID}
  -> RssSourcePlugin.fetch 获取 RSS/Atom
  -> parse_feed_payload 提取条目和媒体链接
  -> _youtube_embed_url 生成 youtube-nocookie 嵌入地址
  -> items.media_json 仅保存远端 URL / 嵌入地址
~~~

对应实现位置：

- “app/plugins/youtube.py”：频道地址处理和 RSSHub 路由生成；
- “app/plugins/rss.py”：RSS 解析、媒体链接提取和 YouTube 嵌入地址生成；
- “app/domain/models.py”：FeedItem.media 的注释明确说明抓取时不下载媒体文件；
- “docs/INTEGRATION_2026-08-04_FETCH_MEDIA_FAVORITES.md”：说明媒体只保存远端 URL，不下载或代理文件。

所以，RSSHub 连接器适合发现频道更新和展示视频嵌入，并不等于取得本地 MP4。新的下载接口不会改动 collector、processor、RSS 解析或摘要链路；它只在调用方显式提交一条视频 URL 时运行 yt-dlp。

### 1.1 当前项目可直接调用的接口

~~~text
POST /api/youtube/download
Content-Type: application/json

{
  "url": "https://www.youtube.com/watch?v=视频ID"
}
~~~

成功时直接返回 MP4 文件，响应完成后会删除本次临时文件。它只接受一条视频，Shorts、Live、Embed 和 youtu.be 短链会被规范化为标准视频地址；播放列表不会被下载。

Linux、macOS 或 Git Bash 调用示例：

~~~bash
curl -X POST "https://你的域名/api/youtube/download" \
  -H "Content-Type: application/json" \
  --data '{"url":"https://www.youtube.com/watch?v=视频ID"}' \
  --output video.mp4
~~~

Windows PowerShell 调用示例：

~~~powershell
$body = @{ url = "https://www.youtube.com/watch?v=视频ID" } | ConvertTo-Json -Compress
Invoke-WebRequest -Method Post -Uri "https://你的域名/api/youtube/download" -ContentType "application/json" -Body $body -OutFile ".\video.mp4"
~~~

这是用户明确选择的无额外鉴权、同步下载实现：请求会占用一个 Web Worker，适合个人低并发使用，不适合开放给不受信任的公网用户。YouTube 的 YOUTUBE_KEY 仍由 RSSHub 的频道抓取使用；yt-dlp 下载接口不读取它。

### 1.2 遇到“YouTube 要求登录验证”时如何配置 Cookie

Google 的 YouTube Data API Key 只能读取元数据和频道信息，不能替 yt-dlp 取得视频媒体流。某些 IP、视频或请求频率下，YouTube 会要求浏览器登录会话来完成机器人验证；这时应由部署者自己在网页中保存 Cookie，而不是把 Cookie 发给接口调用方或贴到聊天中。

在 NewsRSSHub 的“设置与连接”→“平台连接设置”中，会有单独的“YOUTUBE DOWNLOAD / YouTube 下载”卡片。它与下方“公开可用”的 YouTube 频道抓取卡片是两件事：

- YouTube 频道/RSSHub 自动抓取仍使用公开 RSS 路由，不需要 Cookie；
- 该 Cookie 只会在 `POST /api/youtube/download` 下载单条视频时传给 yt-dlp；
- 保存时会立即进行一次模拟解析：验证成功才会启用新的 Cookie；Cookie 到期后重新保存并验证即可；
- 状态会明确显示“Cookie 可用”或“Cookie 验证失败”。一次验证成功代表当时的服务器、Cookie 和指定视频可以完成解析，并不保证所有受限视频永久可下载。

获取方式如下。请只在自己的已登录浏览器和自己的 NewsRSSHub 实例中操作：

1. 在浏览器中登录 [YouTube](https://www.youtube.com/)；
2. 按 `F12` 打开开发者工具，进入 `Network`（网络）面板；
3. 刷新一个 `youtube.com` 页面，选择列表中一个域名为 `www.youtube.com` 或 `youtube.com` 的请求；
4. 在 `Request Headers`（请求头）中找到 `Cookie`，复制其完整的单行值；若复制内容包含 `Cookie:` 前缀，也可以一并粘贴；
5. 在“设置与连接”中粘贴 YouTube Cookie，并在同一表单填入一条公开单视频 URL；
6. 点击“保存并验证 YouTube Cookie”；服务会先让 yt-dlp 模拟解析该视频，不下载媒体文件；
7. 只有看到“Cookie 可用”及成功提示时，新的 Cookie 才会保存并供下载接口使用。无需在请求体或请求头额外传 Cookie。

不要复制响应里的 `Set-Cookie`，不要使用只包含 `PREF`、`YSC` 等访客字段的不完整 Cookie，也不要在聊天、工单、截图或代码仓库中泄露完整 Cookie。服务会要求 Cookie 至少包含一个登录会话字段，格式不正确时不会覆盖原有文件。

保存过程会先把候选 Cookie 转换成 yt-dlp 所需的 Netscape 格式并放入私有临时文件，再执行固定的 `yt-dlp --simulate --cookies ...` 命令。它会实际请求指定视频但不会下载媒体、不会调用 FFmpeg；验证成功后，候选文件才会原子替换数据卷 `youtube-runtime/cookies.txt` 中的当前 Cookie。验证失败的候选文件会立即删除；如果之前已有验证通过的 Cookie，它会继续保留。Cookie 文件和验证结果文件均尽量使用目录 `0700`、文件 `0600` 权限，不写入 SQLite、不进入应用日志、不在网页回显。

每次验证或下载都会先在本任务目录创建 Cookie 的私有临时副本交给 yt-dlp，子进程退出后立即删除。这样即使 yt-dlp 回写它的 Cookie Jar，也不会破坏设置页保存的原始文件。验证结果只反映当时的会话、服务器 IP 和该视频；Cookie 随时可能过期，不能把一次成功当成永久有效。

## 2. 推荐架构

### 2.1 组件职责

| 组件 | 职责 | 不应承担的职责 |
| --- | --- | --- |
| yt-dlp | 解析视频页、选择格式、下载音视频流 | Web 鉴权、数据库状态管理、长期存储策略 |
| FFmpeg | 合并分离的音视频流、转封装、音频提取 | 解析 YouTube 页面 |
| API 服务 | 校验 URL、创建任务、授权访问任务状态 | 在 HTTP 请求线程中执行长时间下载 |
| 队列 / Worker | 执行下载、更新进度、处理超时和清理 | 直接暴露给公网 |
| 对象存储或私有磁盘 | 保存最终文件，按保留策略清理 | 作为公开、永久的文件索引 |

yt-dlp 是适合作为工程依赖的下载器。它既可以经命令行调用，也能作为 Python 库使用。服务端优先推荐让 Worker 通过 subprocess 调用固定版本的 yt-dlp，这样升级、故障隔离和进程取消更可控；小型本地工具才更适合直接调用 Python API。

高画质视频常将视频流和音频流分开提供。FFmpeg 缺失时，yt-dlp 只能退回单个渐进式文件，或无法完成合并，因此生产环境应将 FFmpeg 视为必需依赖。

### 2.2 按场景选择实现

| 场景 | 推荐方式 | 原因 |
| --- | --- | --- |
| 本地脚本、内部运维工具 | 固定命令行或 Python API | 接入快，运行者就是文件所有者 |
| Web 产品、多人系统、自动化平台 | API 创建任务 + 队列 + 独立 Worker | 避免请求超时，便于配额、权限、清理和审计 |
| 当前 NewsRSSHub 个人实例 | 同步 POST 接口直接返回文件 | 改动最小，不增加额外凭证、队列或持久化任务表 |

不要把用户提交的 URL、格式选择器或输出模板原样拼进 shell 字符串。用户只应提交 URL 和预定义下载预设，例如 video_mp4、best_available、audio_mp3。

## 3. 安装与健康检查

### 3.1 Python 环境

在项目虚拟环境中安装：

~~~powershell
python -m pip install --upgrade yt-dlp
python -m yt_dlp --version
ffmpeg -version
~~~

使用 “python -m yt_dlp” 比依赖 PATH 中是否存在 yt-dlp 可执行文件更稳定。FFmpeg 需要单独安装，并确保 Worker 的 PATH 可见。

正式环境不要无限期跟随最新版。应在测试环境验证一个 yt-dlp 版本后，将它写入锁文件或镜像构建参数；YouTube 页面或接口变化导致下载失败时，再按“升级 → 回归测试 → 发布”更新。

### 3.2 Docker 镜像示例

下面的 Dockerfile 让构建时显式传入已验证版本，避免部署时悄悄升级：

~~~dockerfile
FROM python:3.12-slim

ARG YTDLP_VERSION

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN test -n "$YTDLP_VERSION" \
    && python -m pip install --no-cache-dir "yt-dlp==$YTDLP_VERSION"

# 下载 Worker 只需要一个可写的任务目录，不要以 root 身份运行。
RUN useradd --create-home --uid 10001 downloader \
    && mkdir -p /var/lib/youtube-downloads \
    && chown -R downloader:downloader /var/lib/youtube-downloads

USER downloader
WORKDIR /app
~~~

构建示例：

~~~powershell
docker build --build-arg YTDLP_VERSION=已验证的版本号 -t my-youtube-worker .
~~~

下载目录应由 Worker 私有挂载，或改为使用对象存储凭据。不要把它直接暴露为 Web 服务器的静态公开目录。

### 3.3 无下载健康检查

部署后，先对一条团队拥有或明确获授权的视频进行模拟解析：

~~~powershell
python -m yt_dlp --simulate --no-playlist "https://www.youtube.com/watch?v=视频ID"
~~~

这会验证页面解析和网络连通性，但不会写入媒体文件。再执行一次真实的小文件下载，确认 FFmpeg 能合并音视频流，且任务目录权限正确。

## 4. 命令行速查

以下 URL 只能替换为有权下载的视频地址。输出路径使用视频 ID，避免标题中的特殊字符造成跨平台问题。

### 4.1 兼容性优先的 MP4

~~~powershell
python -m yt_dlp --no-playlist --format "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b" --merge-output-format mp4 --output "downloads/%(id)s.%(ext)s" "https://www.youtube.com/watch?v=视频ID"
~~~

格式含义：

- “bv*[ext=mp4]+ba[ext=m4a]”：优先选择可直接合并为 MP4 的独立视频和音频；
- “/b[ext=mp4]/b”：前一种组合不存在时，退回单文件 MP4，再退回最佳单文件；
- “--no-playlist”：即使 URL 带有播放列表参数，也只处理一个视频；
- “--merge-output-format mp4”：要求 FFmpeg 以 MP4 容器输出。

### 4.2 画质优先

~~~powershell
python -m yt_dlp --no-playlist --format "bv*+ba/b" --merge-output-format mkv --output "downloads/%(id)s.%(ext)s" "https://www.youtube.com/watch?v=视频ID"
~~~

此预设倾向使用站点提供的最佳音视频流。不同编码组合未必适合 MP4，因此使用 MKV 作为合并容器更稳妥。若产品承诺始终 MP4，应使用上一节的兼容性预设，或另行设计受控转码流程并评估 CPU 成本。

### 4.3 仅音频

~~~powershell
python -m yt_dlp --no-playlist --extract-audio --audio-format mp3 --audio-quality 0 --output "downloads/%(id)s.%(ext)s" "https://www.youtube.com/watch?v=视频ID"
~~~

音频转换同样依赖 FFmpeg。“--audio-quality 0” 是高质量 VBR 预设，不代表源音频会凭空变得更高保真。

### 4.4 让程序可靠取得最终文件路径

不要从人类可读日志中猜测输出文件名。当前版本的 yt-dlp 可打印移动或合并后的最终路径：

~~~powershell
python -m yt_dlp --quiet --no-playlist --format "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b" --merge-output-format mp4 --paths "D:\youtube-jobs\任务ID" --output "%(id)s.%(ext)s" --print "after_move:filepath" "https://www.youtube.com/watch?v=视频ID"
~~~

Worker 必须将工具打印的路径再次校验为“存在、是普通文件、仍位于当前任务目录下”，不能直接把它拼成公开下载 URL。

## 5. 输入校验：只接受一条 YouTube 视频 URL

下载器是高权限网络和磁盘操作，应在进入 Worker 前严格约束输入。最低要求：

1. 只接受 HTTPS；
2. 只接受 youtube.com、www.youtube.com、m.youtube.com、music.youtube.com、youtu.be；
3. 只接受 /watch?v=…、/shorts/…、/live/…、/embed/… 或短链；
4. 拒绝账号密码、file:、本地 IP、第三方跳转页和播放列表作为入口；
5. 规范化为不带 list、si 等多余参数的标准视频 URL。

下面函数可直接放入 Python 项目。它使用 hostname，而不是未经处理的 netloc，可避免 “youtube.com@恶意域名” 一类误判。

~~~python
from __future__ import annotations

import re
from urllib.parse import parse_qs, quote, urlsplit


_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class InvalidYouTubeUrl(ValueError):
    """用户提交的地址不是允许下载的单条 YouTube 视频链接。"""


def normalize_youtube_video_url(raw_url: str) -> tuple[str, str]:
    """校验 URL，并返回规范化 URL 与视频 ID。"""

    parsed = urlsplit(raw_url.strip())
    host = (parsed.hostname or "").casefold()
    if (
        parsed.scheme != "https"
        or host not in _YOUTUBE_HOSTS
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise InvalidYouTubeUrl("只支持不含账号信息的 HTTPS YouTube 视频链接。")

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
        raise InvalidYouTubeUrl("链接中缺少有效的视频 ID。")

    # 只将规范化后的 URL 交给下载器，避免播放列表和无关参数影响任务范围。
    return f"https://www.youtube.com/watch?v={quote(video_id)}", video_id
~~~

这不是版权或权限校验。业务层仍应要求用户确认其拥有下载权限，并在多人系统中将任务关联到创建者。

## 6. 下载 Worker 的最小实现

### 6.1 目录模型

每个任务使用独立目录，避免并发任务互相覆盖：

~~~text
/var/lib/youtube-downloads/
  01J...任务ID/
    原始下载与合并中的临时文件
    VIDEO_ID.mp4
    manifest.json
~~~

任务成功后可以将最终文件转移到对象存储或受控归档目录；失败或过期任务则由清理器删除整个任务目录。对外只暴露任务 ID 和经鉴权的文件接口，绝不暴露真实路径。

### 6.2 安全调用 yt-dlp

下面是适合放入 Worker 的最小示例。它故意不接收用户自定义参数，也不使用 shell=True。

~~~python
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


class YouTubeDownloadFailed(RuntimeError):
    """yt-dlp 未能产生经过校验的最终文件。"""


@dataclass(frozen=True, slots=True)
class DownloadedArtifact:
    path: Path


def _is_inside(path: Path, root: Path) -> bool:
    """确认工具报告的文件没有逃出当前任务目录。"""

    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def download_mp4(
    *,
    canonical_url: str,
    task_directory: Path,
    timeout_seconds: int = 60 * 60,
) -> DownloadedArtifact:
    """在单独任务目录内下载一条视频，并返回最终媒体文件。"""

    root = task_directory.resolve()
    root.mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--quiet",
        "--no-warnings",
        "--no-playlist",
        "--format",
        "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
        "--merge-output-format",
        "mp4",
        "--paths",
        str(root),
        "--output",
        "%(id)s.%(ext)s",
        # 让 Worker 读取机器可用的最终路径，而不是解析普通日志。
        "--print",
        "after_move:filepath",
        canonical_url,
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise YouTubeDownloadFailed("下载任务超时。") from exc

    if completed.returncode != 0:
        # stderr 只写入受限诊断日志；返回给用户的错误需经过分类和脱敏。
        raise YouTubeDownloadFailed("下载器执行失败。")

    candidates: set[Path] = set()
    for line in completed.stdout.splitlines():
        reported_text = line.strip()
        if not reported_text:
            continue
        reported = Path(reported_text)
        candidate = (root / reported).resolve() if not reported.is_absolute() else reported.resolve()
        if _is_inside(candidate, root) and candidate.is_file():
            candidates.add(candidate)

    if len(candidates) != 1:
        raise YouTubeDownloadFailed("未找到唯一的最终媒体文件。")
    return DownloadedArtifact(path=next(iter(candidates)))
~~~

生产化时仍要补上：

- 超时后终止整个进程组，避免 FFmpeg 子进程残留；
- 任务取消标记、进度回调和重试退避；
- 最大文件大小、最大时长、最大并发数和可用磁盘空间检查；
- 完整 stderr 只进入受限日志，不写进面向用户的 API 响应；
- 任务结束时生成包含视频 ID、输出大小、哈希、下载器版本和时间的 manifest.json。

### 6.3 下载前读取元数据并实施配额

下载前可以以 download=False 调用 yt-dlp Python API，读取时长、标题、频道、可用格式和估算文件大小。随后检查业务限制，例如：

| 限制 | 建议做法 |
| --- | --- |
| 最长时长 | 拒绝超过产品允许上限的视频；未知时长按更严格策略处理 |
| 最大文件 | 根据目标格式的 filesize 或 filesize_approx 预估，下载过程中仍需监控磁盘 |
| 每用户并发 | 同一用户只允许有限的排队或运行中任务 |
| 每日配额 | 按文件字节数、下载次数或时长累计 |
| 磁盘余量 | Worker 开始前保留安全余量，避免写满宿主机 |

元数据只是下载前快照，源站格式和大小仍可能变化，所以这些检查只能降低风险，不能替代运行时配额。

## 7. 面向多人系统的 Web API 与异步任务设计

本节是其他项目需要多人、长任务或高并发时的推荐方案。当前 NewsRSSHub 按个人使用需求采用第 1.1 节的同步接口，不创建任务 ID，也不要求第二个 API Key。

### 7.1 建议 API

~~~text
POST /v1/youtube-downloads
  请求：{ "url": "...", "preset": "video_mp4" }
  返回：202 { "id": "任务ID", "status": "queued" }

GET /v1/youtube-downloads/{任务ID}
  返回：任务状态、脱敏元数据、进度、失败码、过期时间

GET /v1/youtube-downloads/{任务ID}/file
  返回：仅创建者或被授权者可访问的文件流 / 短期签名 URL

DELETE /v1/youtube-downloads/{任务ID}
  作用：请求取消；已完成文件按策略删除或立即撤销访问
~~~

POST 不应等到文件下载结束。文件大小、网络状况和转封装时间不可预测，同步等待会占住 Web Worker，容易触发反向代理超时。

### 7.2 状态机

~~~text
queued -> inspecting -> downloading -> processing -> succeeded
                  |             |              |
                  +-----------> failed <-------+

queued / inspecting / downloading / processing -> canceled
succeeded -> expired
~~~

建议持久化以下字段：

| 字段 | 用途 |
| --- | --- |
| id | 不可预测的任务 ID |
| created_by | 权限和配额归属 |
| canonical_url、video_id | 可追溯输入；日志中不要保存敏感查询参数 |
| preset | 只允许服务端定义的预设名 |
| status、progress_percent、stage | 前端轮询与运维诊断 |
| title、channel_name、duration_seconds | 经长度限制后的展示元数据 |
| artifact_key、size_bytes、sha256 | 文件定位与完整性校验 |
| error_code、error_detail_safe | 面向用户的稳定错误信息 |
| created_at、finished_at、expires_at | 清理和审计 |

### 7.3 FastAPI 创建任务示例

下面端点只负责校验和入队。enqueue_download 应由项目已有队列实现；不要在这里直接调用下载函数。

~~~python
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, HttpUrl


app = FastAPI()


class CreateDownloadRequest(BaseModel):
    url: HttpUrl
    preset: Literal["video_mp4", "best_available", "audio_mp3"] = "video_mp4"


@app.post("/v1/youtube-downloads", status_code=status.HTTP_202_ACCEPTED)
def create_youtube_download(
    body: CreateDownloadRequest,
    current_user=Depends(require_current_user),
):
    try:
        canonical_url, video_id = normalize_youtube_video_url(str(body.url))
    except InvalidYouTubeUrl as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # 入队前检查账户配额；队列只接收规范化 URL 和固定预设。
    ensure_download_quota(current_user.id, body.preset)
    job = enqueue_download(
        user_id=current_user.id,
        canonical_url=canonical_url,
        video_id=video_id,
        preset=body.preset,
    )
    return {"id": job.id, "status": job.status}
~~~

示例里的 require_current_user、ensure_download_quota、enqueue_download 是宿主项目应实现的接口。匿名公开下载接口会被迅速滥用，不建议提供。

## 8. 预设、格式和字幕策略

把格式选择集中在服务端，而不是让客户端传 yt-dlp 的 -f 表达式：

| 预设名 | 格式选择 | 输出 | 适用场景 |
| --- | --- | --- | --- |
| video_mp4 | bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b | MP4 | 浏览器、手机、业务系统兼容性优先 |
| best_available | bv*+ba/b | MKV 或源容器 | 归档、后期制作，画质优先 |
| audio_mp3 | 最佳音频 + FFmpeg 提取 | MP3 | 已获授权的音频处理 |

字幕、缩略图、描述和章节应作为独立、可选产物。它们会增加存储和清理复杂度，建议只有明确产品需求时才启用：

~~~powershell
python -m yt_dlp --write-subs --write-auto-subs --sub-langs "zh-Hans,zh-Hant,en.*" --convert-subs srt --write-thumbnail --no-playlist "https://www.youtube.com/watch?v=视频ID"
~~~

自动字幕可能不准确，也不等同于视频作者提供的字幕。产品应在元数据中区分人工字幕和自动字幕，并遵守相应版权与隐私要求。

## 9. 登录、Cookie 与受限视频

公开视频通常不需要登录。需要登录才能观看的内容，先判断用户是否确实拥有访问权，以及产品是否允许由服务端代表用户处理该内容。

如业务经审查后必须使用授权账户：

1. 使用单独的受控服务账户或经过用户明确授权的凭据；
2. 以密钥管理服务或只读私有挂载保存，不写入普通数据库字段、日志、镜像、仓库或报错响应；
3. 设置最小权限、轮换和失效流程；
4. 仅在 Worker 容器中读取，Web API 不返回或展示凭据；
5. 访问失败时返回“需要重新授权”等稳定错误，不回显 Cookie 内容或命令行参数。

不要以 Cookie 或代理配置绕过年龄、付费、地域、DRM 或其他访问限制。DRM 加密内容不应列为此下载服务的支持范围。

## 10. 安全、可靠性与运维

### 10.1 必做安全项

- API 必须鉴权；文件下载也必须再次鉴权，不能因为知道任务 ID 就可访问。
- URL 使用前述白名单和规范化逻辑；拒绝任意 URL、任意命令参数和任意输出模板。
- 使用参数数组调用 subprocess，绝不使用 shell=True。
- 下载 Worker 与 Web 进程分离，以非 root 用户运行，只有任务目录可写。
- 为 Worker 设置 CPU、内存、进程数、磁盘和并发限制；下载和转码都属于资源密集操作。
- 临时文件与最终文件使用随机任务目录；文件名使用视频 ID，不信任标题。
- 完成后计算哈希；文件服务根据数据库中的产物记录读取，不接受客户端传入路径。
- 不记录授权头、Cookie、完整敏感 URL 查询参数或原始工具环境变量。
- 定期清理失败任务目录和过期成品，并验证清理范围只位于下载根目录内。

### 10.2 网络隔离

下载器会访问 YouTube 页面和媒体 CDN。若部署环境支持网络隔离，应让 Worker 只拥有必要的出网能力，不能访问内部管理网、数据库管理端口或云元数据服务。单靠 URL 白名单不足以代替容器隔离，因为媒体地址会由下载器解析后访问。

在多租户环境中，建议将 Worker 放在独立容器或独立节点，并限制：

- 同时运行的下载数；
- 单任务最长运行时间；
- 单任务允许写入的最大字节数；
- 成品最长保留时间；
- 每个账号的日配额和失败重试次数。

### 10.3 错误分类

不要把 yt-dlp 的原始英文输出直接展示给用户。可以维护以下业务错误码：

| 错误码 | 常见原因 | 用户提示 | 是否自动重试 |
| --- | --- | --- | --- |
| invalid_url | 非法 URL、非单视频 URL | 链接格式不支持 | 否 |
| not_available | 视频删除、私有或不可用 | 视频当前不可下载 | 否 |
| authorization_required | 合法访问需要重新授权 | 需要重新授权后再试 | 否 |
| format_unavailable | 所选预设没有可用格式 | 当前没有可用的目标格式 | 可换预设后重试 |
| ffmpeg_unavailable | 容器中未安装或找不到 FFmpeg | 服务配置异常 | 否，通知运维 |
| quota_exceeded | 用户、磁盘或任务限制触发 | 已达到下载限制 | 否 |
| upstream_temporary | 网络、限流或上游短暂故障 | 服务暂时不可用，请稍后重试 | 有退避地重试 |
| timeout | 下载或处理超过上限 | 任务超时 | 视业务决定 |
| internal_error | 未归类故障 | 下载失败 | 不盲目重试 |

错误分类可参考退出码和受限日志，但不要依赖某一句错误文案做唯一判断；yt-dlp 升级后文本可能变化。

### 10.4 监控指标

至少记录以下脱敏指标：

- 按状态、预设、错误码统计的任务数量；
- 排队时长、下载时长、合并或转码时长；
- 成功文件字节数、失败文件字节数和清理字节数；
- Worker 并发、可用磁盘、FFmpeg 和 yt-dlp 版本；
- 上游暂时故障和限流发生率。

日志中推荐用 job_id、video_id、预设、耗时和错误码关联一次任务；避免完整 URL 和私人标题进入长期日志。

## 11. 测试清单

### 11.1 单元测试

- normalize_youtube_video_url 接受 watch、短链、Shorts、Live、Embed；
- 拒绝 http、第三方域名、youtube.com@evil.example、账号密码、播放列表入口和空 ID；
- 预设只能取固定枚举值；
- 子进程命令为参数列表，URL 只是最后一个参数；
- 工具输出路径若不在任务根目录、不是文件或不唯一，任务必须失败；
- 权限校验阻止其他用户查询、下载或删除任务。

### 11.2 集成测试

使用团队自己上传的短测试视频或已获得明确许可的素材：

1. 模拟解析成功；
2. MP4 预设可下载并由 FFmpeg 合并；
3. 音频预设可产生正确容器；
4. 删除源视频、私有视频、格式缺失和网络中断被正确分类；
5. 超时和取消后没有残留 Worker 或 FFmpeg 进程；
6. 过期清理不会越过下载根目录，也不会删除仍被授权访问的文件。

不要把外部公开视频 ID 写死为关键 CI 依赖。YouTube 的可用性、地区和格式会变化；CI 更适合测试 URL 校验、任务状态机、命令构造和模拟的 yt-dlp 进程输出。

## 12. 推荐接入步骤

1. 明确产品只服务于本人拥有、授权或可合法保存的内容，并确定保留期和配额。
2. 在测试环境安装固定版本的 yt-dlp 与 FFmpeg，使用自有测试视频完成模拟解析和真实下载。
3. 实现 URL 规范化、固定预设、任务表和鉴权。
4. 建立独立 Worker 与每任务目录；先实现 video_mp4 一个预设即可。
5. 加入磁盘、时长、并发限制、错误分类、任务取消和过期清理。
6. 接入私有文件服务或短期签名下载 URL，而不是公开静态目录。
7. 增加监控、告警和下载器升级回归测试。
8. 最后才考虑音频、字幕、批量任务或对象存储；这些不应阻塞单视频 MP4 的安全落地。

## 13. 当前 NewsRSSHub 的实现与部署

当前调用链：

~~~text
POST /api/youtube/download
  -> 校验并规范化单条 YouTube 视频 URL
  -> 若存在 youtube-runtime/cookies.txt，复制到任务内私有临时文件
  -> 以临时文件的 --cookies 参数调用 yt-dlp，退出后删除该副本
  -> Web 进程同步调用 yt-dlp + FFmpeg
  -> FileResponse 直接回传 MP4
  -> 响应结束后的 BackgroundTask 删除本次任务目录
~~~

涉及文件：

- “app/services/youtube_download.py”：URL 校验、yt-dlp 调用、输出路径校验和安全清理；
- “app/services/youtube_session.py”：网页 Cookie 解析、Netscape 格式转换和私有运行时文件原子写入；
- “app/web.py”：无额外鉴权的 POST 接口和文件响应；
- “app/templates/settings.html”：手动保存 YouTube 下载 Cookie 的设置页入口；
- “requirements.txt”：固定版本的 yt-dlp；
- “Dockerfile”：安装 FFmpeg；
- 可选配置：“config.yml” 中的 app.youtube_download_timeout_seconds；未填写时默认 3600 秒。

部署时执行一次镜像重建，使 yt-dlp 和 FFmpeg 进入 Web 容器：

~~~bash
docker compose up -d --build
~~~

本机不用 Docker 时，也必须安装 requirements.txt 中的 Python 依赖和系统级 FFmpeg。YOUTUBE_KEY 不需要复制到主服务，也不会被该接口使用；它仍只属于 RSSHub 的 YouTube 频道抓取配置。

边界仍保持清楚：

- collector、processor、RSSHub 和 items.media_json 继续只负责资讯发现和远端预览；
- YouTube 下载 Cookie 只供 yt-dlp 下载使用，不会让频道抓取变成“必须登录”或写入来源配置；
- 下载只由接口调用显式触发，不批量下载订阅频道；
- 下载成品不会写入 RSS 条目、media_json 或 SQLite，响应结束即清理；
- 下载失败不会影响来源抓取、摘要、事件筛选和今日热点；
- 因为接口没有额外鉴权，部署者应自行确保站点访问范围符合“仅自己使用”的前提。

## 14. 官方参考

- [yt-dlp 安装与用法](https://github.com/yt-dlp/yt-dlp#installation)
- [yt-dlp 格式选择说明](https://github.com/yt-dlp/yt-dlp#format-selection)
- [FFmpeg 下载页](https://ffmpeg.org/download.html)
- [YouTube 服务条款](https://www.youtube.com/t/terms)

上游工具和平台页面会更新。实施或排障前，应以当时官方文档、已锁定版本的帮助输出和项目合规要求为准。

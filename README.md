# NewsRSSHub

一个面向个人的每日情报台：从 RSS、X 与 Reddit 采集信息；每条帖子先生成摘要，再由项目级 Skill 合并同一事件、去除重复并划分为必看、重要更新、资讯速览和已隐藏。

## 你每天看到什么

- 首页默认打开“必看”，每页最多 50 条；四个 Tab 互不混排；
- 普通资讯可只看标题，点击后才查看摘要、原始内容与链接；
- 来源可在网页中添加、测试、启停、编辑和归档；
- 来源管理可导出全部来源，并会每 3 天自动保存一份可下载快照；导出和快照均可直接回传到“批量添加”恢复来源；
- 同一事件的多条内容会合并，避免信息流重复轰炸。
- 所有启用来源共用一套抓取策略；首次、新增、重新启用和策略变更都会在 1–5 分钟内随机错峰，单批请求之间再等待 2–5 秒。
- 资讯详情可预览已验证的图片、直链视频和受信任平台嵌入；收藏可在来源暂停或归档后继续阅读。
- 来源保存名称、账号简介、平台、地址、官方标记和启用状态；历史的单来源抓取间隔仅为旧数据兼容保留，不再参与调度。

## Docker 启动

1. 在 `config.yml` 中填写 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 与 `OPENAI_MODEL_NAME`。GitHub/Docker 部署会直接使用这份配置。
   如果要添加 YouTube 频道，还需在 `app.rsshub_base_url` 填入 **NewsRSSHub 容器可访问** 的 RSSHub 地址，例如同一 Docker 网络中的 `http://rsshub:1200`。

   YouTube 的 Cookie 不由 NewsRSSHub 读取或转发。不要在这里添加 `YouTube_Cookie = "..."` 这类 `.env` 写法：它不是 YAML，会让应用无法启动；当前 RSSHub 的频道路由通常不需要 Cookie。若你部署的特定 RSSHub 版本明确要求 Cookie，请按该 RSSHub 的部署文档把它配置在 **RSSHub 自己的容器** 中，而不是 NewsRSSHub 的 `config.yml`。
2. 如果要在网页中维护 X Cookie 或模型连接，为它设置一次加密主密钥（这不是 X Cookie）：

   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   将输出填入 `config.yml` 的 `CREDENTIAL_ENCRYPTION_KEY`。
3. 在 Portainer 创建或更新 Git Stack：仓库填写本项目 GitHub 地址，Compose 文件路径填写 `docker-compose.yml`，然后点击“从 Git 仓库重新部署”。主 Stack 只包含 Web 和 Worker，Portainer 会负责拉取代码、构建镜像和启动服务。

### SQLite 结构升级

当前数据库结构为 v9。v8 升级到 v9 仅新增 `sources.description` 且有默认空值，Web 服务启动时会在单个 SQLite 事务中自动完成；常规 Git Stack 或 `docker compose up -d --build` 部署不需要单独构建或运行迁移镜像。

更早的历史结构仍不会在运行服务中重建。只有部署前遗留在 v7 或更早版本的数据库，才需要停掉服务后使用同一份 Compose 配置执行 `docker compose --profile maintenance run --rm migrate --check` 和 `--apply`。维护命令会先用 SQLite backup API 在持久化数据目录创建备份，再进行单事务迁移和完整性校验；不要手工复制正在 WAL 模式运行的数据库文件。详细原理与回滚步骤见 [docs/SQLITE_SCHEMA_AND_MIGRATION.md](docs/SQLITE_SCHEMA_AND_MIGRATION.md)。

4. 打开 `http://localhost:8188`，进入“设置与连接”的 X 区域，粘贴 `auth_token` 值或完整 Cookie 片段。系统先验证，成功后才加密保存。

Web 服务只负责页面和配置；Compose 中的 `collector` Worker 只负责排期、抓取和来源快照，`processor` Worker 独立完成中文标题/摘要/重点 → Skill 筛选 → 正文译文 → 每日简报与过期内容清理。SQLite 数据保存在 `data/`，重启容器不会丢失。项目策略文件 `.agents/skills/curate-personal-news/SKILL.md` 会一并复制到镜像，缺失时系统会明确显示筛选不可用，而不会退回旧关键词评分。

## iPhone 手机通知

NewsRSSHub 支持标准 Web Push。它只发送一条聚合提醒，不会按资讯逐条打扰手机：

```text
NewsRSSHub
本轮抓取发现 X 条新内容，点此查看
```

其中 `X` 是本轮真正新写入数据库的条目数；点击通知始终打开首页。首次发现新增后会等待 1 分钟收拢错峰来源，之后在当前全局抓取间隔内最多再发一条。临时网络错误会重试；浏览器返回订阅失效时，系统会停止推送并要求重新开启。

启用步骤：

1. 站点必须以稳定 HTTPS 域名访问，并在 `config.yml` 的 `app.web_push_subject` 中填写同一站点的 HTTPS 地址或有效的 `mailto:` 地址。
2. 重新部署后，用 iPhone Safari 删除旧的主屏幕图标，再通过“分享 → 添加到主屏幕”创建新版 Web App。
3. 从主屏幕图标打开 NewsRSSHub，进入“设置与连接 → 手机通知”，点击“开启手机通知”，允许 iOS 系统权限后可用“发送测试通知”验证。

VAPID 私钥会在首次开启时自动生成，并使用 `CREDENTIAL_ENCRYPTION_KEY` 加密保存到 SQLite；不需要 Apple 开发者账号、App Store 或额外容器。若这台手机在 iOS 设置中关闭了通知，或浏览器订阅被系统回收，只需按上述页面重新开启即可。

## 本地开发

```bash
python -m pip install -r requirements.txt
python -m app.worker --once --force
uvicorn app.web:app --reload
```

默认会导入 `sources/feeds.yml` 中已有的来源。X 账号 Cookie 只存入 SQLite 的加密字段，既不会写入 `docker-compose.yml`，也不会回显到网页；更换 Cookie 后不需要重启容器。Worker 每轮抓取 X 账号前都会先验证 Cookie，失效时首页和来源管理页会提示更新。

## 来源导出与自动备份

在“来源管理”点击“导出全部来源”，即可下载当前来源的 YAML。这个文件和页面中的任一自动备份文件都可以直接上传到“批量添加”入口：名称、账号简介、平台、地址、官方标记和暂停/归档状态都会恢复；已存在的来源默认安全跳过，但 YAML 中的非空 `description` 会同步更新该来源的账号简介，不会覆盖当前启停和抓取状态。

来源快照刻意不覆盖运行中实例的全局抓取间隔；恢复到新环境后，如需沿用原有节奏，请在“设置与连接 → 统一抓取策略”中重新设置。这避免导入来源时意外改变现有实例的调度负载。

Worker 首次运行会创建一份来源快照，之后每 3 天最多创建一份。快照保存到容器的 `/app/data/source_backups/`，因 Compose 已挂载数据目录，服务器实际位置为 `/home/jzb/docker/rss-hub/data/source_backups/`。系统仅保留最新 5 份；Cookie、API Key、抓取状态、错误记录和连接器缓存都不会写入这些 YAML 文件。

本次 SQLite v9、账号简介、全局抓取、媒体预览与收藏保留的整合说明见 [整合记录](docs/INTEGRATION_2026-08-04_FETCH_MEDIA_FAVORITES.md)；此前 SQLite v7、来源备份恢复和筛选合并规则调整的背景见 [2026-08-04 迭代记录](docs/ITERATION_2026-08-04_SQLITE_AND_SOURCE_BACKUP.md)。

## 内容处理顺序

```text
原始帖子 → 中文标题、摘要、重点 → 项目 Skill（合并 / 去重 / 四层判断） → 必看/重要正文中文译文 → SQLite → 四层 Tab
```

筛选模型只收到用户自然语言画像及每条帖子的 `id`、原始标题、中文摘要、发布时间；不会收到原始正文、账号、Cookie 或链接。正文翻译是详情页展示缓存：后台只预翻译“必看”和“重要更新”的主来源，其他来源可在详情页按需生成。完整产品需求、数据迁移与架构见 `docs/PRODUCT_REQUIREMENTS_AND_ARCHITECTURE.md`。

## 安全提示

`config.yml` 是唯一的部署配置来源；项目不再读取或需要 `.env`。请将包含 API Key 的仓库设为私有仓库。

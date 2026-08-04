# NewsRSSHub

一个面向个人的每日情报台：从 RSS、X 与 Reddit 采集信息；每条帖子先生成摘要，再由项目级 Skill 合并同一事件、去除重复并划分为必看、重要更新、资讯速览和已隐藏。

## 你每天看到什么

- 首页默认打开“必看”，每页最多 50 条；四个 Tab 互不混排；
- 普通资讯可只看标题，点击后才查看摘要、原始内容与链接；
- 来源可在网页中添加、测试、启停、编辑和归档；
- 来源管理可导出全部来源，并会每 3 天自动保存一份可下载快照；导出和快照均可直接回传到“批量添加”恢复来源；
- 同一事件的多条内容会合并，避免信息流重复轰炸。
- 来源只保存名称、平台、地址、抓取间隔、官方标记和启用状态；不再有主题、优先级或备用链接字段。

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

### SQLite 结构

当前项目固定使用 SQLite v7。你的线上数据库已完成一次性 v6 → v7 升级，因此后续正常更新只需要在 Portainer 点击“从 Git 仓库重新部署”，不再有迁移容器、`--check`、`--apply` 或 SSH 部署步骤。

空的数据目录会自动创建 v7 数据库；已有 v7 数据库会在启动时校验结构。项目不会自动修改旧版本数据库，避免普通部署意外改动历史数据。v7 数据结构和这次一次性升级的记录见 [docs/SQLITE_SCHEMA_AND_MIGRATION.md](docs/SQLITE_SCHEMA_AND_MIGRATION.md)。

4. 打开 `http://localhost:8188`，进入“设置与连接”的 X 区域，粘贴 `auth_token` 值或完整 Cookie 片段。系统先验证，成功后才加密保存。

Web 服务只负责页面和配置；Worker 独立完成抓取 → 中文标题/摘要/重点 → Skill 筛选 → 必看与重要更新的正文译文 → 每日简报。SQLite 数据保存在 `data/`，重启容器不会丢失。项目策略文件 `.agents/skills/curate-personal-news/SKILL.md` 会一并复制到镜像，缺失时系统会明确显示筛选不可用，而不会退回旧关键词评分。

## 本地开发

```bash
python -m pip install -r requirements.txt
python -m app.worker --once --force
uvicorn app.web:app --reload
```

默认会导入 `sources/feeds.yml` 中已有的来源。X 账号 Cookie 只存入 SQLite 的加密字段，既不会写入 `docker-compose.yml`，也不会回显到网页；更换 Cookie 后不需要重启容器。Worker 每轮抓取 X 账号前都会先验证 Cookie，失效时首页和来源管理页会提示更新。

## 来源导出与自动备份

在“来源管理”点击“导出全部来源”，即可下载当前来源的 YAML。这个文件和页面中的任一自动备份文件都可以直接上传到“批量添加”入口：名称、平台、地址、官方标记、暂停/归档状态和抓取间隔都会恢复；已存在的来源会被安全跳过，不会覆盖当前记录。

Worker 首次运行会创建一份来源快照，之后每 3 天最多创建一份。快照保存到容器的 `/app/data/source_backups/`，因 Compose 已挂载数据目录，服务器实际位置为 `/home/jzb/docker/rss-hub/data/source_backups/`。系统仅保留最新 5 份；Cookie、API Key、抓取状态、错误记录和连接器缓存都不会写入这些 YAML 文件。

本次 SQLite v7、来源备份恢复和筛选合并规则调整的背景、问题、实现与部署边界见 [2026-08-04 迭代记录](docs/ITERATION_2026-08-04_SQLITE_AND_SOURCE_BACKUP.md)。

## 内容处理顺序

```text
原始帖子 → 中文标题、摘要、重点 → 项目 Skill（合并 / 去重 / 四层判断） → 必看/重要正文中文译文 → SQLite → 四层 Tab
```

筛选模型只收到用户自然语言画像及每条帖子的 `id`、原始标题、中文摘要、发布时间；不会收到原始正文、账号、Cookie 或链接。正文翻译是详情页展示缓存：后台只预翻译“必看”和“重要更新”的主来源，其他来源可在详情页按需生成。完整产品需求、数据迁移与架构见 `docs/PRODUCT_REQUIREMENTS_AND_ARCHITECTURE.md`。

## 安全提示

`config.yml` 是唯一的部署配置来源；项目不再读取或需要 `.env`。请将包含 API Key 的仓库设为私有仓库。

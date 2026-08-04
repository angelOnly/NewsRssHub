# NewsRSSHub

一个面向个人的每日情报台：从 RSS、X 与 Reddit 采集信息；每条帖子先生成摘要，再由项目级 Skill 合并同一事件、去除重复并划分为必看、重要更新、资讯速览和已隐藏。

## 你每天看到什么

- 首页默认打开“必看”，每页最多 50 条；四个 Tab 互不混排；
- 普通资讯可只看标题，点击后才查看摘要、原始内容与链接；
- 来源可在网页中添加、测试、启停、编辑和归档；
- 同一事件的多条内容会合并，避免信息流重复轰炸。
- 来源只保存名称、平台、地址、抓取间隔、备用链接、官方标记和启用状态；不再有主题或优先级字段。

## Docker 启动

1. 在 `config.yml` 中填写 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 与 `OPENAI_MODEL_NAME`。GitHub/Docker 部署会直接使用这份配置。
   如果要添加 YouTube 频道，还需在 `app.rsshub_base_url` 填入 **NewsRSSHub 容器可访问** 的 RSSHub 地址，例如同一 Docker 网络中的 `http://rsshub:1200`。

   YouTube 的 Cookie 不由 NewsRSSHub 读取或转发。不要在这里添加 `YouTube_Cookie = "..."` 这类 `.env` 写法：它不是 YAML，会让应用无法启动；当前 RSSHub 的频道路由通常不需要 Cookie。若你部署的特定 RSSHub 版本明确要求 Cookie，请按该 RSSHub 的部署文档把它配置在 **RSSHub 自己的容器** 中，而不是 NewsRSSHub 的 `config.yml`。
2. 如果要在网页中维护 X Cookie 或模型连接，为它设置一次加密主密钥（这不是 X Cookie）：

   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   将输出填入 `config.yml` 的 `CREDENTIAL_ENCRYPTION_KEY`。
3. 执行：

   ```bash
   docker compose up -d --build
   ```

4. 打开 `http://localhost:8188`，进入“设置与连接”的 X 区域，粘贴 `auth_token` 值或完整 Cookie 片段。系统先验证，成功后才加密保存。

Web 服务只负责页面和配置；Worker 独立完成抓取 → 中文标题/摘要/重点 → Skill 筛选 → 必看与重要更新的正文译文 → 每日简报。SQLite 数据保存在 `data/`，重启容器不会丢失。项目策略文件 `.agents/skills/curate-personal-news/SKILL.md` 会一并复制到镜像，缺失时系统会明确显示筛选不可用，而不会退回旧关键词评分。

## 本地开发

```bash
python -m pip install -r requirements.txt
python -m app.worker --once --force
uvicorn app.web:app --reload
```

默认会导入 `sources/feeds.yml` 中已有的来源。X 账号 Cookie 只存入 SQLite 的加密字段，既不会写入 `docker-compose.yml`，也不会回显到网页；更换 Cookie 后不需要重启容器。Worker 每轮抓取 X 账号前都会先验证 Cookie，失效时首页和来源管理页会提示更新。

## 内容处理顺序

```text
原始帖子 → 中文标题、摘要、重点 → 项目 Skill（合并 / 去重 / 四层判断） → 必看/重要正文中文译文 → SQLite → 四层 Tab
```

筛选模型只收到用户自然语言画像及每条帖子的 `id`、原始标题、中文摘要、发布时间；不会收到原始正文、账号、Cookie 或链接。正文翻译是详情页展示缓存：后台只预翻译“必看”和“重要更新”的主来源，其他来源可在详情页按需生成。完整产品需求、数据迁移与架构见 `docs/PRODUCT_REQUIREMENTS_AND_ARCHITECTURE.md`。

## 安全提示

`config.yml` 是唯一的部署配置来源；项目不再读取或需要 `.env`。请将包含 API Key 的仓库设为私有仓库。

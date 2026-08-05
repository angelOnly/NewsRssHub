# RSSHub 全平台抓取部署

本项目的 X、Reddit、YouTube 和通用 RSS 均通过 RSSHub 抓取。X 的完整 Cookie 不写入 Compose 环境变量或 SQLite，也不依赖容器重启；Web 页面保存并验证成功后，应用会原子写入宿主机的共享文件。

共享目录为：

```text
/home/jzb/docker/rss-hub/data/rsshub-runtime/
├── x-twitter.json   # 完整 x.com Cookie，权限 0600
└── rss-feeds.json   # 通用 RSS 白名单，权限 0600
```

NewsRSSHub 的 Web、collector、processor、topics 已将 `/home/jzb/docker/rss-hub/data` 挂载到 `/app/data`，所以不需要再给应用增加卷。RSSHub 只读挂载上述 `rsshub-runtime` 子目录，无法读取 `rss_news.db`。

## 1. 部署自定义 RSSHub 镜像

将仓库中的 `deploy/rsshub-custom` 目录放到 RSSHub 宿主机，例如：

```bash
mkdir -p /home/jzb/docker/rss-hub/data/rsshub-runtime
chmod 700 /home/jzb/docker/rss-hub/data/rsshub-runtime
cd /home/jzb/docker/rss-hub
# 将项目 deploy/rsshub-custom 复制到这里后，使用其中的 compose 文件。
```

把现有 RSSHub Compose 的 `rsshub` 服务替换为下面的内容。保留你已有的 `YOUTUBE_KEY` 实际值；不要添加 `TWITTER_AUTH_TOKEN`。

```yaml
services:
  rsshub:
    build:
      context: ./rsshub-custom
    image: local/newsrsshub-rsshub:latest
    restart: unless-stopped
    ports:
      - "31200:1200"
    volumes:
      - /home/jzb/docker/rss-hub/data/rsshub-runtime:/run/newsrsshub-runtime:ro
    environment:
      TZ: "Asia/Shanghai"
      NODE_ENV: "production"
      CACHE_TYPE: "memory"
      CACHE_EXPIRE: "300"
      CACHE_CONTENT_EXPIRE: "43200"
      DEBUG_INFO: "false"
      YOUTUBE_KEY: "保留你原来的值"
      NEWSRSSHUB_RUNTIME_DIR: "/run/newsrsshub-runtime"
```

构建并启动：

```bash
cd /home/jzb/docker/rss-hub
docker compose build rsshub
docker compose up -d rsshub
docker compose logs -f --tail=100 rsshub
```

镜像会从固定的 RSSHub 源码提交构建并应用项目补丁。首次构建需要下载 Node 依赖和 Chromium，耗时明显长于直接拉取 `diygod/rsshub:chromium-bundled`，后续构建会利用 Docker 缓存。

## 2. 部署 NewsRSSHub 应用

更新 NewsRSSHub 到本次代码后重新构建其 Web 和 Worker：

```bash
docker compose up -d --build web collector processor topics
```

应用启动时会完成两件事：

1. 将旧数据库中 X、Reddit、YouTube、通用 RSS 的直连 `feed_url` 迁移为 RSSHub 路由；
2. 保留已有的 `x-twitter.json`，并从所有未归档通用 RSS 来源生成 `rss-feeds.json`。

之后在 Web 的“设置与连接”更新 X Cookie，会依次执行：校验完整 Cookie 至少包含 `auth_token` 与 `ct0` → 写入候选共享文件 → 请求 RSSHub 的 X 验证路由 → 由 RSSHub 实际访问 X 成功后保留该文件。验证失败会恢复之前的共享文件。X Cookie 没有 SQLite 副本；升级时应用会删除旧版 SQLite 中的 `x_session` 凭据，旧版仅 `auth_token` 的文件需重新粘贴完整 Cookie。

## 3. 真实验证

以下命令应返回 HTTP 200 和 RSS/Atom 内容：

```bash
curl -fsS https://rsshub.xiaolicloud.cn:18443/reddit/r/OpenAI | head
curl -fsS https://rsshub.xiaolicloud.cn:18443/youtube/channel/UCXZCJLdBC09xxGZ6gcdrc6A | head
curl -fsS https://rsshub.xiaolicloud.cn:18443/newsrsshub/x/validate | head
curl -fsS https://rsshub.xiaolicloud.cn:18443/twitter/user/OpenAI | head
```

通用 RSS 的 RSSHub 地址已存入数据库，可先查出一条再请求：

```bash
sqlite3 /home/jzb/docker/rss-hub/data/rss_news.db \
  "SELECT feed_url FROM sources WHERE kind = 'rss' AND archived = 0 LIMIT 1;"
```

将输出的 URL 传给 `curl -fsS '上一步输出的 URL' | head`。不要把 `x-twitter.json`、Cookie 或 API Key 贴到终端日志、截图或聊天中。

如果 Reddit 返回上游限流或 X 返回 5xx，先查看 `docker compose logs --tail=200 rsshub`。这表示 RSSHub 已接收到对应路由请求，但上游平台拒绝或暂时不可用；它与路由未注册导致的 404 不同。

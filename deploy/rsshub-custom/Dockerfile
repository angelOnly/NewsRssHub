# 固定 RSSHub 源码提交并应用本项目补丁，避免上游 latest 更新后自定义路由悄然失效。
FROM node:24-trixie AS builder

ARG RSSHUB_REPOSITORY=https://github.com/DIYgod/RSSHub.git
ARG RSSHUB_COMMIT=ff4256ae226692fdcc4d9c575d3e93749cca16ff
ARG TARGETPLATFORM=linux/amd64

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && git clone "$RSSHUB_REPOSITORY" /app \
    && git -C /app checkout --detach "$RSSHUB_COMMIT"

COPY patches/newsrsshub-routes.patch /tmp/newsrsshub-routes.patch

RUN git -C /app apply --check /tmp/newsrsshub-routes.patch \
    && git -C /app apply /tmp/newsrsshub-routes.patch

# Chromium 单独下载，防止 pnpm install 在不同平台上隐式改变依赖内容。
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=true \
    PLAYWRIGHT_BROWSERS_PATH=/app/node_modules/.cache/ms-playwright

RUN corepack enable pnpm \
    && pnpm install --frozen-lockfile \
    && pnpm rb \
    && pnpm build \
    && pnpm prune --prod \
    && if [ "$TARGETPLATFORM" = "linux/amd64" ]; then pnpm exec patchright install chromium; fi

FROM node:24-trixie-slim

ARG TARGETPLATFORM=linux/amd64

ENV NODE_ENV=production \
    TZ=Asia/Shanghai \
    PLAYWRIGHT_BROWSERS_PATH=/app/node_modules/.cache/ms-playwright

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends dumb-init git curl ca-certificates \
    && if [ "$TARGETPLATFORM" = "linux/amd64" ]; then \
        apt-get install -y --no-install-recommends \
            fonts-liberation wget xdg-utils \
            libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 libcairo2 libcups2t64 libdbus-1-3 libdrm2 \
            libexpat1 libgbm1 libglib2.0-0t64 libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 \
            libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2; \
    else \
        apt-get install -y --no-install-recommends chromium; \
    fi \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/dist ./dist
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./package.json

RUN if [ "$TARGETPLATFORM" = "linux/amd64" ]; then \
        chrome_path=$(find /app/node_modules/.cache/ms-playwright -name chrome -type f -executable | head -n 1); \
        test -n "$chrome_path"; \
        echo "CHROMIUM_EXECUTABLE_PATH=$chrome_path" > /app/.env; \
    else \
        echo "CHROMIUM_EXECUTABLE_PATH=$(command -v chromium)" > /app/.env; \
    fi

EXPOSE 1200

ENTRYPOINT ["dumb-init", "--"]
CMD ["npm", "run", "start"]

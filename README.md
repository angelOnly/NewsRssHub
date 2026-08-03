# NewsRSSHub

一个面向个人的每日情报台：从 RSS、X 与 Reddit 采集信息，去重为事件，按重要性排序，再生成中文摘要与每日简报。

## 你每天看到什么

- 首页默认只显示摘要，每页最多 50 条，按重要性排序；
- 点击摘要后才展示模型完整解读、原始内容与链接；
- 来源可在网页中添加、测试、启停、编辑和归档；
- 同一事件的多条内容会合并，避免信息流重复轰炸。

## Docker 启动

1. 复制 `.env.example` 为 `.env`，填入模型密钥和模型地址。不要把密钥写进 `config.yml`。
2. 为 X Cookie 设置一次加密主密钥（这不是 X Cookie）：

   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   将输出填入 `.env` 的 `CREDENTIAL_ENCRYPTION_KEY`。
3. 确认 `config.yml` 中的 RSSHub 地址正确。
4. 执行：

   ```bash
   docker compose up -d --build
   ```

5. 打开 `http://localhost:8188`，进入“X 登录状态”，粘贴 `auth_token` 值或完整 Cookie 片段。系统先验证，成功后才加密保存。

Web 服务只负责页面和配置；Worker 独立抓取并生成摘要。SQLite 数据保存在 `data/`，重启容器不会丢失。

## 本地开发

```bash
python -m pip install -r requirements.txt
python -m app.worker --once --force
uvicorn app.web:app --reload
```

默认会导入 `sources/feeds.yml` 中已有的来源。X 账号 Cookie 只存入 SQLite 的加密字段，既不会写入 `docker-compose.yml`，也不会回显到网页；更换 Cookie 后不需要重启容器。Worker 每轮抓取 X 账号前都会先验证 Cookie，失效时首页和来源管理页会提示更新。

## 安全提示

旧版 `config.yml` 中的模型配置仅为兼容保留。建议撤销其中已暴露的旧密钥，改用 `.env` 的 `OPENAI_API_KEY`。

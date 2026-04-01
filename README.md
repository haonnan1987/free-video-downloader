# Video Fetch — 全能视频下载器

一键部署的视频下载 Web 应用。支持 YouTube、TikTok、Twitter/X、Instagram、B站、Vimeo 等 20+ 平台。

## 快速启动（Docker）

```bash
docker-compose up -d
```

打开 http://localhost:8000 即可使用。**用户无需任何配置。**

## YouTube 增强（可选）

大部分 YouTube 视频无需任何配置即可下载。如果遇到 "需要登录" 提示，说明 YouTube 对你当前 IP 实施了反机器人检测。
解决方法：导出浏览器 YouTube Cookie，放到 `cookies/` 目录即可。

### 步骤

1. 安装浏览器扩展 [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
2. 在浏览器中打开 YouTube 并登录你的 Google 账号
3. 点击扩展图标 → "Export" → 保存为 `cookies.txt`
4. 将文件放到项目的 `cookies/cookies.txt`
5. 重启服务：`docker-compose restart web`

> Cookie 通过只读卷挂载到容器中，不会被修改或上传到任何地方。

## 架构

```
用户浏览器  →  FastAPI Web 服务（端口 8000）
                  ├─ cobalt 引擎（优先）     → YouTube/TikTok/Twitter 等 20+ 平台
                  ├─ yt-dlp 引擎（兜底）     → 1000+ 站点
                  └─ PoToken 提供者          → 绕过 YouTube 机器人检测
```

- **cobalt** — 开源媒体下载服务，Docker sidecar 自动运行
- **yt-dlp** — 成熟命令行下载工具 + bgutil PoToken 插件
- **pot-provider** — YouTube PoToken 生成服务（减少机器人检测）

## 本地开发

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

本地开发时如需 cobalt，可单独启动：

```bash
docker run -p 9000:9000 -e API_URL=http://localhost:9000 ghcr.io/imputnet/cobalt:10
```

然后在 `.env` 中设置 `COBALT_API_URL=http://localhost:9000`。

## 托管到 GitHub

1. 在 [GitHub](https://github.com/new) 新建空仓库（不要勾选添加 README）。
2. 在项目根目录执行（将 `你的用户名` / `仓库名` 换成实际值）：

```bash
git init
git add .
git commit -m "Initial commit: VideoFetch 视频下载 Web 应用"
git branch -M main
git remote add origin https://github.com/你的用户名/仓库名.git
git push -u origin main
```

3. **切勿提交** `.env`、`cookies/cookies.txt`（已在 `.gitignore` 中忽略）。部署时复制 `.env.example` 为 `.env` 并按需填写。

若使用 SSH：`git remote add origin git@github.com:你的用户名/仓库名.git`。

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/resolve` | 解析视频链接，返回标题、缩略图、可用格式 |
| POST | `/api/download` | 创建下载任务 |
| GET  | `/api/jobs/{id}` | 查询任务状态 |
| GET  | `/api/jobs/{id}/file` | 下载完成的文件 |

## 技术栈

- **前端** — 原生 HTML/CSS/JS，移动端适配
- **后端** — Python FastAPI + uvicorn
- **下载引擎** — cobalt + yt-dlp + bgutil PoToken
- **部署** — Docker Compose 一键启动

## 合规

仅下载你有权获取的内容。禁止绕过 DRM、付费墙或侵犯他人版权。

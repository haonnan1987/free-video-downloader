from __future__ import annotations

import os
import shlex
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
# 必须从「项目根目录」加载 .env；若仅用 load_dotenv()，在其它 cwd 下启动 uvicorn 会读不到配置
ENV_FILE_PATH = BASE_DIR / ".env"
load_dotenv(ENV_FILE_PATH)

# Vercel / AWS Lambda 等：文件系统除 /tmp 外多为只读，且不适合长任务与 yt-dlp 完整链路
IS_VERCEL = os.environ.get("VERCEL", "").strip().lower() in ("1", "true", "yes")

def _default_download_dir() -> Path:
    if IS_VERCEL:
        return Path("/tmp/videofetch-downloads")
    raw = os.environ.get("DOWNLOAD_DIR", str(BASE_DIR / "data" / "downloads"))
    return Path(raw).resolve()


DOWNLOAD_DIR = _default_download_dir()
try:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass

JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", "86400"))
RESOLVE_TIMEOUT = int(os.environ.get("RESOLVE_TIMEOUT", "120"))
DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", "3600"))
MAX_URL_BYTES = int(os.environ.get("MAX_URL_BYTES", "2048"))
CLEANUP_INTERVAL_SECONDS = int(os.environ.get("CLEANUP_INTERVAL_SECONDS", "600"))

ALLOW_LOCAL_NETWORK = os.environ.get("ALLOW_LOCAL_NETWORK", "false").lower() in ("1", "true", "yes")

# 抖音：无 cookies.txt 时尝试自动拉取访客 Cookie（curl_cffi/httpx + 可选无头 Chromium）；设为 false 可关闭
DOUYIN_GUEST_COOKIES = os.environ.get("DOUYIN_GUEST_COOKIES", "true").lower() in ("1", "true", "yes")
# 抖音：若轻量请求拿不到 s_v_web_id 等，再用 Playwright 开页导出 Cookie（不要求用户或站长提供 cookies.txt）；镜像需含 Chromium
_default_playwright = "false" if IS_VERCEL else "true"
DOUYIN_PLAYWRIGHT = os.environ.get(
    "DOUYIN_PLAYWRIGHT", _default_playwright
).lower() in ("1", "true", "yes")

# yt-dlp 可选增强（YouTube 常见问题见 README）
YTDLP_COOKIES_FILE = os.environ.get("YTDLP_COOKIES_FILE", "").strip()
YTDLP_COOKIES_FROM_BROWSER = os.environ.get("YTDLP_COOKIES_FROM_BROWSER", "").strip()
YTDLP_JS_RUNTIMES = os.environ.get("YTDLP_JS_RUNTIMES", "").strip()
YTDLP_EXTRACTOR_ARGS = os.environ.get("YTDLP_EXTRACTOR_ARGS", "").strip()
_extra = os.environ.get("YTDLP_EXTRA_ARGS", "").strip()
YTDLP_EXTRA_ARGS: list[str] = shlex.split(_extra) if _extra else []

# 未设置 YTDLP_JS_RUNTIMES 时，自动探测 PATH 中的 node/deno/bun（默认开启）
YTDLP_AUTO_JS_RUNTIME = os.environ.get("YTDLP_AUTO_JS_RUNTIME", "true").lower() in ("1", "true", "yes")

# cobalt 备用通道（yt-dlp 失败时使用）
COBALT_API_URL = os.environ.get("COBALT_API_URL", "").strip()
COBALT_ENABLED = os.environ.get("COBALT_ENABLED", "true").lower() in ("1", "true", "yes")

# YouTube PoToken 提供者（绕过 "确认你不是机器人" 限制）
POT_PROVIDER_URL = os.environ.get("POT_PROVIDER_URL", "").strip()

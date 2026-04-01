from __future__ import annotations

import shutil
from pathlib import Path

from app import config
from app.cobalt import check_cobalt_sync as _check_cobalt
from app.ytdlp import _js_runtimes_cli


def get_diagnostics() -> dict:
    cookies_file = Path(config.YTDLP_COOKIES_FILE).expanduser() if config.YTDLP_COOKIES_FILE else None
    cookies_file_ok = bool(cookies_file and cookies_file.is_file())
    browser = bool(config.YTDLP_COOKIES_FROM_BROWSER)
    ffmpeg_ok = bool(shutil.which("ffmpeg"))
    ffprobe_ok = bool(shutil.which("ffprobe"))

    js_cli = _js_runtimes_cli()
    js_effective = js_cli[1] if len(js_cli) >= 2 and js_cli[0] == "--js-runtimes" else None

    cobalt_any, cobalt_public, cobalt_self = _check_cobalt()

    try:
        import playwright  # noqa: F401

        playwright_pkg = True
    except ImportError:
        playwright_pkg = False

    issues: list[str] = []
    if not browser and not cookies_file_ok:
        issues.append(
            "未配置 Cookie：YouTube 等需登录 Cookie 的平台可能失败；"
            "请在 .env 设置 YTDLP_COOKIES_FROM_BROWSER=chrome 或提供有效的 YTDLP_COOKIES_FILE。"
            "抖音默认由服务端自动生成访客 Cookie（DOUYIN_GUEST_COOKIES / DOUYIN_PLAYWRIGHT），不要求访客提供文件。"
        )
    elif cookies_file and config.YTDLP_COOKIES_FILE and not cookies_file_ok:
        issues.append("YTDLP_COOKIES_FILE 路径不存在或不可读")
    if not js_effective:
        issues.append("未检测到 node/deno/bun（或未设置 YTDLP_JS_RUNTIMES），YouTube 容易解析失败")
    if not ffmpeg_ok:
        issues.append("未在 PATH 中找到 ffmpeg，合并音视频会失败")
    if not cobalt_self and not cobalt_any:
        issues.append(
            "cobalt 备用通道不可用。推荐自建："
            "docker run -p 9000:9000 ghcr.io/imputnet/cobalt:10，"
            "然后在 .env 设置 COBALT_API_URL=http://localhost:9000"
        )
    if config.DOUYIN_PLAYWRIGHT and not playwright_pkg:
        issues.append(
            "DOUYIN_PLAYWRIGHT 已开启但未检测到 playwright 包；抖音解析可能缺少 s_v_web_id。"
            "Docker 镜像应执行 python -m playwright install --with-deps chromium。"
        )

    return {
        "env_file_path": str(config.ENV_FILE_PATH),
        "env_file_exists": config.ENV_FILE_PATH.is_file(),
        "douyin_guest_cookies": config.DOUYIN_GUEST_COOKIES,
        "douyin_playwright": config.DOUYIN_PLAYWRIGHT,
        "playwright_python_package": playwright_pkg,
        "cookies_from_browser_configured": browser,
        "cookies_file_configured": bool(config.YTDLP_COOKIES_FILE),
        "cookies_file_exists": cookies_file_ok,
        "ffmpeg_in_path": ffmpeg_ok,
        "ffprobe_in_path": ffprobe_ok,
        "js_runtime_for_ytdlp": js_effective,
        "youtube_ready": (browser or cookies_file_ok) and bool(js_effective) and ffmpeg_ok,
        "cobalt_available": cobalt_any,
        "cobalt_public_instances": cobalt_public,
        "cobalt_self_hosted": cobalt_self or "",
        "issues": issues,
    }

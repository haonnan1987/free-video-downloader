"""抖音：用无头 Chromium 打开页面，由 JS 写入访客 Cookie（如 s_v_web_id），无需用户或站长提供 cookies.txt。

注意：这是服务端自动生成的会话标识，并非「零请求」；只是不要求任何人手动导出 Cookie 文件。"""

from __future__ import annotations

import logging
import time

_log = logging.getLogger("uvicorn.error")

_DOUYIN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def playwright_douyin_cookie_lines(video_page_url: str) -> list[str]:
    """返回 Netscape 数据行（不含文件头）。失败返回 []."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _log.debug("douyin: playwright 未安装，跳过无头浏览器拉 Cookie")
        return []

    lines: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-gpu",
                ],
            )
            ctx = browser.new_context(
                user_agent=_DOUYIN_UA,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                viewport={"width": 1365, "height": 900},
            )
            page = ctx.new_page()
            page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=35000)
            page.goto(video_page_url.strip(), wait_until="load", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            try:
                page.evaluate("window.scrollTo(0, Math.min(600, document.body.scrollHeight || 600))")
            except Exception:
                pass
            page.wait_for_timeout(5000)
            for c in ctx.cookies():
                dom = (c.get("domain") or "").lower()
                if "douyin" not in dom:
                    continue
                name = c.get("name") or ""
                if not name:
                    continue
                value = c.get("value") or ""
                path = c.get("path") or "/"
                secure = "TRUE" if c.get("secure") else "FALSE"
                domain = c.get("domain") or ".douyin.com"
                dom_spec = "TRUE" if str(domain).startswith(".") else "FALSE"
                exp = c.get("expires")
                if exp is None or exp <= 0:
                    exp_sec = str(int(time.time()) + 86400 * 365)
                else:
                    exp_sec = str(int(exp))
                lines.append(f"{domain}\t{dom_spec}\t{path}\t{secure}\t{exp_sec}\t{name}\t{value}")
            browser.close()
    except Exception as e:
        _log.warning("douyin playwright: %s", e)
        return []

    if lines:
        _log.info("douyin playwright: got %d cookie(s)", len(lines))
    return lines

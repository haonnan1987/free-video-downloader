"""抖音 Web 接口需要访客态 Cookie。由服务端自动生成：curl_cffi / httpx、HTML 内嵌字段、可选 Playwright 无头 Chromium；可与站长可选的 cookies.txt 合并。

访客与站长均无需手动导出 Cookie 文件；cookies.txt 仅作可选增强。"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import httpx

from app import config

_log = logging.getLogger("uvicorn.error")

_DOUYIN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _extract_cookie_tokens_from_html(html: str) -> dict[str, str]:
    """从视频页 HTML/内嵌脚本中提取常见访客标识（抖音常把 s_v_web_id 写在脚本里）。"""
    out: dict[str, str] = {}
    if not html:
        return out
    patterns = [
        (r's_v_web_id["\']?\s*[=:]\s*["\']([^"\']{6,})["\']', "s_v_web_id"),
        (r'"s_v_web_id"\s*:\s*"([^"]+)"', "s_v_web_id"),
        (r"s_v_web_id=([0-9a-f]{8,})", "s_v_web_id"),
        (r'"ttwid"\s*:\s*"([^"]+)"', "ttwid"),
        (r"ttwid=([^;\s\"']+)", "ttwid"),
        (r'"msToken"\s*:\s*"([^"]+)"', "msToken"),
    ]
    for rx, name in patterns:
        if name in out:
            continue
        m = re.search(rx, html, re.I)
        if m:
            val = (m.group(1) or "").strip()
            if val and len(val) < 4096:
                out[name] = val
    return out


def _dedupe_netscape_data_lines(lines: list[str]) -> list[str]:
    """同名同域保留最后一次出现的行。"""
    by_key: dict[tuple[str, str], str] = {}
    for ln in lines:
        parts = ln.split("\t")
        if len(parts) >= 7:
            key = (parts[0], parts[5])
        else:
            key = ("", ln)
        by_key[key] = ln
    return list(by_key.values())


def _cookielib_cookie_to_line(c) -> str | None:
    """http.cookiejar.Cookie / requests Cookie → Netscape 一行。"""
    try:
        name = getattr(c, "name", None) or ""
        if not name:
            return None
        domain = getattr(c, "domain", None) or ".douyin.com"
        path = getattr(c, "path", None) or "/"
        secure = "TRUE" if getattr(c, "secure", False) else "FALSE"
        dom_spec = "TRUE" if str(domain).startswith(".") else "FALSE"
        exp = getattr(c, "expires", None)
        if exp is None:
            exp_sec = str(int(time.time()) + 86400 * 365)
        else:
            try:
                exp_sec = str(int(exp))
            except (TypeError, ValueError):
                exp_sec = str(int(time.time()) + 86400 * 365)
        value = getattr(c, "value", "") or ""
        return f"{domain}\t{dom_spec}\t{path}\t{secure}\t{exp_sec}\t{name}\t{value}"
    except Exception:
        return None


def _jar_to_netscape_lines(jar) -> list[str]:
    """httpx 等 CookieJar → Netscape 数据行。"""
    lines: list[str] = []
    try:
        cookie_list = list(jar)
    except (TypeError, ValueError):
        return lines
    for c in cookie_list:
        ln = _cookielib_cookie_to_line(c)
        if ln:
            lines.append(ln)
    return lines


def _requests_cookiejar_to_lines(jar) -> list[str]:
    """requests / curl_cffi 的 RequestsCookieJar。"""
    lines: list[str] = []
    try:
        for c in jar:
            ln = _cookielib_cookie_to_line(c)
            if ln:
                lines.append(ln)
    except (TypeError, ValueError):
        pass
    return lines


def _fetch_douyin_pages_curl_cffi(video_page_url: str) -> tuple[list[str], str]:
    """用 curl_cffi 模拟 Chrome TLS，常比纯 httpx 多拿到 Set-Cookie。"""
    try:
        from curl_cffi import requests as cr  # type: ignore[import-untyped]
    except ImportError:
        return [], ""
    base_h = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Upgrade-Insecure-Requests": "1",
    }
    try:
        s = cr.Session(impersonate="chrome131")
        s.headers.update({"User-Agent": _DOUYIN_UA})
        s.get("https://www.douyin.com/", headers=base_h, timeout=28)
        r2 = s.get(
            video_page_url.strip(),
            headers={**base_h, "Referer": "https://www.douyin.com/"},
            timeout=28,
        )
        lines = _requests_cookiejar_to_lines(s.cookies)
        return lines, (r2.text or "")
    except Exception as e:
        _log.debug("douyin guest cookies: curl_cffi failed: %s", e)
        return [], ""


def _fetch_douyin_pages_httpx(video_page_url: str) -> tuple[list[str], str]:
    base_h = {
        "User-Agent": _DOUYIN_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Upgrade-Insecure-Requests": "1",
    }
    try:
        with httpx.Client(timeout=28.0, follow_redirects=True, headers=base_h) as client:
            client.get("https://www.douyin.com/")
            r_vid = client.get(
                video_page_url.strip(),
                headers={**base_h, "Referer": "https://www.douyin.com/"},
            )
            lines = _jar_to_netscape_lines(client.cookies.jar)
            return lines, (r_vid.text or "")
    except (httpx.HTTPError, OSError, ValueError) as e:
        _log.debug("douyin guest cookies: httpx failed: %s", e)
        return [], ""


def fetch_guest_cookie_file(video_page_url: str, out_path: Path) -> bool:
    """
    拉取抖音首页 + 视频页 Cookie，写入 Netscape 文件。
    优先 curl_cffi（浏览器 TLS 指纹），失败再用 httpx。
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data_lines, html = _fetch_douyin_pages_curl_cffi(video_page_url)
    if not data_lines:
        data_lines, html = _fetch_douyin_pages_httpx(video_page_url)

    exp = str(int(time.time()) + 86400 * 180)
    for ck_name, ck_val in _extract_cookie_tokens_from_html(html).items():
        data_lines.append(f".douyin.com\tTRUE\t/\tFALSE\t{exp}\t{ck_name}\t{ck_val}")

    blob = "\n".join(data_lines).lower()
    if config.DOUYIN_PLAYWRIGHT and "s_v_web_id" not in blob:
        from app.douyin_playwright import playwright_douyin_cookie_lines

        data_lines.extend(playwright_douyin_cookie_lines(video_page_url))

    data_lines = _dedupe_netscape_data_lines(data_lines)

    if not data_lines:
        return False
    body = "# Netscape HTTP Cookie File\n" + "\n".join(data_lines) + "\n"
    try:
        out_path.write_text(body, encoding="utf-8")
    except OSError:
        return False
    raw = body.lower()
    if any(k in raw for k in ("s_v_web_id", "ttwid", "__ac_signature", "sessionid")):
        return True
    return "\t" in body


def merge_netscape_cookie_files(primary: Path, secondary: Path, out_path: Path) -> None:
    """合并两个 Netscape cookies.txt，同名同域以 primary 为准。"""
    def data_lines(p: Path) -> list[str]:
        if not p.is_file():
            return []
        out: list[str] = []
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            out.append(ln)
        return out

    seen: set[tuple[str, str]] = set()
    merged: list[str] = ["# Netscape HTTP Cookie File", ""]
    for ln in data_lines(primary) + data_lines(secondary):
        parts = ln.split("\t")
        if len(parts) >= 7:
            key = (parts[0], parts[5])
            if key in seen:
                continue
            seen.add(key)
        merged.append(ln)
    out_path.write_text("\n".join(merged) + "\n", encoding="utf-8")

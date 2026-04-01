from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx

from app import config
from app.douyin_guest import fetch_guest_cookie_file, merge_netscape_cookie_files


class YtDlpError(Exception):
    def __init__(self, message: str, stderr: str | None = None):
        super().__init__(message)
        self.stderr = stderr or ""


# Windows 下默认解码 cp936 会导致 yt-dlp 英文报错乱码，无法匹配关键词
_SUBPROC_TEXT = {"encoding": "utf-8", "errors": "replace"}


def _ytdlp_cmd() -> list[str]:
    return [sys.executable, "-m", "yt_dlp"]


def _normalize_url(url: str) -> str:
    """将带播放列表参数的 YouTube 观看页规范为单视频链接，避免误展开列表。"""
    u = url.strip()
    p = urlparse(u)
    host = (p.hostname or "").lower()
    if host in ("www.youtube.com", "youtube.com", "m.youtube.com"):
        q = parse_qs(p.query, keep_blank_values=False)
        vid_list = q.get("v")
        if vid_list and vid_list[0]:
            vid = vid_list[0]
            new_q = urlencode({"v": vid}, doseq=True)
            return urlunparse((p.scheme or "https", "www.youtube.com", "/watch", "", new_q, ""))
    return u


_BILI_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_BILI_REFERER = "https://www.bilibili.com/"

_XHS_PAGE_HEADERS = {
    "User-Agent": _BILI_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.xiaohongshu.com/",
}

_OG_IMAGE_RES = (
    re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.I),
)
_OG_TITLE_RES = (
    re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']', re.I),
)


def _looks_like_xhs_placeholder_title(title: str, note_id: str) -> bool:
    t = (title or "").strip()
    if not t or t == "未命名" or t == "视频":
        return True
    if re.match(r"^xiaohongshu_[\da-f]+$", t, re.I):
        return True
    if note_id and re.fullmatch(r"[\da-f]+", note_id, re.I):
        if t.replace("-", "").lower() == note_id.replace("-", "").lower():
            return True
        if t == f"xiaohongshu_{note_id}":
            return True
    if re.fullmatch(r"[\da-f]{20,}", t, re.I):
        return True
    return False


def _xhs_og_meta(page_url: str) -> dict[str, str | None]:
    """笔记页 HTML 中的 og:title / og:image，弥补接口里无封面或标题为 ID 的情况。"""
    out: dict[str, str | None] = {"title": None, "thumbnail": None}
    u = (page_url or "").strip()
    if "xiaohongshu.com" not in u.lower():
        return out
    try:
        with httpx.Client(timeout=22, follow_redirects=True, headers=_XHS_PAGE_HEADERS) as c:
            r = c.get(u)
        r.raise_for_status()
        body = r.text
    except Exception:
        return out
    for rx in _OG_IMAGE_RES:
        m = rx.search(body)
        if m:
            raw = html.unescape(m.group(1).strip())
            if raw.startswith("//"):
                raw = "https:" + raw
            if raw.startswith("http"):
                out["thumbnail"] = raw
            break
    for rx in _OG_TITLE_RES:
        m = rx.search(body)
        if m:
            t = html.unescape(m.group(1).strip())
            if t:
                out["title"] = t
            break
    return out


def _bilibili_http_headers() -> dict[str, str]:
    return {"User-Agent": _BILI_UA, "Referer": _BILI_REFERER}


def _expand_b23_url(url: str) -> str:
    p = urlparse(url.strip())
    h = (p.hostname or "").lower()
    if h not in ("b23.tv", "www.b23.tv"):
        return url.strip()
    try:
        with httpx.Client(timeout=15, follow_redirects=True) as c:
            r = c.get(url.strip(), headers=_bilibili_http_headers())
        return str(r.url)
    except Exception:
        return url.strip()


_DOUYIN_UA_HEADERS = {
    "User-Agent": _BILI_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _expand_douyin_url(url: str) -> str:
    """抖音短链 v.douyin.com 与 /share/video/ 规范为 www.douyin.com/video/{id}，供 yt-dlp Douyin 提取器匹配。"""
    u = url.strip()
    m = re.search(r"(?:www\.|m\.)?douyin\.com/share/video/(\d+)", u, re.I)
    if m:
        return f"https://www.douyin.com/video/{m.group(1)}"
    m = re.match(r"https?://m\.douyin\.com/video/(\d+)", u, re.I)
    if m:
        return f"https://www.douyin.com/video/{m.group(1)}"
    p = urlparse(u)
    h = (p.hostname or "").lower()
    if h not in ("v.douyin.com", "www.v.douyin.com"):
        return u
    try:
        hdrs = {**_DOUYIN_UA_HEADERS, "Referer": "https://www.douyin.com/"}
        with httpx.Client(timeout=30, follow_redirects=True) as c:
            r = c.get(u, headers=hdrs)
        final = str(r.url)
        vm = re.search(r"(?:www\.)?douyin\.com/video/(\d+)", final, re.I)
        if vm:
            return f"https://www.douyin.com/video/{vm.group(1)}"
    except Exception:
        pass
    return u


def normalize_fetch_url(url: str) -> str:
    """解析/下载前统一规范化（YouTube 单视频、B 站 av、抖音短链等）。"""
    u = _normalize_url(url.strip())
    u = _expand_douyin_url(u)
    u = _bilibili_bv_to_av_url(u)
    return u


def _bilibili_bvid_from_watch(url: str) -> str | None:
    p = urlparse(url)
    m = re.search(r"/video/(BV[\w]+)/?", p.path or "", re.I)
    if m:
        return m.group(1)
    q = parse_qs(p.query)
    bv = (q.get("bvid") or [None])[0]
    if bv and re.match(r"^BV[\w]+$", bv, re.I):
        return bv
    return None


def _bilibili_bv_to_av_url(url: str) -> str:
    """BV 观看页 HTML 在多地会返回 HTTP 412；公开 view API 仍可用，av 链接让 yt-dlp 走 API 分支。"""
    u = _expand_b23_url(url)
    p = urlparse(u)
    host = (p.hostname or "").lower()
    if "bilibili.com" not in host:
        return u
    if re.search(r"/video/av\d+", p.path or "", re.I):
        return u
    bvid = _bilibili_bvid_from_watch(u)
    if not bvid:
        return u
    try:
        with httpx.Client(timeout=20) as c:
            r = c.get(
                "https://api.bilibili.com/x/web-interface/view",
                params={"bvid": bvid},
                headers=_bilibili_http_headers(),
            )
            r.raise_for_status()
            body = r.json()
    except Exception:
        return u
    if body.get("code") != 0:
        return u
    aid = (body.get("data") or {}).get("aid")
    if aid is None:
        return u
    try:
        aid_int = int(aid)
    except (TypeError, ValueError):
        return u
    qs = parse_qs(p.query)
    p_part = (qs.get("p") or [None])[0]
    av = f"https://www.bilibili.com/video/av{aid_int}"
    if p_part and str(p_part).isdigit() and int(p_part) > 1:
        return f"{av}?p={int(p_part)}"
    return av


def _js_runtimes_cli() -> list[str]:
    if config.YTDLP_JS_RUNTIMES:
        return ["--js-runtimes", config.YTDLP_JS_RUNTIMES]
    if config.YTDLP_AUTO_JS_RUNTIME:
        for name in ("node", "deno", "bun"):
            if shutil.which(name):
                return ["--js-runtimes", name]
    return []


def _ytdlp_cookie_cli(url: str, tmp_cleanup: list[Path]) -> list[str]:
    """抖音：优先合并访客自动 Cookie + 用户 cookies.txt；其它站点沿用原逻辑。"""
    u = url.lower()
    if "douyin.com" in u and config.DOUYIN_GUEST_COOKIES:
        try:
            fd, guest_p = tempfile.mkstemp(suffix=".txt", prefix="dy_guest_")
            os.close(fd)
            gp = Path(guest_p)
            tmp_cleanup.append(gp)
            if fetch_guest_cookie_file(url, gp):
                user_p = Path(config.YTDLP_COOKIES_FILE).expanduser() if config.YTDLP_COOKIES_FILE else None
                if user_p and user_p.is_file():
                    fd2, merged_p = tempfile.mkstemp(suffix=".txt", prefix="dy_merged_")
                    os.close(fd2)
                    mp = Path(merged_p)
                    tmp_cleanup.append(mp)
                    merge_netscape_cookie_files(user_p, gp, mp)
                    return ["--cookies", str(mp)]
                return ["--cookies", str(gp)]
        except OSError:
            pass

    out: list[str] = []
    if config.YTDLP_COOKIES_FILE:
        cpath = Path(config.YTDLP_COOKIES_FILE).expanduser()
        if cpath.is_file():
            out.extend(["--cookies", str(cpath)])
    if config.YTDLP_COOKIES_FROM_BROWSER:
        out.extend(["--cookies-from-browser", config.YTDLP_COOKIES_FROM_BROWSER])
    return out


def _global_ytdlp_opts(url: str, tmp_cleanup: list[Path]) -> list[str]:
    opts: list[str] = ["--no-playlist"]
    opts.extend(_ytdlp_cookie_cli(url, tmp_cleanup))
    opts.extend(_js_runtimes_cli())

    if config.YTDLP_EXTRACTOR_ARGS:
        opts.extend(["--extractor-args", config.YTDLP_EXTRACTOR_ARGS])
    if config.POT_PROVIDER_URL:
        opts.extend(["--extractor-args", "youtube:player_client=web"])
        opts.extend(["--extractor-args", f"youtubepot-bgutilhttp:base_url={config.POT_PROVIDER_URL}"])

    opts.extend(config.YTDLP_EXTRA_ARGS)
    return opts


_GENERIC_RESOLVE_FAIL = "无法解析该链接，请检查链接是否正确"

# 抖音 Cookie 类失败统一口径：避免前端仍显示「必须浏览器扩展导出」等历史/误传文案
_DOUYIN_COOKIE_GUIDANCE = (
    "抖音暂无法解析：服务端正自动获取访客会话（curl-cffi、Playwright/Chromium）。"
    "请稍后重试，或在项目根目录执行 docker compose build --no-cache 后重启。"
    "访客不必安装扩展或导出 Cookie；站长可选在 cookies/cookies.txt 增加访客态条目以提升稳定性。"
)

# 历史/第三方返回的「必须浏览器扩展导出」类文案（仓库内可能已无源字符串，部署或 Cobalt 仍可能带回）
_LEGACY_DOUYIN_COOKIE_UI_MARKERS = (
    "抖音需要",
    "浏览器扩展",
    "有效的网页",
    "放入项目",
    "cookies/ 目录",
    "get cookies.txt",
    "cclelndahbckbenkjhflpdbgdldlbecc",
    "s_v_web_id 的访客",
    "导出 douyin.com",
    "douyin.com 的 cookies",
)


def sanitize_douyin_resolve_user_detail(url: str, detail: str) -> str:
    """抖音相关链接：把遗留的「必须扩展导出 cookies」提示统一替换为当前产品口径。"""
    h = (urlparse(url or "").hostname or "").lower()
    if "douyin" not in h:
        return detail
    t = (detail or "").strip()
    if not t:
        return t
    if any(m in t for m in _LEGACY_DOUYIN_COOKIE_UI_MARKERS):
        return _DOUYIN_COOKIE_GUIDANCE
    return t


def _ytdlp_subprocess_error_text(proc: Any) -> str:
    """yt-dlp 常把 ERROR 打在 stdout，stderr 为空，需合并后再匹配关键词。"""
    chunks: list[str] = []
    if getattr(proc, "stderr", None) and str(proc.stderr).strip():
        chunks.append(str(proc.stderr).strip())
    if getattr(proc, "stdout", None) and str(proc.stdout).strip():
        chunks.append(str(proc.stdout).strip())
    if not chunks:
        return ""
    return "\n".join(chunks)[-4000:]


def _friendly_fail_message(stderr: str) -> str:
    text = stderr or ""
    low = text.lower()
    # Douyin 提取器在 yt-dlp 内的固定报错文案
    if "failed to download web detail" in low:
        return _DOUYIN_COOKIE_GUIDANCE
    # 抖音：匹配 [Douyin]、中文「抖音」、旧版/误传文案里的「扩展、有效网页 Cookie」等
    douyin_ctx = (
        "douyin" in low
        or "抖音" in text
        or "douyin.com" in low
        or "[douyin]" in low
    )
    cookieish = (
        "cookie" in low
        or "cookies" in low
        or "s_v_web_id" in low
        or "fresh cookies" in low
        or "浏览器扩展" in text
        or "有效的网页" in text
        or ("cookies.txt" in text and "抖音" in text)
    )
    if douyin_ctx and cookieish:
        return _DOUYIN_COOKIE_GUIDANCE
    if "sign in" in low or "not a bot" in low:
        return "该平台要求验证身份，暂时无法解析此链接"
    if "no supported javascript runtime" in low or "js runtime" in low:
        return "服务端缺少运行环境，请联系管理员"
    if "ffmpeg" in low and ("not found" in low or "not recognized" in low or "winerror 2" in low):
        return "服务端缺少视频处理组件，请联系管理员"
    if "login" in low or "authenticate" in low:
        return "该视频需要登录才能访问"
    if "private" in low:
        return "该视频为私密内容，无法下载"
    if "copyright" in low or "blocked" in low:
        return "该视频因版权限制不可用"
    if "geo" in low or "not available in your" in low:
        return "该视频存在地区限制"
    if "412" in low or "precondition failed" in low:
        return "B 站暂时拦截了该链接（HTTP 412），请稍后重试；会员或高码率稿件可尝试在 cookies.txt 中配置登录 Cookie"
    if "http error 403" in low or " 403 " in low or "status code: 403" in low or "forbidden" in low:
        return "上游拒绝访问（403），常见原因：需登录 Cookie、地区或防盗链限制"
    if "http error 429" in low or "too many requests" in low or " 429 " in low:
        return "请求过于频繁（429），请稍后再试"
    if "unable to extract" in low or "no suitable extractors" in low or "unsupported url" in low:
        return "无法从该链接提取视频，请使用作品页地址栏里的完整链接（勿用手机 App 私有分享格式）"
    if "this video is not available" in low or "video unavailable" in low:
        return "该视频已删除、设为私密或在你所在地区不可用"
    return _GENERIC_RESOLVE_FAIL


def _is_youtube_url(url: str) -> bool:
    u = url.lower()
    return "youtube.com" in u or "youtu.be" in u


def _url_platform_hint(url: str) -> str:
    u = (url or "").lower()
    if "douyin.com" in u:
        return "提示：访客无需登录或导出 Cookie。服务端会用 curl_cffi、无头 Chromium 等自动生成访客会话；失败时可由站长可选配置 cookies/cookies.txt 并重建镜像。"
    if "xiaohongshu.com" in u:
        return "提示：小红书可尝试配置 xiaohongshu.com 的 cookies.txt，或使用标准笔记链接。"
    if "youtube.com" in u or "youtu.be" in u:
        return "提示：YouTube 若遇机器人验证，请将 youtube.com 的 cookies.txt 放入 cookies/。"
    if "bilibili.com" in u or "b23.tv" in u:
        return "提示：B 站 412 或风控时可尝试在 cookies.txt 中配置登录态。"
    if "tiktok.com" in u:
        return "提示：TikTok 部分视频需稳定网络；若仅音频可换链或稍后再试。"
    return "提示：请确认链接为浏览器打开作品页时的地址；已部署 Docker 时请重建镜像以使用最新逻辑。"


def public_resolve_error_detail(original_url: str, exc: YtDlpError | None) -> str:
    """面向终端用户的错误文案，不暴露技术细节。"""
    if exc is None:
        h = _url_platform_hint(original_url)
        out = f"{_GENERIC_RESOLVE_FAIL} {h}" if h else _GENERIC_RESOLVE_FAIL
        return sanitize_douyin_resolve_user_detail(original_url, out)
    msg = str(exc)
    if any(x in msg for x in ("仅支持 http", "链接过长", "无效的链接", "不允许访问")):
        return sanitize_douyin_resolve_user_detail(original_url, msg)
    raw = (exc.stderr or "").strip()
    # 同时看 yt-dlp 输出与异常文案，避免 stderr 为空时仍把旧版「浏览器扩展」提示原样返回
    blob = f"{raw}\n{msg}".strip()
    detail = _friendly_fail_message(blob)
    if detail == _GENERIC_RESOLVE_FAIL:
        detail = _friendly_fail_message(msg) if msg != blob else detail
    if detail == _GENERIC_RESOLVE_FAIL:
        h = _url_platform_hint(original_url)
        if h:
            detail = f"{detail} {h}"
    u = (original_url or "").lower()
    if "douyin.com" in u:
        tail = f"{blob}\n{detail}".lower()
        if any(s in tail for s in ("浏览器扩展", "get cookies.txt", "有效的网页", "cclelndahbckbenkjhflpdbgdldlbecc")):
            detail = _DOUYIN_COOKIE_GUIDANCE
    return sanitize_douyin_resolve_user_detail(original_url, detail)


# 默认合并链在 TikTok 等站点常失败（仅有单一混流或无独立音轨），末尾加 worst 保证可选中格式
_FORMAT_FALLBACK = (
    "bv*+ba/bestvideo*+bestaudio/bestvideo+bestaudio/bv+ba/best/worst"
)

_MEDIA_EXTS = frozenset({
    ".mp4",
    ".webm",
    ".mkv",
    ".m4a",
    ".opus",
    ".mov",
    ".flv",
    ".mp3",
    ".m4v",
    ".avi",
})


def _pick_latest_media_file(dest_dir: Path) -> Path | None:
    """选取目录内最新生成的媒体文件（排除 .part 等临时文件）。"""
    try:
        entries = list(dest_dir.iterdir())
    except OSError:
        return None
    files: list[Path] = []
    for p in entries:
        if not p.is_file():
            continue
        name = p.name.lower()
        if name.endswith(".part") or name.endswith(".ytdl") or name.endswith(".temp"):
            continue
        if p.suffix.lower() not in _MEDIA_EXTS:
            continue
        files.append(p)
    if not files:
        return None
    return max(files, key=lambda x: x.stat().st_mtime)


def validate_public_url(url: str) -> None:
    u = url.strip()
    if len(u) > config.MAX_URL_BYTES:
        raise YtDlpError("链接过长")
    parsed = urlparse(u)
    if parsed.scheme not in ("http", "https"):
        raise YtDlpError("仅支持 http/https 链接")
    host = (parsed.hostname or "").lower()
    if not host:
        raise YtDlpError("无效的链接")
    if not config.ALLOW_LOCAL_NETWORK:
        if host in ("localhost", "127.0.0.1", "::1"):
            raise YtDlpError("不允许访问本地地址")
        if re.match(r"^127\.\d+\.\d+\.\d+$", host):
            raise YtDlpError("不允许访问本地地址")
        if host.endswith(".local"):
            raise YtDlpError("不允许访问该主机名")
        if host.startswith("192.168."):
            raise YtDlpError("不允许访问内网地址")
        if host.startswith("10."):
            raise YtDlpError("不允许访问内网地址")
        if host.startswith("172."):
            parts = host.split(".")
            if len(parts) >= 2 and parts[1].isdigit():
                second = int(parts[1])
                if 16 <= second <= 31:
                    raise YtDlpError("不允许访问内网地址")


def fetch_metadata(url: str) -> dict[str, Any]:
    url = normalize_fetch_url(url)
    validate_public_url(url)
    tmp_cookie_files: list[Path] = []
    try:
        cmd = [
            *_ytdlp_cmd(),
            *_global_ytdlp_opts(url, tmp_cookie_files),
            "-f",
            _FORMAT_FALLBACK,
            "-J",
            "--skip-download",
            "--no-warnings",
            url,
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=config.RESOLVE_TIMEOUT,
                check=False,
                **_SUBPROC_TEXT,
            )
        except subprocess.TimeoutExpired as e:
            raise YtDlpError("解析超时，请稍后重试或换一条链接") from e
        if proc.returncode != 0:
            err = _ytdlp_subprocess_error_text(proc) or "(yt-dlp 无输出)"
            raise YtDlpError(_friendly_fail_message(err), stderr=err)
        try:
            data: dict[str, Any] = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise YtDlpError("解析返回异常") from e
        return data
    finally:
        for p in tmp_cookie_files:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def simplify_formats(meta: dict[str, Any], limit: int = 10) -> list[dict[str, Any]]:
    formats = meta.get("formats") or []
    rows: list[dict[str, Any]] = []
    for f in formats:
        fid = f.get("format_id")
        if fid is None:
            continue
        vcodec = f.get("vcodec") or "none"
        acodec = f.get("acodec") or "none"
        if vcodec == "none" and acodec == "none":
            continue
        if vcodec != "none" and acodec != "none":
            kind = "muxed"
        elif vcodec != "none":
            kind = "video"
        else:
            kind = "audio"
        height = f.get("height") or 0
        rows.append(
            {
                "format_id": str(fid),
                "ext": f.get("ext") or "",
                "height": height,
                "vcodec": vcodec,
                "acodec": acodec,
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "tbr": f.get("tbr"),
                "kind": kind,
            }
        )

    def combined_score(r: dict[str, Any]) -> tuple[int, int]:
        v = r["vcodec"] != "none"
        a = r["acodec"] != "none"
        both = 2 if v and a else (1 if v else 0)
        return (both, int(r["height"] or 0))

    rows.sort(key=combined_score, reverse=True)
    merged: list[dict[str, Any]] = [
        {
            "format_id": "best",
            "label": "默认最佳（推荐）",
            "ext": "mp4",
            "height": None,
            "kind": "muxed",
            "filesize": None,
        }
    ]
    seen: set[str] = {"best"}
    for r in rows:
        fid = r["format_id"]
        if fid in seen:
            continue
        seen.add(fid)
        label_parts = [fid]
        if r["height"]:
            label_parts.append(f"{r['height']}p")
        if r["ext"]:
            label_parts.append(r["ext"])
        merged.append(
            {
                **r,
                "label": " · ".join(label_parts[1:]) if len(label_parts) > 1 else fid,
            }
        )
        if len(merged) >= limit + 1:
            break
    return merged


def build_resolve_response(meta: dict[str, Any]) -> dict[str, Any]:
    thumb = meta.get("thumbnail")
    if not thumb and isinstance(meta.get("thumbnails"), list) and meta["thumbnails"]:
        thumbs = [t for t in meta["thumbnails"] if isinstance(t, dict) and t.get("url")]
        if thumbs:
            thumb = max(
                thumbs,
                key=lambda t: (t.get("width") or 0) * (t.get("height") or 0),
            ).get("url")

    page_url = meta.get("webpage_url") or meta.get("original_url")
    title = meta.get("title") or "未命名"
    note_id = str(meta.get("id") or "")
    if page_url and "xiaohongshu.com" in page_url.lower():
        need_og = (not thumb) or _looks_like_xhs_placeholder_title(title, note_id)
        if need_og:
            og = _xhs_og_meta(page_url)
            if not thumb and og.get("thumbnail"):
                thumb = og["thumbnail"]
            if og.get("title") and _looks_like_xhs_placeholder_title(title, note_id):
                title = og["title"]

    formats = simplify_formats(meta)
    fapprox = meta.get("filesize") or meta.get("filesize_approx")
    if not fapprox:
        for row in formats:
            sz = row.get("filesize")
            if sz:
                fapprox = sz
                break
    return {
        "title": title,
        "thumbnail": thumb,
        "duration": meta.get("duration"),
        "webpage_url": meta.get("webpage_url") or meta.get("original_url"),
        "extractor": meta.get("extractor") or meta.get("ie_key"),
        "formats": formats,
        "filesize_approx": fapprox,
    }


_FORMAT_AUDIO = "ba/bestaudio/ba*/best/worst"
_FORMAT_VIDEO_ONLY = "bv/bestvideo*/bv*/best/worst"


def download_to_dir(
    url: str,
    dest_dir: Path,
    format_spec: str | None,
    *,
    media_mode: str = "original",
    merge_container: str = "mp4",
) -> Path:
    url = normalize_fetch_url(url)
    validate_public_url(url)
    dest_dir.mkdir(parents=True, exist_ok=True)
    # 用标题 + 视频 ID，避免浏览器保存时全是 video.mp4 互相覆盖（不用 []，避免与 yt-dlp 可选片段语法混淆）
    out_tmpl = str(dest_dir / "%(title)s_%(id)s.%(ext)s")
    merge_container = (merge_container or "mp4").lower()
    if merge_container not in ("mp4", "webm", "mkv"):
        merge_container = "mp4"

    if media_mode == "audio_only":
        if not format_spec or format_spec in ("best", "cobalt"):
            fmt = _FORMAT_AUDIO
        else:
            fmt = format_spec
    elif media_mode == "video_only":
        if not format_spec or format_spec in ("best", "cobalt"):
            fmt = _FORMAT_VIDEO_ONLY
        else:
            fmt = format_spec
    elif not format_spec or format_spec in ("best", "cobalt"):
        fmt = _FORMAT_FALLBACK
    else:
        fmt = format_spec

    tmp_cookie_files: list[Path] = []
    try:
        cmd = [*_ytdlp_cmd(), *_global_ytdlp_opts(url, tmp_cookie_files)]
        if media_mode != "audio_only":
            cmd.extend(["--merge-output-format", merge_container])
        else:
            # 仅音频：转码为 mp3（需 FFmpeg），避免仍是 m4a/webm 等「视频容器」
            cmd.extend(["--extract-audio", "--audio-format", "mp3"])
        cmd.extend(
            [
                "-f",
                fmt,
                "--restrict-filenames",
                "--trim-filenames",
                "100",
                "--no-warnings",
                "-o",
                out_tmpl,
                url,
            ]
        )
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=config.DOWNLOAD_TIMEOUT,
                check=False,
                **_SUBPROC_TEXT,
            )
        except subprocess.TimeoutExpired as e:
            raise YtDlpError("下载超时") from e
        if proc.returncode != 0:
            err = _ytdlp_subprocess_error_text(proc) or "(yt-dlp 无输出)"
            raise YtDlpError(_friendly_fail_message(err) or "下载失败", stderr=err)

        picked = _pick_latest_media_file(dest_dir)
        if not picked:
            raise YtDlpError("未找到输出文件")
        return picked
    finally:
        for p in tmp_cookie_files:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

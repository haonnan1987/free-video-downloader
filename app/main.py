from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import re
from typing import Literal
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import config
from app.cobalt import CobaltError, cobalt_resolve
from app.diagnostics import get_diagnostics
from app.jobs import run_download_job, store
from app.ytdlp import (
    YtDlpError,
    build_resolve_response,
    fetch_metadata,
    normalize_fetch_url,
    public_resolve_error_detail,
    sanitize_douyin_resolve_user_detail,
)

_log = logging.getLogger("uvicorn.error")


def _prefer_ytdlp_before_cobalt(url: str) -> bool:
    """这些站点在 Cobalt 之前优先 yt-dlp。抖音与 TikTok 一样先 Cobalt，失败再用 yt-dlp（并带访客 Cookie 补强）。"""
    host = (urlparse(url.strip()).hostname or "").lower()
    if not host:
        return False
    if "xiaohongshu.com" in host:
        return True
    if host == "b23.tv" or host.endswith(".b23.tv"):
        return True
    return host.endswith(".bilibili.com") or host == "bilibili.com"


_THUMB_PROXY_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "Chrome/131.0.0.0 Safari/537.36"
)
_THUMB_MAX_BYTES = 6 * 1024 * 1024


def _is_xhs_thumbnail_cdn_host(host: str) -> bool:
    """小红书封面除 xhscdn 外，常见落在 picasso-static / qimg 等子域。"""
    h = (host or "").lower().rstrip(".")
    if not h or "." not in h:
        return False
    if h.endswith(".xhscdn.com") or h == "xhscdn.com" or h.endswith(".xhscdn.net") or h == "xhscdn.net":
        return True
    if not h.endswith(".xiaohongshu.com"):
        return False
    if h in ("www.xiaohongshu.com", "xiaohongshu.com"):
        return False
    sub0 = h.split(".")[0]
    if sub0 in (
        "picasso-static",
        "qimg",
        "fe-static",
        "ci",
        "edith",
        "sns-avatar-qc",
    ):
        return True
    if sub0.startswith(("picasso", "sns-", "fe-", "lf-", "apm-")):
        return True
    return False


def _normalize_thumbnail_url(thumb: str | None) -> str | None:
    if not thumb:
        return None
    t = thumb.strip()
    if t.startswith("//"):
        return "https:" + t
    return t


_TW_PAGE_HOSTS = frozenset({
    "x.com",
    "www.x.com",
    "mobile.x.com",
    "twitter.com",
    "www.twitter.com",
    "mobile.twitter.com",
})


def _is_twitter_x_page_url(page_url: str) -> bool:
    h = (urlparse(page_url).hostname or "").lower().rstrip(".")
    if h in _TW_PAGE_HOSTS:
        return True
    return h.endswith(".twitter.com")


def _twitter_syndication_thumbnail(page_url: str) -> str | None:
    """视频推文：publish.twitter.com oEmbed 常无 thumbnail_url，用官方 syndication JSON 取封面。"""
    if not _is_twitter_x_page_url(page_url):
        return None
    m = re.search(r"/status/(\d+)", page_url)
    if not m:
        return None
    tid = m.group(1)
    api = f"https://cdn.syndication.twimg.com/tweet-result?id={tid}&token=0&lang=en"
    try:
        with httpx.Client(timeout=14, headers={"User-Agent": _THUMB_PROXY_UA}) as c:
            r = c.get(api)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:
        return None
    video = data.get("video")
    if isinstance(video, dict):
        poster = video.get("poster")
        if isinstance(poster, str) and poster.startswith("http") and "twimg.com" in poster:
            return poster
    for md in data.get("mediaDetails") or []:
        if not isinstance(md, dict):
            continue
        if md.get("type") == "video":
            u = md.get("media_url_https")
            if isinstance(u, str) and u.startswith("http") and "twimg.com" in u:
                return u
    return None


def _sniff_image_media_type(body: bytes) -> str | None:
    if len(body) >= 3 and body[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(body) >= 8 and body[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    return None


def _thumb_host_allowlisted(host: str) -> bool:
    h = host.lower().rstrip(".")
    if not h or "." not in h:
        return False
    suffixes = (
        "tiktokcdn.com",
        "tiktokcdn-us.com",
        "tiktokcdn-eu.com",
        "tiktokv.com",
        "ibyteimg.com",
        "byteimg.com",
        "tiktok.com",
        "ytimg.com",
        "ggpht.com",
        "googleusercontent.com",
        "vimeocdn.com",
        "fbcdn.net",
        "cdninstagram.com",
        "hdslb.com",  # B 站封面 CDN（浏览器 Referer 非 bilibili 时 403）
        "biliimg.com",  # 如 archive.biliimg.com
        "xhscdn.com",  # 小红书封面（与 _is_xhs_thumbnail_cdn_host 重叠，保留兼容）
        "douyinpic.com",  # 抖音封面 CDN
        "twimg.com",  # X/Twitter 推文卡片图（pbs.twimg.com 等），直连易被 403
    )
    if _is_xhs_thumbnail_cdn_host(h):
        return True
    return any(h == s or h.endswith("." + s) for s in suffixes)


def _needs_thumbnail_proxy(host: str) -> bool:
    """TikTok 等 CDN 常拒绝浏览器从第三方站点带上的 Referer，需服务端代拉。"""
    h = host.lower()
    return (
        "tiktokcdn" in h
        or h.endswith(".tiktok.com")
        or "ibyteimg.com" in h
        or "byteimg.com" in h
        or "tiktokv.com" in h
        or h.endswith(".hdslb.com")
        or h == "hdslb.com"
        or h.endswith(".biliimg.com")
        or h == "biliimg.com"
        or h.endswith(".xhscdn.com")
        or h == "xhscdn.com"
        or h.endswith(".xhscdn.net")
        or h == "xhscdn.net"
        or _is_xhs_thumbnail_cdn_host(h)
        or "douyinpic.com" in h
        or "twimg.com" in h
    )


def _split_thumbnail_for_client(thumb: str | None) -> tuple[str | None, str | None]:
    """需代理的长签名 CDN 图：thumbnail 置空，由 thumbnail_proxy_url + POST /api/thumb 拉取（避免 GET 超长、<img> 限长）。"""
    thumb = _normalize_thumbnail_url(thumb)
    if not thumb:
        return None, None
    if thumb.startswith("/"):
        return thumb, None
    if not thumb.startswith(("http://", "https://")):
        return thumb, None
    p = urlparse(thumb)
    host = (p.hostname or "").lower()
    if not _thumb_host_allowlisted(host) or not _needs_thumbnail_proxy(host):
        return thumb, None
    return None, thumb


def _with_proxied_thumbnail(payload: dict) -> dict:
    out = {**payload}
    direct, prox = _split_thumbnail_for_client(out.get("thumbnail"))
    out["thumbnail"] = direct
    if prox:
        out["thumbnail_proxy_url"] = prox
    else:
        out.pop("thumbnail_proxy_url", None)
    return out


async def _resolve_embed_hotlink_thumbnail(payload: dict) -> dict:
    """B 站 / 小红书等防盗链 CDN：解析阶段代拉封面并内嵌为 data URL。"""
    raw = _normalize_thumbnail_url(payload.get("thumbnail_proxy_url") or payload.get("thumbnail"))
    if not raw or not raw.startswith("http"):
        return payload
    host = (urlparse(raw).hostname or "").lower()
    w = (payload.get("webpage_url") or "").lower()
    ex = (payload.get("extractor") or "").lower()

    headers = {
        "User-Agent": _THUMB_PROXY_UA,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    if host.endswith(".hdslb.com") or host == "hdslb.com" or host.endswith(".biliimg.com") or host == "biliimg.com":
        if "bilibili.com" not in w and "b23.tv" not in w and "bili" not in ex:
            return payload
        headers["Referer"] = "https://www.bilibili.com/"
        headers["Origin"] = "https://www.bilibili.com"
    elif _is_xhs_thumbnail_cdn_host(host):
        if "xiaohongshu.com" not in w and "xiaohongshu" not in ex:
            return payload
        headers["Referer"] = "https://www.xiaohongshu.com/"
        headers["Origin"] = "https://www.xiaohongshu.com"
        headers["Accept-Language"] = "zh-CN,zh;q=0.9,en;q=0.8"
    elif "douyinpic.com" in host or ("byteimg.com" in host and "douyin" in raw.lower()):
        if "douyin.com" not in w and "douyin" not in ex:
            return payload
        headers["Referer"] = "https://www.douyin.com/"
        headers["Origin"] = "https://www.douyin.com"
        headers["Accept-Language"] = "zh-CN,zh;q=0.9,en;q=0.8"
    elif "twimg.com" in host:
        if "twitter.com" not in w and "x.com" not in w and "twitter" not in ex:
            return payload
        headers["Referer"] = "https://x.com/"
        headers["Origin"] = "https://x.com"
    else:
        return payload

    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=headers) as c:
            r = await c.get(raw)
    except httpx.RequestError as e:
        _log.warning("hotlink embed thumb: %s", e)
        return payload
    if r.status_code >= 400:
        return payload
    body = r.content
    if len(body) > _THUMB_MAX_BYTES:
        return payload
    ct = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not ct.startswith("image/"):
        sniff = _sniff_image_media_type(body)
        if sniff:
            ct = sniff
        else:
            return payload
    try:
        b64 = base64.standard_b64encode(body).decode("ascii")
    except Exception:
        return payload
    out = {**payload, "thumbnail": f"data:{ct};base64,{b64}"}
    out.pop("thumbnail_proxy_url", None)
    return out


async def finalize_resolve_payload(payload: dict) -> dict:
    """统一补 X/Twitter 视频封面（oEmbed 常无图），再拆分代理 / 服务端内嵌 data URL。"""
    p = {**payload}
    wu = (p.get("webpage_url") or "").strip()
    cur_thumb = (p.get("thumbnail") or "").strip()
    if wu and _is_twitter_x_page_url(wu) and not cur_thumb:
        t = await asyncio.to_thread(_twitter_syndication_thumbnail, wu)
        if t:
            p["thumbnail"] = t
    p = _with_proxied_thumbnail(p)
    return await _resolve_embed_hotlink_thumbnail(p)


def _is_technical_cobalt_display_title(name: str) -> bool:
    """Cobalt 返回的文件名/占位标题，应用 oEmbed 真人标题与封面。"""
    s = name.strip()
    if not s or s == "视频":
        return True
    if _COBALT_FILENAME_RE.match(s):
        return True
    if re.match(
        r"^(tiktok|youtube|twitter|instagram|bilibili|vimeo|reddit|facebook|x|xiaohongshu|douyin)_",
        s,
        re.I,
    ) and re.search(r"\d{8,}", s):
        return True
    return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    _log.info(
        "COBALT_API_URL=%s | COBALT_ENABLED=%s | COOKIES_FROM_BROWSER=%s",
        config.COBALT_API_URL or "(未设置)",
        config.COBALT_ENABLED,
        config.YTDLP_COOKIES_FROM_BROWSER or "(未设置)",
    )

    async def sweeper() -> None:
        while True:
            await asyncio.sleep(config.CLEANUP_INTERVAL_SECONDS)
            await store.cleanup_expired()

    task = asyncio.create_task(sweeper())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


fastapi_app = FastAPI(
    title="Video Fetch",
    version="0.1.0",
    **({"lifespan": lifespan} if not config.IS_VERCEL else {}),
)
fastapi_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ResolveBody(BaseModel):
    url: str = Field(..., min_length=4)


class DownloadBody(BaseModel):
    url: str = Field(..., min_length=4)
    format_id: str | None = Field(default=None, description="best 或具体 format_id")
    media_mode: Literal["original", "video_only", "audio_only"] = "original"
    encoding: Literal["auto", "mp4", "webm"] = "auto"


class ThumbProxyBody(BaseModel):
    url: str = Field(..., min_length=12, max_length=65536)


@fastapi_app.get("/api/diagnostics")
async def api_diagnostics():
    """自检 Cookie / FFmpeg / JS 运行时配置（不含密钥内容）。"""
    return get_diagnostics()


async def _thumb_proxy_fetch(raw: str) -> Response:
    """从白名单 CDN 拉取缩略图字节（供 GET/POST 共用）。"""
    u = raw.strip()
    if not u.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="无效的 URL")
    parsed = urlparse(u)
    host = (parsed.hostname or "").lower()
    if not _thumb_host_allowlisted(host):
        raise HTTPException(status_code=400, detail="不允许的缩略图域名")

    headers = {
        "User-Agent": _THUMB_PROXY_UA,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    lowu = u.lower()
    if "douyinpic.com" in host:
        headers["Referer"] = "https://www.douyin.com/"
        headers["Origin"] = "https://www.douyin.com"
        headers["Accept-Language"] = "zh-CN,zh;q=0.9,en;q=0.8"
    elif "byteimg.com" in host and "douyin" in lowu:
        headers["Referer"] = "https://www.douyin.com/"
        headers["Origin"] = "https://www.douyin.com"
        headers["Accept-Language"] = "zh-CN,zh;q=0.9,en;q=0.8"
    elif "tiktok" in host or "byteimg" in host:
        headers["Referer"] = "https://www.tiktok.com/"
    elif "hdslb.com" in host or "biliimg.com" in host:
        headers["Referer"] = "https://www.bilibili.com/"
        headers["Origin"] = "https://www.bilibili.com"
    elif _is_xhs_thumbnail_cdn_host(host):
        headers["Referer"] = "https://www.xiaohongshu.com/"
        headers["Origin"] = "https://www.xiaohongshu.com"
        headers["Accept-Language"] = "zh-CN,zh;q=0.9,en;q=0.8"
    elif "twimg.com" in host:
        headers["Referer"] = "https://x.com/"
        headers["Origin"] = "https://x.com"

    try:
        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers=headers,
        ) as c:
            r = await c.get(u)
    except httpx.RequestError as e:
        _log.warning("thumb proxy: request failed: %s", e)
        raise HTTPException(status_code=502, detail="缩略图拉取失败") from e

    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"上游 HTTP {r.status_code}")

    body = r.content
    if len(body) > _THUMB_MAX_BYTES:
        raise HTTPException(status_code=502, detail="图片过大")

    ct = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not ct.startswith("image/"):
        sniff = _sniff_image_media_type(body)
        if sniff:
            ct = sniff
        else:
            raise HTTPException(status_code=502, detail="非图片响应")

    return Response(
        content=body,
        media_type=ct,
        headers={"Cache-Control": "private, max-age=300"},
    )


@fastapi_app.get("/api/thumb")
async def api_thumb_proxy_get(
    url: str = Query(..., min_length=12, max_length=65536, description="原始缩略图 URL"),
):
    """兼容短链；TikTok 等长 URL 请用 POST /api/thumb。"""
    return await _thumb_proxy_fetch(url)


@fastapi_app.post("/api/thumb")
async def api_thumb_proxy_post(body: ThumbProxyBody):
    """POST body 传完整封面 URL，避免查询串超长导致 <img> / 浏览器截断。"""
    return await _thumb_proxy_fetch(body.url)


_OEMBED_ENDPOINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"tiktok\.com/", re.I), "https://www.tiktok.com/oembed?url={url}"),
    (re.compile(r"(youtube\.com/|youtu\.be/)", re.I), "https://www.youtube.com/oembed?url={url}&format=json"),
    (re.compile(r"(twitter\.com/|x\.com/)", re.I), "https://publish.twitter.com/oembed?url={url}"),
    (re.compile(r"vimeo\.com/", re.I), "https://vimeo.com/api/oembed.json?url={url}"),
    (re.compile(r"instagram\.com/", re.I), "https://graph.facebook.com/v18.0/instagram_oembed?url={url}&access_token=IGQVJ"),
    (re.compile(r"dailymotion\.com/", re.I), "https://www.dailymotion.com/services/oembed?url={url}&format=json"),
]


async def _fetch_oembed(url: str) -> dict[str, str | None]:
    """Best-effort oEmbed metadata: returns {title, thumbnail}."""
    for pattern, endpoint_tpl in _OEMBED_ENDPOINTS:
        if pattern.search(url):
            endpoint = endpoint_tpl.format(url=httpx.URL(url))
            try:
                async with httpx.AsyncClient(
                    timeout=12,
                    headers={"User-Agent": _THUMB_PROXY_UA},
                ) as c:
                    r = await c.get(str(endpoint))
                if r.status_code == 200:
                    data = r.json()
                    return {
                        "title": data.get("title") or data.get("author_name"),
                        "thumbnail": data.get("thumbnail_url"),
                    }
            except Exception:
                pass
            break
    return {"title": None, "thumbnail": None}


def _extract_thumbnail_from_url(url: str) -> str | None:
    """Fast thumbnail extraction for YouTube/Bilibili (no HTTP call needed)."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if host in ("www.youtube.com", "youtube.com", "m.youtube.com"):
        vid = parse_qs(parsed.query).get("v", [None])[0]
        if vid:
            return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    elif host == "youtu.be":
        vid = parsed.path.strip("/").split("/")[0] if parsed.path else None
        if vid:
            return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

    return None


_COBALT_FILENAME_RE = re.compile(
    r'^(youtube|twitter|tiktok|instagram|bilibili|vimeo|reddit|facebook|x|xiaohongshu|douyin)_'
    r'[A-Za-z0-9_-]{4,}$',
    re.I,
)


def _humanize_cobalt_title(filename: str | None) -> str | None:
    """Return a human title from cobalt filename, or None if it's just a technical ID."""
    if not filename:
        return None
    name = filename.rsplit(".", 1)[0] if "." in filename else filename
    name = re.sub(r'_\d{3,4}x\d{3,4}_\w+$', '', name)
    name = name.strip(" _-")
    if not name or _COBALT_FILENAME_RE.match(name):
        return None
    return name


async def _cobalt_resolve_response(url: str, cb: dict) -> dict:
    thumb = _extract_thumbnail_from_url(url)
    title = _humanize_cobalt_title(cb.get("filename"))
    if title and _is_technical_cobalt_display_title(title):
        title = None

    needs_oembed = not thumb or not title
    if needs_oembed:
        oembed = await _fetch_oembed(url)
        if not thumb and oembed["thumbnail"]:
            thumb = oembed["thumbnail"]
        if not title and oembed["title"]:
            title = oembed["title"]

    payload = await finalize_resolve_payload(
        {
            "title": title or "视频",
            "thumbnail": thumb,
            "duration": None,
            "webpage_url": url,
            "extractor": "cobalt",
            "formats": [
                {
                    "format_id": "cobalt",
                    "label": "cobalt 默认画质",
                    "ext": "mp4",
                    "height": None,
                    "kind": "muxed",
                    "filesize": None,
                }
            ],
            "filesize_approx": None,
            "_cobalt": True,
        }
    )
    # Cobalt 无体积信息：补跑 yt-dlp 元数据用于估算 MB；小红书等同时补强真实标题/封面
    try:
        meta = await asyncio.to_thread(fetch_metadata, url)
        br = build_resolve_response(meta)
        fa = meta.get("filesize") or meta.get("filesize_approx")
        if not fa:
            for f in meta.get("formats") or []:
                if not isinstance(f, dict):
                    continue
                sz = f.get("filesize") or f.get("filesize_approx")
                if sz:
                    fa = sz
                    break
        if fa:
            payload["filesize_approx"] = fa
            fmts = payload.get("formats") or []
            if fmts and fmts[0].get("format_id") == "cobalt":
                fmts[0] = {**fmts[0], "filesize": fa}
        lowu = url.lower()
        if (
            "xiaohongshu.com" in lowu
            or "douyin.com" in lowu
            or _is_twitter_x_page_url(url)
        ):
            enriched = False
            t = (br.get("title") or "").strip()
            if t and t != "未命名":
                payload["title"] = t
                enriched = True
            if br.get("thumbnail"):
                prev = payload.get("thumbnail") or ""
                if not str(prev).startswith("data:image"):
                    payload["thumbnail"] = br["thumbnail"]
                    enriched = True
            if enriched:
                payload = await finalize_resolve_payload(payload)
    except YtDlpError:
        pass
    return payload


@fastapi_app.post("/api/resolve")
async def api_resolve(body: ResolveBody):
    has_cobalt = bool(config.COBALT_API_URL) and config.COBALT_ENABLED

    url = normalize_fetch_url(body.url.strip())
    if has_cobalt and _prefer_ytdlp_before_cobalt(url):
        ytdlp_err: YtDlpError | None = None
        try:
            meta = await asyncio.to_thread(fetch_metadata, url)
            return await finalize_resolve_payload(build_resolve_response(meta))
        except YtDlpError as e:
            ytdlp_err = e
        try:
            cb = await cobalt_resolve(url)
            return await _cobalt_resolve_response(url, cb)
        except CobaltError:
            pass
        raise HTTPException(
            status_code=400,
            detail=public_resolve_error_detail(url, ytdlp_err),
        )

    if has_cobalt:
        cobalt_err: CobaltError | None = None
        try:
            cb = await cobalt_resolve(url)
            return await _cobalt_resolve_response(url, cb)
        except CobaltError as e:
            cobalt_err = e

        ytdlp_after_cobalt: YtDlpError | None = None
        try:
            meta = await asyncio.to_thread(fetch_metadata, url)
            return await finalize_resolve_payload(build_resolve_response(meta))
        except YtDlpError as e:
            ytdlp_after_cobalt = e

        if ytdlp_after_cobalt:
            raise HTTPException(
                status_code=400,
                detail=public_resolve_error_detail(url, ytdlp_after_cobalt),
            )
        if cobalt_err and str(cobalt_err).strip() != "无法解析该链接，请检查链接是否正确":
            raise HTTPException(
                status_code=400,
                detail=sanitize_douyin_resolve_user_detail(url, str(cobalt_err)),
            )
        raise HTTPException(
            status_code=400,
            detail=public_resolve_error_detail(url, None),
        )
    else:
        ytdlp_err: YtDlpError | None = None
        try:
            meta = await asyncio.to_thread(fetch_metadata, url)
            return await finalize_resolve_payload(build_resolve_response(meta))
        except YtDlpError as e:
            ytdlp_err = e
        try:
            cb = await cobalt_resolve(url)
            return await _cobalt_resolve_response(url, cb)
        except CobaltError:
            pass
        raise HTTPException(
            status_code=400,
            detail=public_resolve_error_detail(url, ytdlp_err),
        )


@fastapi_app.post("/api/download")
async def api_download(body: DownloadBody, background_tasks: BackgroundTasks):
    has_cobalt = bool(config.COBALT_API_URL) and config.COBALT_ENABLED
    use_cobalt = False
    url = normalize_fetch_url(body.url.strip())

    try:
        if has_cobalt:
            if _prefer_ytdlp_before_cobalt(url):
                try:
                    await asyncio.to_thread(fetch_metadata, url)
                except YtDlpError as e:
                    raise HTTPException(
                        status_code=400,
                        detail=public_resolve_error_detail(url, e),
                    ) from e
                use_cobalt = False
            else:
                try:
                    await cobalt_resolve(url)
                    use_cobalt = True
                except CobaltError:
                    try:
                        await asyncio.to_thread(fetch_metadata, url)
                    except YtDlpError as e:
                        raise HTTPException(
                            status_code=400,
                            detail=public_resolve_error_detail(url, e),
                        ) from e
        else:
            try:
                await asyncio.to_thread(fetch_metadata, url)
            except YtDlpError as e:
                try:
                    await cobalt_resolve(url)
                    use_cobalt = True
                except CobaltError:
                    raise HTTPException(
                        status_code=400,
                        detail=public_resolve_error_detail(url, e),
                    ) from e
    except HTTPException:
        raise
    except Exception as e:
        _log.exception("download pre-check failed")
        raise HTTPException(status_code=500, detail="服务器内部错误，请稍后重试") from e

    fmt = body.format_id if body.format_id and body.format_id != "best" else None
    merge_container = "mp4" if body.encoding == "auto" else body.encoding
    job = await store.create(
        url,
        fmt,
        media_mode=body.media_mode,
        merge_container=merge_container,
    )
    background_tasks.add_task(run_download_job, job, use_cobalt=use_cobalt)
    return {"job_id": job.id}


@fastapi_app.get("/api/jobs/{job_id}")
async def api_job_status(job_id: str):
    job = await store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    payload: dict = {
        "status": job.status,
        "error": job.error,
    }
    if job.file_path and job.status == "completed":
        payload["filename"] = job.file_path.name
    return payload


@fastapi_app.get("/api/jobs/{job_id}/file")
async def api_job_file(job_id: str):
    job = await store.get(job_id)
    if not job or job.status != "completed" or not job.file_path:
        raise HTTPException(status_code=404, detail="文件不可用")
    path = job.file_path
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件已删除")
    media_type, _ = mimetypes.guess_type(str(path))
    return FileResponse(
        path,
        filename=path.name,
        media_type=media_type or "application/octet-stream",
    )


static_dir = Path(__file__).resolve().parent.parent / "static"
if static_dir.is_dir():
    fastapi_app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

"""Cobalt API fallback — when yt-dlp fails, try cobalt instances.

Priority:
1. User-configured self-hosted instance (COBALT_API_URL in .env) — most reliable
2. Public instances from instances.cobalt.best (may require JWT, best-effort)

Self-host one-liner:
    docker run -p 9000:9000 -e API_URL=http://localhost:9000 ghcr.io/imputnet/cobalt:10
Then set COBALT_API_URL=http://localhost:9000 in .env
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx

from app import config

_log = logging.getLogger("uvicorn.error")

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36 VideoFetch/1.0"

_instances_cache: dict[str, Any] = {"ts": 0.0, "list": []}
_CACHE_TTL = 1800


class CobaltError(Exception):
    pass


def _infer_filename_from_url(url: str) -> str | None:
    """从链接猜一个可区分的默认文件名（避免全是 video.mp4）。"""
    u = url.strip()
    m = re.search(r"(?:www\.)?douyin\.com/video/(\d+)", u, re.I)
    if m:
        return f"douyin_{m.group(1)}.mp4"
    m = re.search(r"tiktok\.com/@[^/]+/video/(\d+)", u, re.I)
    if m:
        return f"tiktok_{m.group(1)}.mp4"
    m = re.search(r"tiktok\.com/.*/video/(\d+)", u, re.I)
    if m:
        return f"tiktok_{m.group(1)}.mp4"
    m = re.search(r"(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]{6,})", u, re.I)
    if m:
        return f"youtube_{m.group(1)}.mp4"
    m = re.search(r"bilibili\.com/video/(BV[\w]+)", u, re.I)
    if m:
        return f"{m.group(1)}.mp4"
    m = re.search(r"xiaohongshu\.com/(?:explore|discovery/item)/([\da-f]+)", u, re.I)
    if m:
        return f"xiaohongshu_{m.group(1)}.mp4"
    m = re.search(r"(?:twitter\.com|x\.com)/\w+/status/(\d+)", u, re.I)
    if m:
        return f"twitter_{m.group(1)}.mp4"
    return None


def _coerce_download_filename(url: str, api_filename: str | None) -> str:
    raw = (api_filename or "").strip()
    stem = Path(raw).stem.lower() if raw else ""
    generic = not raw or stem in ("video", "download", "media", "file", "movie")
    if generic:
        inferred = _infer_filename_from_url(url)
        if inferred:
            return inferred
        h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
        return f"fetch_{h}.mp4"
    fname = raw
    if "." not in fname:
        fname += ".mp4"
    return fname


def _parse_ver(v: str) -> int:
    try:
        return int(v.split(".")[0])
    except (ValueError, IndexError):
        return 10


async def _fetch_public_instances() -> list[dict[str, Any]]:
    now = time.time()
    if _instances_cache["list"] and now - _instances_cache["ts"] < _CACHE_TTL:
        return _instances_cache["list"]
    try:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": _UA}) as c:
            r = await c.get("https://instances.cobalt.best/api/instances.json")
            if r.status_code == 200:
                data = r.json()
                good: list[dict[str, Any]] = []
                for inst in data:
                    if not inst.get("online"):
                        continue
                    hostname = inst.get("api")
                    protocol = inst.get("protocol", "https")
                    if not hostname:
                        continue
                    good.append({
                        "base_url": f"{protocol}://{hostname}",
                        "ver": _parse_ver(inst.get("version", "")),
                        "score": inst.get("score", 0),
                    })
                good.sort(key=lambda x: x["score"], reverse=True)
                if good:
                    _instances_cache["list"] = good
                    _instances_cache["ts"] = now
                    return good
    except Exception as exc:
        _log.debug("cobalt: failed to fetch instance list: %s", exc)
    return []


def _build_instance_list() -> list[dict[str, Any]]:
    """Synchronous helper: returns at least the user-configured instance."""
    result: list[dict[str, Any]] = []
    if config.COBALT_API_URL:
        result.append({
            "base_url": config.COBALT_API_URL.rstrip("/"),
            "ver": 10,
            "score": 999,
            "self_hosted": True,
        })
    return result


async def _get_all_instances() -> list[dict[str, Any]]:
    self_hosted = _build_instance_list()
    public = await _fetch_public_instances()
    return self_hosted + public


async def cobalt_resolve(url: str) -> dict[str, Any]:
    if not config.COBALT_ENABLED:
        raise CobaltError("cobalt 备用通道已禁用（COBALT_ENABLED=false）")

    instances = await _get_all_instances()
    if not instances:
        raise CobaltError(
            "未配置 cobalt 实例。推荐自建：docker run -p 9000:9000 ghcr.io/imputnet/cobalt:10，"
            "然后在 .env 设置 COBALT_API_URL=http://localhost:9000"
        )

    last_err = ""
    user_facing_err = ""
    for inst in instances[:8]:
        base = inst["base_url"]
        ver = inst["ver"]
        try:
            result = await _try_instance(base, ver, url)
            if result:
                result["_instance"] = base
                return result
        except CobaltError as e:
            msg = str(e)
            last_err = msg[:200]
            if any(k in msg for k in ("需要登录", "不可用", "私密", "地区限制")):
                user_facing_err = msg
            _log.debug("cobalt %s failed: %s", base, last_err)
        except Exception as e:
            last_err = str(e)[:200]
            _log.debug("cobalt %s error: %s", base, last_err)

    if user_facing_err:
        raise CobaltError(user_facing_err)

    raise CobaltError("无法解析该链接，请检查链接是否正确")


async def _try_instance(base_url: str, ver: int, url: str) -> dict[str, Any] | None:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": _UA,
    }
    if ver >= 10:
        body: dict[str, Any] = {"url": url, "videoQuality": "1080"}
        endpoint = f"{base_url}/"
    else:
        body = {"url": url, "vQuality": "1080"}
        endpoint = f"{base_url}/api/json"

    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as c:
        r = await c.post(endpoint, json=body, headers=headers)

    if r.status_code == 403:
        return None

    data: dict[str, Any] = {}
    try:
        data = r.json()
    except Exception:
        if r.status_code != 200:
            return None

    status = data.get("status")

    if r.status_code != 200 or status == "error":
        err_code = ""
        if isinstance(data.get("error"), dict):
            err_code = data["error"].get("code", "")
        elif isinstance(data.get("text"), str):
            err_code = data["text"]
        elif isinstance(data.get("error"), str):
            err_code = data["error"]

        if "auth" in err_code.lower():
            return None

        if "youtube.login" in err_code.lower():
            raise CobaltError("该 YouTube 视频需要登录才能观看，暂不支持下载")

        if "content.video.unavailable" in err_code.lower():
            raise CobaltError("视频不可用（可能已删除、私密或有地区限制）")

        if err_code:
            raise CobaltError(err_code[:300])
        return None

    if status in ("redirect", "stream", "tunnel"):
        return {
            "cobalt": True,
            "download_url": data.get("url"),
            "filename": data.get("filename"),
            "status": status,
        }

    if status == "picker":
        items = data.get("picker") or data.get("audio") or []
        if isinstance(items, list) and items:
            first = items[0]
            return {
                "cobalt": True,
                "download_url": first.get("url") if isinstance(first, dict) else None,
                "filename": data.get("filename"),
                "status": "picker",
                "picker": items,
            }

    return None


async def cobalt_download_to_dir(url: str, dest_dir: Path) -> Path:
    result = await cobalt_resolve(url)
    dl_url = result.get("download_url")
    if not dl_url:
        raise CobaltError("cobalt 未返回下载链接")

    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = _coerce_download_filename(url, result.get("filename"))
    for ch in '<>:"/\\|?*':
        fname = fname.replace(ch, "_")
    dest = dest_dir / fname

    dl_timeout = httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0)
    try:
        async with httpx.AsyncClient(
            timeout=dl_timeout,
            follow_redirects=True,
            headers={"User-Agent": _UA},
        ) as c:
            async with c.stream("GET", dl_url) as resp:
                if resp.status_code >= 400:
                    raise CobaltError(f"下载失败 HTTP {resp.status_code}")
                with open(dest, "wb") as f:
                    async for chunk in resp.aiter_bytes(65536):
                        f.write(chunk)
    except httpx.TimeoutException:
        dest.unlink(missing_ok=True)
        raise CobaltError("下载超时，视频可能过大")
    except httpx.HTTPError as e:
        dest.unlink(missing_ok=True)
        raise CobaltError(f"下载网络错误: {type(e).__name__}") from e

    if not dest.is_file() or dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        raise CobaltError("下载文件为空")
    return dest


def check_cobalt_sync() -> tuple[bool, int, str]:
    """Diagnostics helper. Returns (any_available, public_count, self_hosted_url)."""
    self_url = config.COBALT_API_URL or ""
    self_ok = False
    if self_url:
        try:
            r = httpx.get(self_url.rstrip("/") + "/", timeout=5)
            self_ok = r.status_code < 500
        except Exception:
            pass

    public_count = 0
    try:
        r = httpx.get("https://instances.cobalt.best/api/instances.json", timeout=8)
        if r.status_code == 200:
            data = r.json()
            public_count = len([i for i in data if i.get("online") and i.get("api")])
    except Exception:
        pass

    return self_ok or public_count > 0, public_count, self_url

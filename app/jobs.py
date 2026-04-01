from __future__ import annotations

import asyncio
import logging
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import config
from app.cobalt import CobaltError, cobalt_download_to_dir
from app.ytdlp import YtDlpError, download_to_dir, public_resolve_error_detail

_log = logging.getLogger("uvicorn.error")


@dataclass
class Job:
    id: str
    url: str
    format_spec: str | None
    media_mode: str = "original"
    merge_container: str = "mp4"
    status: str = "pending"  # pending | downloading | completed | failed
    error: str | None = None
    file_path: Path | None = None
    created_at: float = field(default_factory=time.time)


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        url: str,
        format_spec: str | None,
        *,
        media_mode: str = "original",
        merge_container: str = "mp4",
    ) -> Job:
        jid = str(uuid.uuid4())
        job = Job(
            id=jid,
            url=url.strip(),
            format_spec=format_spec,
            media_mode=media_mode,
            merge_container=merge_container,
        )
        async with self._lock:
            self._jobs[jid] = job
        return job

    async def get(self, jid: str) -> Job | None:
        async with self._lock:
            return self._jobs.get(jid)

    async def update(self, jid: str, **kwargs: Any) -> None:
        async with self._lock:
            job = self._jobs.get(jid)
            if not job:
                return
            for k, v in kwargs.items():
                setattr(job, k, v)

    async def cleanup_expired(self) -> None:
        now = time.time()
        async with self._lock:
            dead: list[str] = []
            for jid, job in self._jobs.items():
                if now - job.created_at > config.JOB_TTL_SECONDS:
                    dead.append(jid)
            for jid in dead:
                self._jobs.pop(jid, None)
                d = config.DOWNLOAD_DIR / jid
                if d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
            # sweep empty old dirs under download root
            root = config.DOWNLOAD_DIR
            if not root.is_dir():
                return
            for p in root.iterdir():
                if not p.is_dir():
                    continue
                try:
                    if now - p.stat().st_mtime > config.JOB_TTL_SECONDS:
                        shutil.rmtree(p, ignore_errors=True)
                except OSError:
                    pass


store = JobStore()


async def run_download_job(job: Job, *, use_cobalt: bool = False) -> None:
    job_dir = config.DOWNLOAD_DIR / job.id
    try:
        await store.update(job.id, status="downloading")

        path: Path | None = None
        try_cobalt = use_cobalt and job.media_mode == "original"
        if try_cobalt:
            try:
                path = await cobalt_download_to_dir(job.url, job_dir)
            except CobaltError as ce:
                _log.warning("cobalt download failed, falling back to yt-dlp: %s", ce)
                shutil.rmtree(job_dir, ignore_errors=True)
                path = None

        if path is None:
            path = await asyncio.to_thread(
                download_to_dir,
                job.url,
                job_dir,
                job.format_spec,
                media_mode=job.media_mode,
                merge_container=job.merge_container,
            )

        await store.update(job.id, status="completed", file_path=path)
    except (YtDlpError, CobaltError) as e:
        if isinstance(e, YtDlpError):
            err_msg = public_resolve_error_detail(job.url, e)
        else:
            err_msg = str(e)
        await store.update(job.id, status="failed", error=err_msg[:2000])
    except Exception as e:  # noqa: BLE001
        await store.update(job.id, status="failed", error=str(e)[:2000])

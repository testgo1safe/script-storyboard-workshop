from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .config import IMAGE_MAX_CONCURRENT, OUTPUT_DIR
from .image_utils import ReferenceImage
from .protocol_client import protocol_client

logger = logging.getLogger(__name__)
JOBS_FILE = OUTPUT_DIR / "jobs.json"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ImageJob:
    id: str
    prompt: str
    keep_conversation: bool = True
    new_conversation: bool = False
    reference_images: list[ReferenceImage] = field(default_factory=list)
    status: JobStatus = JobStatus.QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    result_path: str | None = None
    error: str | None = None


class ImageTaskManager:
    def __init__(self, max_concurrent: int = IMAGE_MAX_CONCURRENT) -> None:
        self._sem = asyncio.Semaphore(max_concurrent)
        self._jobs: dict[str, ImageJob] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers_started = False
        self._lock = asyncio.Lock()
        self._load_persisted()

    def _load_persisted(self) -> None:
        if not JOBS_FILE.exists():
            return
        try:
            raw = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            try:
                status = JobStatus(item.get("status") or "completed")
            except ValueError:
                continue
            if status not in {JobStatus.COMPLETED, JobStatus.FAILED}:
                continue
            result_path = item.get("result_path")
            if result_path and not Path(result_path).exists():
                continue
            job = ImageJob(
                id=str(item["id"]),
                prompt=str(item.get("prompt") or ""),
                status=status,
                created_at=float(item.get("created_at") or 0),
                started_at=item.get("started_at"),
                finished_at=item.get("finished_at"),
                result_path=result_path,
                error=item.get("error"),
            )
            self._jobs[job.id] = job

    def _persist(self) -> None:
        rows = []
        for job in self._jobs.values():
            if job.status not in {JobStatus.COMPLETED, JobStatus.FAILED}:
                continue
            rows.append(
                {
                    "id": job.id,
                    "prompt": job.prompt,
                    "status": job.status.value,
                    "created_at": job.created_at,
                    "started_at": job.started_at,
                    "finished_at": job.finished_at,
                    "result_path": job.result_path,
                    "error": job.error,
                }
            )
        rows.sort(key=lambda item: item.get("finished_at") or item.get("created_at") or 0, reverse=True)
        JOBS_FILE.write_text(json.dumps(rows[:400], ensure_ascii=False, indent=2), encoding="utf-8")
        for job in self._jobs.values():
            if job.status != JobStatus.COMPLETED or not job.result_path:
                continue
            path = Path(job.result_path)
            if not path.exists():
                continue
            meta = path.with_suffix(path.suffix + ".meta.json")
            meta.write_text(
                json.dumps({"id": job.id, "prompt": job.prompt, "result_path": str(path)}, ensure_ascii=False),
                encoding="utf-8",
            )

    async def _ensure_workers(self) -> None:
        async with self._lock:
            if self._workers_started:
                return
            for _ in range(IMAGE_MAX_CONCURRENT):
                asyncio.create_task(self._worker())
            self._workers_started = True

    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            job = self._jobs.get(job_id)
            if job is None:
                self._queue.task_done()
                continue

            job.status = JobStatus.RUNNING
            job.started_at = time.time()
            logger.info("image job %s started: %s", job.id, job.prompt[:80])
            try:
                async with self._sem:
                    path = await protocol_client.generate_image(
                        job.prompt,
                        keep_conversation=job.keep_conversation,
                        new_conversation=job.new_conversation,
                        output_dir=OUTPUT_DIR,
                        reference_images=job.reference_images,
                    )
                job.result_path = str(path)
                job.status = JobStatus.COMPLETED
                logger.info("image job %s completed -> %s", job.id, job.result_path)
            except Exception as exc:  # noqa: BLE001
                job.error = str(exc)
                job.status = JobStatus.FAILED
                logger.error("image job %s failed: %s", job.id, exc)
            finally:
                job.finished_at = time.time()
                try:
                    self._persist()
                except Exception:
                    logger.exception("persist image jobs failed")
                self._queue.task_done()

    async def submit(
        self,
        prompt: str,
        *,
        keep_conversation: bool = True,
        new_conversation: bool = False,
        reference_images: list[ReferenceImage] | None = None,
        wait: bool = True,
    ) -> ImageJob:
        await self._ensure_workers()
        job = ImageJob(
            id=str(uuid.uuid4()),
            prompt=prompt,
            keep_conversation=keep_conversation,
            new_conversation=new_conversation,
            reference_images=list(reference_images or []),
        )
        self._jobs[job.id] = job
        await self._queue.put(job.id)

        if wait:
            await self._wait_job(job.id)
        return self._jobs[job.id]

    async def submit_batch(
        self,
        prompts: list[str],
        *,
        keep_conversation: bool = False,
    ) -> list[ImageJob]:
        await self._ensure_workers()
        job_ids: list[str] = []
        for prompt in prompts:
            job = ImageJob(
                id=str(uuid.uuid4()),
                prompt=prompt,
                keep_conversation=keep_conversation,
            )
            self._jobs[job.id] = job
            job_ids.append(job.id)
            await self._queue.put(job.id)

        await asyncio.gather(*(self._wait_job(job_id) for job_id in job_ids))
        return [self._jobs[job_id] for job_id in job_ids]

    async def _wait_job(self, job_id: str) -> None:
        while True:
            job = self._jobs[job_id]
            if job.status in {JobStatus.COMPLETED, JobStatus.FAILED}:
                return
            await asyncio.sleep(0.5)

    def get_job(self, job_id: str) -> ImageJob | None:
        return self._jobs.get(job_id)

    def list_jobs(
        self,
        *,
        status: JobStatus | None = None,
        prompt: str | None = None,
        exact: bool = True,
        limit: int = 20,
    ) -> list[ImageJob]:
        jobs = list(self._jobs.values())
        if status is not None:
            jobs = [job for job in jobs if job.status == status]
        if prompt:
            needle = " ".join(prompt.split())
            matched = []
            for job in jobs:
                actual = " ".join(job.prompt.split())
                if exact:
                    if actual == needle or actual == f"请生成一张图片：{needle}":
                        matched.append(job)
                elif needle in actual or actual in needle:
                    matched.append(job)
            jobs = matched
        jobs.sort(key=lambda job: job.finished_at or job.created_at, reverse=True)
        return jobs[: max(1, limit)]

    def record_completed(self, prompt: str, path: Path) -> ImageJob:
        job = ImageJob(
            id=str(uuid.uuid4()),
            prompt=prompt,
            status=JobStatus.COMPLETED,
            started_at=time.time(),
            finished_at=time.time(),
            result_path=str(path),
        )
        self._jobs[job.id] = job
        try:
            self._persist()
        except Exception:
            logger.exception("persist recovered image job failed")
        return job

    async def recover(self, prompt: str) -> ImageJob:
        await self._ensure_workers()
        async with self._sem:
            path = await protocol_client.recover_image_from_conversation(prompt, OUTPUT_DIR)
        return self.record_completed(prompt, path)

    def stats(self) -> dict[str, int]:
        counts = {status.value: 0 for status in JobStatus}
        for job in self._jobs.values():
            counts[job.status.value] += 1
        return counts


image_task_manager = ImageTaskManager()


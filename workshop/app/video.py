from __future__ import annotations

import asyncio
import base64
import io
import subprocess
from pathlib import Path

import httpx
from PIL import Image

from .config import settings
from .schemas import PictureBinding, VideoClip


class MiniMaxError(RuntimeError):
    pass


def _headers(idempotency_key: str = "") -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {settings.minimax_api_key}",
        "Accept": "application/json",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _base() -> str:
    return settings.minimax_base_url.rstrip("/")


def encode_reference_image(path: Path, max_side: int = 1280, quality: int = 85) -> str:
    try:
        img = Image.open(path)
        if img.mode not in {"RGB", "L"}:
            img = img.convert("RGB")
        elif img.mode == "L":
            img = img.convert("RGB")
        w, h = img.size
        scale = min(1.0, max_side / max(w, h, 1))
        if scale < 1:
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"
    except Exception:
        raw = path.read_bytes()
        b64 = base64.b64encode(raw).decode("ascii")
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        return f"data:{mime};base64,{b64}"


def _raise_for_api(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text[:800]}
    if resp.status_code >= 400:
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            message = err.get("message") or resp.text[:800]
        else:
            message = data.get("Message") or data.get("message") or resp.text[:800]
        raise MiniMaxError(f"MiniMax {resp.status_code}: {message}")
    return data if isinstance(data, dict) else {"data": data}


async def health() -> dict:
    if not settings.minimax_api_key:
        return {"configured": False, "status": "missing_key"}
    url = _base() + "/minimax/v2/query/point_usage_summary"
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(url, headers=_headers())
            data = _raise_for_api(resp)
        return {
            "configured": True,
            "status": "ok",
            "available_points": data.get("available_points"),
            "reserved_points": data.get("reserved_points"),
            "total_points": data.get("total_points"),
        }
    except Exception as exc:
        return {"configured": True, "status": "error", "error": str(exc)}


MAX_REF_VIDEO_BYTES = 8 * 1024 * 1024


def encode_reference_video(path: Path) -> str | None:
    if not path.exists() or path.stat().st_size < 4096:
        return None
    raw = path.read_bytes()
    if len(raw) > MAX_REF_VIDEO_BYTES:
        return None
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:video/mp4;base64,{b64}"


def extract_clip_tail(src: Path, dest: Path, seconds: float = 3.0) -> Path | None:
    if not src.exists() or src.stat().st_size < 4096:
        return None
    seconds = max(2.0, min(4.0, float(seconds)))
    dest.parent.mkdir(parents=True, exist_ok=True)
    from .edit import ffmpeg_bin

    cmd = [
        ffmpeg_bin(),
        "-y",
        "-sseof",
        f"-{seconds}",
        "-i",
        str(src),
        "-t",
        str(seconds),
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "28",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    except Exception:
        fallback = [
            ffmpeg_bin(),
            "-y",
            "-i",
            str(src),
            "-t",
            str(seconds),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            "-pix_fmt",
            "yuv420p",
            str(dest),
        ]
        try:
            subprocess.run(fallback, check=True, capture_output=True, timeout=60)
        except Exception:
            return None
    if dest.exists() and 4096 < dest.stat().st_size <= MAX_REF_VIDEO_BYTES:
        return dest
    return None


def video_ref_rejected(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(
        key in msg
        for key in (
            "video_url",
            "reference_video",
            "invalid param",
            "64mb",
            "64 mb",
        )
    )


async def create_task(
    prompt: str,
    *,
    duration: int,
    ratio: str,
    resolution: str,
    images: list[Path],
    idempotency_key: str,
    use_context_ir: bool = False,
    videos: list[Path] | None = None,
) -> str:
    if not settings.minimax_api_key:
        raise MiniMaxError("未配置 MINIMAX_API_KEY")
    content: list[dict] = [{"type": "text", "text": prompt}]
    for path in images[:9]:
        if not path.exists():
            continue
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": encode_reference_image(path)},
                "role": "reference_image",
            }
        )
    for path in (videos or [])[:3]:
        encoded = encode_reference_video(path)
        if not encoded:
            continue
        content.append(
            {
                "type": "video_url",
                "video_url": {"url": encoded},
                "role": "reference_video",
            }
        )
    payload = {
        "model": settings.minimax_model,
        "content": content,
        "resolution": resolution,
        "duration": int(duration),
        "ratio": ratio,
        "use_context_ir": bool(use_context_ir),
        "aigc_watermark": False,
    }
    url = _base() + "/minimax/v2/video_generation"
    headers = _headers(idempotency_key)
    headers["Content-Type"] = "application/json"
    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        data = _raise_for_api(resp)
    task_id = data.get("task_id") or (data.get("task") or {}).get("id")
    if not task_id:
        raise MiniMaxError(f"创建任务未返回 task_id: {data}")
    return str(task_id)


async def query_task(task_id: str) -> dict:
    url = _base() + f"/minimax/v2/query/video_generation/{task_id}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, headers=_headers())
        data = _raise_for_api(resp)
    task = data.get("task") or data
    return task


async def cancel_task(task_id: str) -> dict:
    url = _base() + f"/minimax/v2/video_generation/{task_id}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.delete(url, headers=_headers())
        return _raise_for_api(resp)


async def download_video(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as client:
        async with client.stream("GET", url) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise MiniMaxError(f"下载视频失败 {resp.status_code}: {body[:400]!r}")
            with dest.open("wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)
    return dest


def apply_remote_status(clip: VideoClip, task: dict) -> None:
    status = str(task.get("status") or "")
    clip.task_id = str(task.get("id") or clip.task_id)
    clip.estimated_remaining_seconds = int(task.get("estimated_remaining_seconds") or 0)
    clip.resolution = str(task.get("resolution") or clip.resolution)
    clip.ratio = str(task.get("ratio") or clip.ratio)
    if status in {"queued", "running", "succeeded", "failed", "cancelled"}:
        clip.video_status = status  # type: ignore[assignment]
    error = task.get("error")
    if isinstance(error, dict):
        clip.error = str(error.get("message") or error)
    elif status == "failed":
        clip.error = clip.error or "视频生成失败"


def image_paths_for_clip(image_dir: Path, pictures: list[PictureBinding], clip: VideoClip) -> list[Path]:
    by_index = {p.picture_index: p for p in pictures}
    paths: list[Path] = []
    for idx in clip.picture_indices:
        pic = by_index.get(idx)
        if not pic or pic.status != "ready" or not pic.filename:
            continue
        path = image_dir / pic.filename
        if path.exists():
            paths.append(path)
    return paths[:9]


def concat_with_ffmpeg(paths: list[Path], dest: Path) -> Path | None:
    existing = [p for p in paths if p.exists()]
    if len(existing) < 2:
        if len(existing) == 1:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(existing[0].read_bytes())
            return dest
        return None
    from .edit import ffmpeg_bin

    list_file = dest.parent / "concat.txt"
    list_file.write_text(
        "".join(f"file '{p.resolve().as_posix()}'\n" for p in existing),
        encoding="utf-8",
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin(),
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c",
        "copy",
        str(dest),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        return dest if dest.exists() else None
    except Exception:
        cmd[-3:-1] = ["-c:v", "libx264", "-c:a", "aac"]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=180)
            return dest if dest.exists() else None
        except Exception:
            return None


async def wait_until_done(task_id: str, *, poll_seconds: float = 4.0, timeout: float = 900.0) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout
    last = {}
    while asyncio.get_event_loop().time() < deadline:
        last = await query_task(task_id)
        status = last.get("status")
        if status in {"succeeded", "failed", "cancelled"}:
            return last
        await asyncio.sleep(poll_seconds)
    raise MiniMaxError("等待视频生成超时")


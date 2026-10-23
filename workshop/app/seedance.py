from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import re
from pathlib import Path

import httpx
from PIL import Image

from .config import DATA_DIR, settings
from .schemas import PictureBinding, VideoClip

logger = logging.getLogger(__name__)

# ToAPIs 中转：https://toapis.cn  ·  POST /v1/videos/generations
# 虚拟人像：/v1/videos/doubao-seedance-2-0/private-avatar/*


class SeedanceError(RuntimeError):
    pass


SEEDANCE_MODELS: dict[str, str] = {
    "seedance-2": "标准版",
    "seedance-2-fast": "Fast 快速",
    "seedance-2-mini": "Mini 轻量",
}

_MODEL_ALIASES = {
    "seedance-2": "seedance-2",
    "seedance-2-fast": "seedance-2-fast",
    "seedance-2-mini": "seedance-2-mini",
    "doubao-seedance-2-0-260128": "seedance-2",
    "doubao-seedance-2-0-fast-260128": "seedance-2-fast",
    "doubao-seedance-2-0-mini-260615": "seedance-2-mini",
    "dreamina-seedance-2.0": "seedance-2",
    "dreamina-seedance-2.0-fast": "seedance-2-fast",
    "dreamina-seedance-2.5": "seedance-2",
}

_STATUS_MAP = {
    "not_start": "queued",
    "submitted": "queued",
    "pending": "queued",
    "queued": "queued",
    "in_progress": "running",
    "running": "running",
    "completed": "succeeded",
    "success": "succeeded",
    "succeed": "succeeded",
    "succeeded": "succeeded",
    "failure": "failed",
    "fail": "failed",
    "failed": "failed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "expired": "failed",
}

CACHE_FILE = DATA_DIR / "toapis_assets.json"
_AVATAR_PREFIX = "/v1/videos/doubao-seedance-2-0/private-avatar"


def is_toapis() -> bool:
    host = (settings.seedance_base_url or "").lower()
    return "toapis.cn" in host or "toapis.com" in host


def is_konjac() -> bool:
    """兼容旧调用：现在一律走 ToAPIs 中转。"""
    return is_toapis() or bool(settings.seedance_api_key)


def assets_ready() -> bool:
    return bool(settings.seedance_api_key)


def normalize_model(model: str = "") -> str:
    value = (model or settings.seedance_model or "seedance-2-fast").strip()
    mapped = _MODEL_ALIASES.get(value) or _MODEL_ALIASES.get(value.lower())
    if mapped:
        return mapped
    lowered = value.lower()
    for key, canon in _MODEL_ALIASES.items():
        if key.lower() == lowered:
            return canon
    return "seedance-2-fast"


def api_model(model: str = "") -> str:
    return normalize_model(model)


def model_label(model: str = "") -> str:
    key = normalize_model(model)
    name = SEEDANCE_MODELS.get(key, key)
    return f"Seedance {name}"


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.seedance_api_key}",
        "Accept": "application/json",
    }


def _json_headers() -> dict[str, str]:
    return {**_auth_headers(), "Content-Type": "application/json"}


def _base() -> str:
    raw = (settings.seedance_base_url or "https://toapis.cn").rstrip("/")
    if raw.lower().endswith("/v1"):
        raw = raw[:-3].rstrip("/")
    return raw or "https://toapis.cn"


def map_resolution(resolution: str, model: str = "") -> str:
    key = (resolution or "").strip().upper()
    mapping = {
        "768P": "720p",
        "720P": "720p",
        "1080P": "1080p",
        "2K": "1080p",
        "4K": "4k",
        "480P": "480p",
    }
    if key in mapping:
        mapped = mapping[key]
    else:
        lowered = (resolution or "480p").strip().lower()
        mapped = lowered if lowered.endswith("p") or lowered == "4k" else "480p"
    model_key = api_model(model).lower()
    if ("mini" in model_key or "fast" in model_key) and mapped in {"1080p", "4k"}:
        return "720p"
    return mapped


def map_duration(duration: int, model: str = "") -> int:
    return max(4, min(15, int(duration or 5)))


_CONTENT_INDEX = re.compile(r"content\[(\d+)\]", re.I)


def normalize_asset_uri(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if text.lower().startswith("asset://"):
        rest = text.split("://", 1)[-1].strip()
        return f"asset://{rest}" if rest else ""
    if re.match(r"^(pa_|pg_|asset[_-])", text, re.I):
        return f"asset://{text}"
    return ""


def is_live_asset_uri(value: str) -> bool:
    text = normalize_asset_uri(value).lower()
    return any(token in text for token in ("://pa_", "://asset_img", "://asset_vid", "://asset_aud"))


def real_person_rejected(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "real person" in msg or "真人" in msg or "privacyinformation" in msg


def picture_for_content_error(pictures: list[PictureBinding], message: str) -> PictureBinding | None:
    match = _CONTENT_INDEX.search(message or "")
    if not match:
        return pictures[0] if pictures else None
    idx = int(match.group(1)) - 1
    if 0 <= idx < len(pictures):
        return pictures[idx]
    return pictures[0] if pictures else None


def humanize_create_error(exc: Exception, pictures: list[PictureBinding] | None = None) -> str:
    raw = str(exc)
    if not real_person_rejected(exc):
        return raw
    pic = picture_for_content_error(pictures or [], raw)
    name = f"「{pic.name}」" if pic else "角色参考图"
    return (
        f"Seedance 把{name}判成了真人照片。"
        "请先点「一键入库角色图」，等卡片出现 asset:// 后再出这一段。"
        "场景图和道具图不用入库。"
    )


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
            message = (
                (data.get("message") if isinstance(data, dict) else None)
                or (data.get("Message") if isinstance(data, dict) else None)
                or resp.text[:800]
            )
        raise SeedanceError(f"Seedance {resp.status_code}: {message}")
    if isinstance(data, dict) and data.get("success") is False:
        raise SeedanceError(data.get("message") or data.get("error") or str(data)[:400])
    return data if isinstance(data, dict) else {"data": data}


def _normalize_status(raw: object) -> str:
    text = str(raw or "").strip()
    return _STATUS_MAP.get(text.lower(), text.lower())


def _nested_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _task_video_url_raw(task: dict) -> str:
    if not isinstance(task, dict):
        return ""
    result = task.get("result")
    if isinstance(result, dict):
        items = result.get("data")
        if isinstance(items, list) and items:
            first = items[0]
            if isinstance(first, dict):
                url = first.get("url") or first.get("video_url")
                if isinstance(url, str) and url.startswith("http"):
                    return url
            elif isinstance(first, str) and first.startswith("http"):
                return first
        for key in ("url", "video_url", "result_url"):
            value = result.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
    for key in ("result_url", "video_url", "url"):
        value = task.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    content = task.get("content")
    if isinstance(content, dict):
        for key in ("video_url", "url", "result_url"):
            value = content.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
    inner = task.get("data")
    if isinstance(inner, dict):
        return _task_video_url_raw(inner)
    return ""


def normalize_task(data: dict, task_id: str = "") -> dict:
    envelope = data if isinstance(data, dict) else {}
    outer = _nested_dict(envelope.get("data")) or envelope
    nested = _nested_dict(outer.get("data"))
    status = _normalize_status(
        outer.get("status") or nested.get("status") or envelope.get("status")
    )
    error = outer.get("fail_reason") or outer.get("error") or nested.get("error") or envelope.get("error")
    if isinstance(error, dict):
        error_obj = error
    elif error:
        error_obj = {"message": str(error)}
    else:
        error_obj = None
    video_url = _task_video_url_raw(outer) or _task_video_url_raw(nested) or _task_video_url_raw(envelope)
    resolved_id = str(
        outer.get("id")
        or outer.get("task_id")
        or nested.get("id")
        or envelope.get("id")
        or task_id
        or ""
    )
    content = nested.get("content") if isinstance(nested.get("content"), dict) else outer.get("content")
    if not isinstance(content, dict):
        content = {}
    if video_url:
        content = {**content, "video_url": video_url}
    progress = outer.get("progress") or nested.get("progress") or envelope.get("progress") or ""
    return {
        "id": resolved_id,
        "status": status,
        "content": content,
        "error": error_obj,
        "resolution": nested.get("resolution") or outer.get("resolution") or envelope.get("resolution"),
        "ratio": nested.get("aspect_ratio") or nested.get("ratio") or outer.get("aspect_ratio") or outer.get("ratio"),
        "estimated_remaining_seconds": int(
            nested.get("estimated_remaining_seconds") or outer.get("estimated_remaining_seconds") or 0
        ),
        "progress": progress,
    }


async def health() -> dict:
    if not settings.seedance_api_key:
        return {"configured": False, "status": "missing_key"}
    info = {
        "configured": True,
        "status": "ok",
        "gateway": "toapis",
        "model": settings.seedance_model,
        "default_model": normalize_model(),
        "api_model": api_model(),
        "models": SEEDANCE_MODELS,
        "base_url": settings.seedance_base_url,
        "assets_ready": assets_ready(),
    }
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(_base() + "/v1/balance", headers=_auth_headers())
            data = resp.json() if resp.content else {}
        if resp.status_code >= 400:
            info["status"] = "error"
            err = data.get("error") if isinstance(data, dict) else None
            info["error"] = (err or {}).get("message") if isinstance(err, dict) else data.get("message") or resp.text[:200]
        elif isinstance(data, dict) and data.get("success") is False:
            info["status"] = "error"
            info["error"] = data.get("message") or "余额查询失败"
        elif isinstance(data, dict):
            info["remain_balance"] = data.get("remain_balance")
            info["remain_credits"] = data.get("remain_credits")
    except Exception as exc:
        info["status"] = "error"
        info["error"] = str(exc)
    return info


def _public_or_asset_url(item: Path | str) -> str | None:
    if isinstance(item, str):
        uri = normalize_asset_uri(item)
        if uri:
            return uri
        if item.startswith("http://") or item.startswith("https://") or item.startswith("data:"):
            return item
        return None
    return None


def _audio_data_url(path: Path) -> str:
    raw = path.read_bytes()
    if len(raw) > 15 * 1024 * 1024:
        raise SeedanceError("参考音频超过 15MB，请剪到 2–15 秒的 MP3/WAV")
    suffix = path.suffix.lower()
    mime = "audio/wav" if suffix == ".wav" else "audio/mpeg"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


async def create_task(
    prompt: str,
    *,
    duration: int,
    ratio: str,
    resolution: str,
    images: list,
    idempotency_key: str = "",
    videos: list | None = None,
    audios: list | None = None,
    model: str = "",
) -> str:
    if not settings.seedance_api_key:
        raise SeedanceError("未配置 SEEDANCE_API_KEY")
    model_id = api_model(model)
    image_with_roles: list[dict] = []
    for item in images[:9]:
        url = await _resolve_image_url(item)
        if url:
            image_with_roles.append({"url": url, "role": "reference_image"})
    video_with_roles: list[dict] = []
    for item in (videos or [])[:3]:
        url = await _resolve_video_url(item)
        if url:
            video_with_roles.append({"url": url, "role": "reference_video"})
    audio_with_roles: list[dict] = []
    for item in (audios or [])[:3]:
        url = await _resolve_audio_url(item)
        if url:
            audio_with_roles.append({"url": url, "role": "reference_audio"})
    if audio_with_roles and not image_with_roles and not video_with_roles:
        raise SeedanceError("参考音频必须配合角色图或参考视频一起出片")
    payload: dict = {
        "model": model_id,
        "prompt": prompt,
        "duration": map_duration(duration, model_id),
        "aspect_ratio": ratio or "9:16",
        "resolution": map_resolution(resolution, model_id),
        "generate_audio": True,
    }
    if idempotency_key:
        payload["client_business_id"] = idempotency_key[:128]
    if image_with_roles:
        payload["image_with_roles"] = image_with_roles
    if video_with_roles:
        payload["video_with_roles"] = video_with_roles
    if audio_with_roles:
        payload["audio_with_roles"] = audio_with_roles
    url = _base() + "/v1/videos/generations"
    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(url, headers=_json_headers(), json=payload)
        data = _raise_for_api(resp)
    task_id = data.get("id") or data.get("task_id")
    if not task_id and isinstance(data.get("data"), dict):
        task_id = data["data"].get("id") or data["data"].get("task_id")
    if not task_id:
        raise SeedanceError(f"创建任务未返回 id: {data}")
    return str(task_id)


async def query_task(task_id: str) -> dict:
    url = _base() + f"/v1/videos/generations/{task_id}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, headers=_json_headers())
        data = _raise_for_api(resp)
    return normalize_task(data, task_id)


async def cancel_task(task_id: str) -> dict:
    return {"id": task_id, "status": "cancelled"}


def apply_remote_status(clip: VideoClip, task: dict) -> None:
    status = str(task.get("status") or "")
    clip.task_id = str(task.get("id") or task.get("task_id") or clip.task_id)
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


def video_url_from_task(task: dict) -> str:
    return _task_video_url_raw(task)


async def download_task_video(task: dict, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = video_url_from_task(task)
    if not url:
        raise SeedanceError("任务成功但没有视频地址")
    async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as client:
        async with client.stream("GET", url, headers=_auth_headers()) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise SeedanceError(f"下载视频失败 {resp.status_code}: {body[:400]!r}")
            with dest.open("wb") as fh:
                async for chunk in resp.aiter_bytes():
                    fh.write(chunk)
    return dest


def video_ref_rejected(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(
        key in msg
        for key in (
            "video_url",
            "reference_video",
            "video_with_roles",
            "invalid",
            "unsupported",
            "reference",
        )
    )


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {"group_id": "", "hashes": {}}
    try:
        raw = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"group_id": "", "hashes": {}}
    if not isinstance(raw, dict):
        return {"group_id": "", "hashes": {}}
    raw.setdefault("group_id", "")
    raw.setdefault("hashes", {})
    return raw


def _save_cache(cache: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _jpeg_bytes(path: Path, max_side: int = 1280, quality: int = 85) -> bytes:
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
    return buf.getvalue()


async def _api_json(method: str, path: str, body: dict | None = None) -> dict:
    url = _base() + path
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.request(method, url, headers=_json_headers(), json=body)
        return _raise_for_api(resp)


async def _upload_image(path: Path) -> str:
    payload = _jpeg_bytes(path)
    filename = f"{path.stem[:40] or 'image'}.jpg"
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            _base() + "/v1/uploads/images",
            headers=_auth_headers(),
            files={"file": (filename, payload, "image/jpeg")},
        )
        result = _raise_for_api(resp)
    url = str(_nested_dict(result.get("data")).get("url") or result.get("url") or "")
    if not url.startswith("http"):
        raise SeedanceError(f"图片上传未返回 URL：{result}")
    return url


async def _upload_video(path: Path) -> str:
    async with httpx.AsyncClient(timeout=180.0) as client:
        with path.open("rb") as fh:
            resp = await client.post(
                _base() + "/v1/uploads/videos",
                headers=_auth_headers(),
                files={"file": (path.name or "clip.mp4", fh, "video/mp4")},
            )
        result = _raise_for_api(resp)
    url = str(_nested_dict(result.get("data")).get("url") or result.get("url") or "")
    if not url.startswith("http"):
        raise SeedanceError(f"视频上传未返回 URL：{result}")
    return url


async def _resolve_image_url(item: Path | str) -> str | None:
    direct = _public_or_asset_url(item)
    if direct:
        return direct
    path = Path(item)
    if not path.exists():
        return None
    return await _upload_image(path)


async def _resolve_video_url(item: Path | str) -> str | None:
    direct = _public_or_asset_url(item)
    if direct:
        return direct
    path = Path(item)
    if not path.exists():
        return None
    return await _upload_video(path)


async def _resolve_audio_url(item: Path | str) -> str | None:
    direct = _public_or_asset_url(item)
    if direct:
        return direct
    path = Path(item)
    if not path.exists():
        return None
    return _audio_data_url(path)


async def _ensure_avatar_group() -> str:
    cache = _load_cache()
    cached = str(cache.get("group_id") or "")
    if cached:
        return cached
    created = await _api_json(
        "POST",
        f"{_AVATAR_PREFIX}/groups",
        {"name": "chaifen-characters", "description": "剧本工坊角色虚拟人像"},
    )
    info = _nested_dict(created.get("data"))
    group_id = str(info.get("group_id") or created.get("group_id") or "")
    if not group_id:
        raise SeedanceError(f"虚拟人像素材组创建失败：{created}")
    cache["group_id"] = group_id
    _save_cache(cache)
    return group_id


async def _notify(on_status, message: str) -> None:
    if on_status:
        await on_status(message)


async def _poll_avatar(asset_id: str, digest: str, label: str, on_status=None) -> str:
    cache = _load_cache()
    deadline = asyncio.get_event_loop().time() + 120
    last: dict = {}
    ticks = 0
    while asyncio.get_event_loop().time() < deadline:
        polled = await _api_json("GET", f"{_AVATAR_PREFIX}/assets/{asset_id}")
        last = _nested_dict(polled.get("data")) or polled
        status = str(last.get("status") or "").lower()
        uri = normalize_asset_uri(str(last.get("asset_url") or last.get("asset_id") or asset_id))
        if status == "active" and uri:
            cache.setdefault("hashes", {})[digest] = uri
            _save_cache(cache)
            return uri
        if status == "failed":
            raise SeedanceError(f"{label}素材审核失败：{last.get('message') or last}")
        ticks += 1
        if ticks == 1 or ticks % 2 == 0:
            await _notify(on_status, f"{label}审核中（{status or '排队'}）…")
        await asyncio.sleep(3)
    raise SeedanceError(f"{label}入库超时（仍是 {last.get('status') or '未知状态'}）")


async def ingest_character_image(path: Path, *, owner: str, character: str, on_status=None) -> str:
    if not path.exists():
        raise SeedanceError(f"找不到角色图：{path}")
    digest = _file_digest(path)
    cache = _load_cache()
    cached = (cache.get("hashes") or {}).get(digest)
    if cached:
        return str(cached)
    await _notify(on_status, f"正在上传角色图：{character}")
    source_url = await _upload_image(path)
    group_id = await _ensure_avatar_group()
    created = await _api_json(
        "POST",
        f"{_AVATAR_PREFIX}/assets",
        {
            "group_id": group_id,
            "asset_type": "image",
            "source_url": source_url,
            "name": character[:128] or "character",
        },
    )
    info = _nested_dict(created.get("data"))
    asset_id = str(info.get("asset_id") or created.get("asset_id") or "")
    if not asset_id:
        raise SeedanceError(f"角色「{character}」上传未返回素材 ID：{created}")
    uri = normalize_asset_uri(str(info.get("asset_url") or asset_id))
    if str(info.get("status") or "").lower() == "active" and uri:
        cache.setdefault("hashes", {})[digest] = uri
        _save_cache(cache)
        return uri
    return await _poll_avatar(asset_id, digest, f"角色「{character}」", on_status=on_status)


async def ingest_character_audio(path: Path, *, owner: str, character: str) -> str:
    if not path.exists():
        raise SeedanceError(f"找不到参考音频：{path}")
    digest = _file_digest(path)
    cache = _load_cache()
    cached = (cache.get("hashes") or {}).get(digest)
    if cached:
        return str(cached)
    source_url = _audio_data_url(path)
    group_id = await _ensure_avatar_group()
    try:
        created = await _api_json(
            "POST",
            f"{_AVATAR_PREFIX}/assets",
            {
                "group_id": group_id,
                "asset_type": "audio",
                "source_url": source_url,
                "name": f"{character[:100]}-voice",
            },
        )
    except SeedanceError:
        return ""
    info = _nested_dict(created.get("data"))
    asset_id = str(info.get("asset_id") or created.get("asset_id") or "")
    if not asset_id:
        return ""
    try:
        return await _poll_avatar(asset_id, digest, f"角色「{character}」声线")
    except SeedanceError:
        return ""


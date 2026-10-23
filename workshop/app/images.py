from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from .config import settings


class ImageAPIError(RuntimeError):
    pass


@dataclass
class ImageGenerationResult:
    path: Path
    job_id: str = ""
    generated_prompt: str = ""
    source_url: str = ""
    source_path: str = ""
    verified: bool = True
    warnings: list[str] = field(default_factory=list)
    sync_mode: str = "generated"
    stamp: str = ""


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.image_api_key}",
        "Content-Type": "application/json",
    }


async def health() -> dict:
    url = settings.image_api_base.rstrip("/") + "/health"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        return {"status": "down", "error": str(exc)}


def _path_from_file_url(url: str) -> Path:
    parsed = urlparse(url)
    path = unquote(parsed.path)
    if parsed.netloc and ":" in parsed.netloc:
        return Path(parsed.netloc + path)
    if path.startswith("/") and len(path) >= 3 and path[2] == ":":
        return Path(path[1:])
    return Path(path)


def _normalize_prompt(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _prompts_match(expected: str, actual: str) -> bool:
    a = _normalize_prompt(expected)
    b = _normalize_prompt(actual)
    if not a:
        return True
    if not b:
        return False
    if a == b:
        return True
    for prefix in ("请生成一张图片：", "请基于参考图生成一张新图片"):
        if b == f"{prefix}{a}" or a == f"{prefix}{b}":
            return True
    return False


def _assert_prompt_match(expected: str, actual: str) -> None:
    if not expected:
        return
    if not actual:
        raise ImageAPIError("平台任务没有记录提示词，无法确认是不是这张参考图，已跳过。")
    if not _prompts_match(expected, actual):
        raise ImageAPIError("平台这张图的提示词与当前参考图不一致，已跳过以免拉错图。")


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_TIMESTAMP_IMAGE = re.compile(r"^image_\d{9,12}\.[a-z0-9]+$", re.I)


def _unreliable_source(path: str) -> bool:
    name = Path(path or "").name
    return bool(_TIMESTAMP_IMAGE.match(name))


def _same_file(left: Path, right: Path) -> bool:
    try:
        if left.resolve() == right.resolve():
            return True
        if left.exists() and right.exists() and _file_hash(left) == _file_hash(right):
            return True
    except OSError:
        return False
    return False


def _payload_usable(payload: dict, expected_prompt: str, dest: Path) -> bool:
    if not _prompts_match(expected_prompt, str(payload.get("prompt") or "")):
        return False
    local_path = str(payload.get("local_path") or "")
    url = str(payload.get("url") or "")
    if local_path and _unreliable_source(local_path):
        return False
    if url:
        parsed = urlparse(url)
        if _unreliable_source(Path(parsed.path).name):
            return False
    if local_path and dest.exists() and _same_file(Path(local_path), dest):
        return False
    return True


def _validate_image_file(path: Path) -> None:
    if not path.exists():
        raise ImageAPIError(f"图片文件不存在: {path}")
    size = path.stat().st_size
    if size <= 512:
        raise ImageAPIError(f"图片文件过小（{size} 字节），可能不是有效出图结果")


async def _download_http(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        resp = await client.get(url, headers=_headers())
        if resp.status_code >= 400:
            raise ImageAPIError(f"下载出图失败 {resp.status_code}: {resp.text[:300]}")
        tmp.write_bytes(resp.content)
    tmp.replace(dest)
    _validate_image_file(dest)
    return dest


def _copy_local(source: Path, dest: Path) -> Path:
    if not source.exists():
        raise ImageAPIError(f"本地出图文件不存在: {source}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    shutil.copy2(source, tmp)
    tmp.replace(dest)
    _validate_image_file(dest)
    return dest


async def _save_job_payload(
    payload: dict,
    dest: Path,
    *,
    expected_prompt: str = "",
) -> ImageGenerationResult:
    local_path = payload.get("local_path") or ""
    url = payload.get("url") or ""
    generated_prompt = str(payload.get("prompt") or "")
    job_id = str(payload.get("id") or "")
    _assert_prompt_match(expected_prompt, generated_prompt)
    warnings: list[str] = []

    saved: Path | None = None
    errors: list[str] = []

    if url and url.startswith(("http://", "https://")):
        try:
            saved = await _download_http(url, dest)
        except Exception as exc:
            errors.append(f"HTTP 下载失败: {exc}")

    if saved is None:
        candidates: list[Path] = []
        if local_path:
            candidates.append(Path(local_path))
        if url and url.startswith("file://"):
            candidates.append(_path_from_file_url(url))
        for source in candidates:
            try:
                saved = _copy_local(source, dest)
                break
            except Exception as exc:
                errors.append(f"复制 {source} 失败: {exc}")

    if saved is None:
        detail = "；".join(errors) if errors else "无可用来源"
        raise ImageAPIError(f"无法保存出图结果: {detail}")

    if local_path:
        source = Path(local_path)
        if source.exists() and source.resolve() != saved.resolve():
            try:
                if _file_hash(source) != _file_hash(saved):
                    warnings.append("平台本地文件与下载结果不一致，已优先使用 HTTP 下载版本。")
            except OSError:
                pass

    return ImageGenerationResult(
        path=saved,
        job_id=job_id,
        generated_prompt=generated_prompt,
        source_url=url if url.startswith(("http://", "https://")) else "",
        source_path=local_path,
        verified=not warnings,
        warnings=warnings,
        stamp=str(saved.stat().st_mtime_ns),
    )


async def get_job_payload(job_id: str) -> dict:
    base = settings.image_api_base.rstrip("/")
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(f"{base}/v1/images/jobs/{job_id}", headers=_headers())
        if resp.status_code == 404:
            raise ImageAPIError(f"出图任务不存在: {job_id}")
        if resp.status_code >= 400:
            raise ImageAPIError(f"查询出图任务失败 {resp.status_code}: {resp.text[:800]}")
        payload = resp.json()
        payload.setdefault("id", job_id)
        return payload


async def fetch_job_result(
    job_id: str,
    dest: Path,
    *,
    expected_prompt: str = "",
    poll_seconds: int = 180,
    sync_mode: str = "job",
) -> ImageGenerationResult:
    base = settings.image_api_base.rstrip("/")
    poll_url = f"{base}/v1/images/jobs/{job_id}"
    deadline = asyncio.get_event_loop().time() + poll_seconds
    async with httpx.AsyncClient(timeout=20.0) as client:
        while asyncio.get_event_loop().time() < deadline:
            resp = await client.get(poll_url, headers=_headers())
            if resp.status_code == 404:
                raise ImageAPIError(f"出图任务不存在: {job_id}")
            if resp.status_code >= 400:
                raise ImageAPIError(f"查询出图任务失败 {resp.status_code}: {resp.text[:800]}")
            payload = resp.json()
            status = payload.get("status")
            if status == "completed":
                payload.setdefault("id", job_id)
                result = await _save_job_payload(payload, dest, expected_prompt=expected_prompt)
                result.sync_mode = sync_mode
                return result
            if status == "failed":
                raise ImageAPIError(payload.get("error") or "出图失败")
            if status in {None, "", "pending", "queued", "running", "processing"}:
                await asyncio.sleep(2)
                continue
            await asyncio.sleep(2)
        raise ImageAPIError("等待出图任务完成超时")


async def find_matching_jobs(prompt: str, *, limit: int = 5) -> list[dict]:
    base = settings.image_api_base.rstrip("/")
    params = {"prompt": prompt, "status": "completed", "exact": "true", "limit": limit}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"{base}/v1/images/jobs", headers=_headers(), params=params)
        if resp.status_code == 404:
            return []
        if resp.status_code >= 400:
            raise ImageAPIError(f"查找出图历史失败 {resp.status_code}: {resp.text[:800]}")
        data = resp.json().get("data") or []
        return [item for item in data if isinstance(item, dict)]


def _error_detail(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
        detail = payload.get("detail") if isinstance(payload, dict) else payload
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        if isinstance(detail, list) and detail:
            return str(detail[0])
    except Exception:
        pass
    return resp.text[:800]


async def recover_from_conversation(prompt: str, dest: Path) -> ImageGenerationResult:
    """从 ChatGPT 对话历史里按提示词找回已出过的图，不重新生成。"""
    base = settings.image_api_base.rstrip("/")
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{base}/v1/images/recover",
            headers=_headers(),
            json={"prompt": prompt},
        )
    if resp.status_code == 404:
        raise ImageAPIError(_error_detail(resp) or "对话里没有找到这张提示词对应的图。")
    if resp.status_code >= 400:
        raise ImageAPIError(f"从对话找回图片失败 {resp.status_code}: {_error_detail(resp)}")
    payload = resp.json()
    payload.setdefault("prompt", prompt)
    result = await _save_job_payload(payload, dest, expected_prompt=prompt)
    result.sync_mode = "conversation"
    return result


async def sync_image(
    prompt: str,
    dest: Path,
    *,
    job_id: str = "",
    source_path: str = "",
    source_url: str = "",
    generated_prompt: str = "",
    allow_generate: bool = False,
    poll_seconds: int = 180,
) -> ImageGenerationResult:
    """只拉取提示词完全一致、且文件名可靠的已生成图。"""
    candidates: list[dict] = []
    if job_id:
        try:
            payload = await get_job_payload(job_id)
            if payload.get("status") == "completed" and _payload_usable(payload, prompt, dest):
                candidates.append(payload)
        except ImageAPIError:
            pass

    matches = await find_matching_jobs(prompt, limit=8)
    for item in matches:
        if item.get("id") and any(item.get("id") == c.get("id") for c in candidates):
            continue
        if _payload_usable(item, prompt, dest):
            candidates.append(item)

    for payload in candidates:
        try:
            result = await _save_job_payload(payload, dest, expected_prompt=prompt)
            result.sync_mode = "job" if payload.get("id") == job_id else "match"
            return result
        except ImageAPIError:
            continue

    if source_path and not _unreliable_source(source_path):
        if generated_prompt and _prompts_match(prompt, generated_prompt) and not (dest.exists() and _same_file(Path(source_path), dest)):
            try:
                payload = {
                    "id": job_id,
                    "prompt": generated_prompt,
                    "local_path": source_path,
                    "url": source_url,
                }
                if _payload_usable(payload, prompt, dest):
                    result = await _save_job_payload(payload, dest, expected_prompt=prompt)
                    result.sync_mode = "source"
                    return result
            except ImageAPIError:
                pass

    recover_error = ""
    try:
        result = await recover_from_conversation(prompt, dest)
        return result
    except ImageAPIError as exc:
        recover_error = str(exc)

    if not allow_generate:
        raise ImageAPIError(
            "本地任务里没有可用的对应图。"
            + (f" 对话扫描：{recover_error}" if recover_error else " 对话里也没找到。")
            + " 未重新出图。请点卡片上的「保存并重出」。"
        )

    result = await generate_image(prompt, dest, poll_seconds=poll_seconds)
    result.sync_mode = "generated"
    return result


async def generate_image(
    prompt: str,
    dest: Path,
    *,
    poll_seconds: int = 180,
) -> ImageGenerationResult:
    base = settings.image_api_base.rstrip("/")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{base}/v1/images/generations",
            headers=_headers(),
            json={"prompt": prompt, "async_mode": True, "keep_conversation": True},
        )
        if resp.status_code >= 400:
            raise ImageAPIError(f"出图请求失败 {resp.status_code}: {resp.text[:800]}")
        data = resp.json()

    if data.get("data"):
        item = data["data"][0]
        item.setdefault("prompt", prompt)
        result = await _save_job_payload(item, dest, expected_prompt=prompt)
        result.sync_mode = "generated"
        return result

    job_id = data.get("id")
    poll_url = data.get("poll_url") or (f"/v1/images/jobs/{job_id}" if job_id else None)
    if not poll_url:
        raise ImageAPIError(f"出图接口未返回任务 ID: {data}")

    deadline = asyncio.get_event_loop().time() + poll_seconds
    unknown_streak = 0
    async with httpx.AsyncClient(timeout=20.0) as client:
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(3)
            job = await client.get(base + poll_url, headers=_headers())
            if job.status_code >= 400:
                raise ImageAPIError(f"轮询出图失败 {job.status_code}: {job.text[:800]}")
            payload = job.json()
            status = payload.get("status")
            if status == "completed":
                payload.setdefault("id", job_id or "")
                payload.setdefault("prompt", payload.get("prompt") or prompt)
                result = await _save_job_payload(payload, dest, expected_prompt=prompt)
                result.sync_mode = "generated"
                return result
            if status == "failed":
                raise ImageAPIError(payload.get("error") or "出图失败")
            if status in {None, "", "pending", "queued", "running", "processing"}:
                unknown_streak = 0
                continue
            unknown_streak += 1
            if unknown_streak >= 5:
                raise ImageAPIError(f"出图任务状态异常：{status}")
        raise ImageAPIError("等待图片生成超时")


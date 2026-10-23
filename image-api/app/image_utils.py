from __future__ import annotations

import base64
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def unique_image_path(output_dir: Path, suffix: str = ".png") -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    ext = suffix if suffix.startswith(".") else f".{suffix}"
    if ext == ".jpeg":
        ext = ".jpg"
    return output_dir / f"image_{uuid.uuid4().hex}{ext}"


@dataclass
class ReferenceImage:
    data: bytes
    filename: str
    mime_type: str
    source_path: str | None = None


def _guess_mime(filename: str, data: bytes) -> str:
    ext = Path(filename).suffix.lower()
    if ext in MIME_BY_EXT:
        return MIME_BY_EXT[ext]
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


async def resolve_reference_image(
    *,
    base64_data: str | None = None,
    url: str | None = None,
    path: str | None = None,
) -> ReferenceImage | None:
    images = await resolve_reference_images(
        base64_list=[base64_data] if base64_data else None,
        url_list=[url] if url else None,
        path_list=[path] if path else None,
    )
    return images[0] if images else None


async def _resolve_one_base64(raw: str, index: int) -> ReferenceImage:
    raw = raw.strip()
    filename = f"reference_{index}.png"
    match = re.match(r"data:(image/[^;]+);base64,(.+)", raw, re.S)
    if match:
        mime_type = match.group(1)
        raw = match.group(2)
        ext = mime_type.split("/")[-1]
        filename = f"reference_{index}.{ext}"
    data = base64.b64decode(raw)
    return ReferenceImage(data=data, filename=filename, mime_type=_guess_mime(filename, data))


async def resolve_reference_images(
    *,
    base64_list: list[str] | None = None,
    url_list: list[str] | None = None,
    path_list: list[str] | None = None,
) -> list[ReferenceImage]:
    images: list[ReferenceImage] = []

    if base64_list:
        for index, item in enumerate(base64_list):
            if item:
                images.append(await _resolve_one_base64(item, index))

    if path_list:
        for index, item in enumerate(path_list):
            if not item:
                continue
            file_path = Path(item)
            if not file_path.exists():
                raise FileNotFoundError(f"参考图不存在: {item}")
            data = file_path.read_bytes()
            images.append(
                ReferenceImage(
                    data=data,
                    filename=file_path.name,
                    mime_type=_guess_mime(file_path.name, data),
                    source_path=str(file_path.resolve()),
                )
            )

    if url_list:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            for index, item in enumerate(url_list):
                if not item:
                    continue
                response = await client.get(item)
                response.raise_for_status()
                data = response.content
                filename = Path(item.split("?", 1)[0]).name or f"reference_{index}.png"
                images.append(
                    ReferenceImage(
                        data=data,
                        filename=filename,
                        mime_type=_guess_mime(filename, data),
                    )
                )

    return images


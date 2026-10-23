from __future__ import annotations

from pathlib import Path

from .config import OUTPUT_DIR
from .image_task_manager import image_task_manager
from .image_utils import ReferenceImage
from .protocol_client import protocol_client


async def chat_via_playwright(messages: list[dict[str, str]]) -> str:
    content, _ = await protocol_client.chat(messages)
    return content


async def image_via_playwright(
    prompt: str,
    output_dir: Path = OUTPUT_DIR,
    *,
    keep_conversation: bool = True,
    new_conversation: bool = False,
    reference_images: list[ReferenceImage] | None = None,
) -> Path:
    job = await image_task_manager.submit(
        prompt,
        keep_conversation=keep_conversation,
        new_conversation=new_conversation,
        reference_images=reference_images,
        wait=True,
    )
    if job.status.value == "failed":
        raise RuntimeError(job.error or "图片生成失败")
    return Path(job.result_path or "")


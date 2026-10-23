from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .browser_helper import cdp_diagnostics
from .chat_client import chat_completion
from .config import API_KEY, HOST, IMAGE_MAX_CONCURRENT, IMAGE_MODE, IMAGE_REUSE_CONVERSATION, OUTPUT_DIR, PORT, PUBLIC_BASE_URL
from .image_task_manager import JobStatus, image_task_manager
from .image_utils import resolve_reference_images
from .playwright_client import chat_via_playwright, image_via_playwright
from .protocol_client import protocol_client
from .session import SessionManager

app = FastAPI(
    title="ChatGPT Web Local API",
    description="本地 OpenAI 兼容 API：HTTP 协议直连 ChatGPT backend-api。",
    version="0.2.0",
)

session_manager = SessionManager()


def image_file_urls(path: Path, request: Request | None = None) -> dict[str, str]:
    filename = path.name
    if request is not None:
        base = str(request.base_url).rstrip("/")
    elif PUBLIC_BASE_URL:
        base = PUBLIC_BASE_URL
    else:
        host = "127.0.0.1" if HOST in {"0.0.0.0", "::"} else HOST
        base = f"http://{host}:{PORT}"
    return {
        "url": f"{base}/output/{filename}",
        "local_path": str(path),
        "filename": filename,
    }


def verify_api_key(authorization: str | None = Header(default=None)) -> None:
    if not API_KEY:
        return
    expected = f"Bearer {API_KEY}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Invalid API key")


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = "auto"
    messages: list[ChatMessage]
    stream: bool = False
    conversation_id: str | None = None


class ImageGenerationRequest(BaseModel):
    prompt: str
    model: str = "gpt-image-1"
    size: str | None = None
    keep_conversation: bool = True
    new_conversation: bool = False
    async_mode: bool = False
    reference_image_base64: str | None = None
    reference_image_url: str | None = None
    reference_image_path: str | None = None
    reference_images_base64: list[str] | None = None
    reference_images_url: list[str] | None = None
    reference_images_path: list[str] | None = None


class ImageBatchRequest(BaseModel):
    prompts: list[str] = Field(min_length=1, max_length=20)
    keep_conversation: bool = False


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/session")
async def get_session(_: None = Depends(verify_api_key)) -> dict[str, Any]:
    try:
        session = await protocol_client.session(force=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "mode": "protocol",
        "image_mode": IMAGE_MODE,
        "image_reuse_conversation": IMAGE_REUSE_CONVERSATION,
        "email": session.email,
        "expires_at": session.expires_at,
        "has_token": bool(session.access_token),
        "device_id": session.device_id,
    }


@app.post("/v1/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest,
    _: None = Depends(verify_api_key),
) -> Any:
    messages = [msg.model_dump() for msg in body.messages]

    if body.stream:
        raise HTTPException(status_code=501, detail="流式请先用非 stream 模式；Cookie 失败时会走 Playwright。")

    content = ""
    conversation_id = body.conversation_id
    try:
        content, conversation_id = await protocol_client.chat(messages, model=body.model)
    except Exception:
        try:
            session = await session_manager.get()
            content, conversation_id = await chat_completion(
                session,
                messages,
                model=body.model,
                conversation_id=body.conversation_id,
            )
        except Exception:
            try:
                content = await chat_via_playwright(messages)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "id": f"chatcmpl-{uuid.uuid4()}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "conversation_id": conversation_id,
    }


@app.post("/v1/conversations/cleanup")
async def cleanup_conversations(
    _: None = Depends(verify_api_key),
) -> dict[str, int]:
    deleted = await protocol_client.cleanup_conversations()
    return {"deleted": deleted}


@app.get("/v1/images/page-state")
async def image_page_state(_: None = Depends(verify_api_key)) -> dict[str, object]:
    from .playwright_session import ensure_logged_in, get_chatgpt_page

    page = await get_chatgpt_page()
    await ensure_logged_in(page)
    return await page.evaluate(
        """
        () => ({
          url: location.href,
          title: document.title,
          snippet: (document.body.innerText || '').slice(0, 600),
          selectors: {
            prose: !!document.querySelector("div.ProseMirror[contenteditable='true']"),
            promptTextarea: !!document.querySelector('#prompt-textarea'),
            testidPrompt: !!document.querySelector('[data-testid="prompt-textarea"]'),
            login: !!document.querySelector('[data-testid="login-button"]'),
          },
        })
        """
    )


@app.get("/v1/images/diagnostics")
async def image_diagnostics(_: None = Depends(verify_api_key)) -> dict[str, object]:
    from .playwright_session import browser_session_info

    info = cdp_diagnostics()
    info["playwright_session"] = await browser_session_info()
    return info


@app.post("/v1/images/generations")
async def image_generations(
    body: ImageGenerationRequest,
    request: Request,
    _: None = Depends(verify_api_key),
) -> JSONResponse:
    try:
        base64_list = list(body.reference_images_base64 or [])
        if body.reference_image_base64:
            base64_list.insert(0, body.reference_image_base64)
        url_list = list(body.reference_images_url or [])
        if body.reference_image_url:
            url_list.insert(0, body.reference_image_url)
        path_list = list(body.reference_images_path or [])
        if body.reference_image_path:
            path_list.insert(0, body.reference_image_path)

        reference_images = await resolve_reference_images(
            base64_list=base64_list or None,
            url_list=url_list or None,
            path_list=path_list or None,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if body.async_mode:
        job = await image_task_manager.submit(
            body.prompt,
            keep_conversation=body.keep_conversation,
            new_conversation=body.new_conversation,
            reference_images=reference_images,
            wait=False,
        )
        return JSONResponse(
            {
                "id": job.id,
                "status": job.status.value,
                "poll_url": f"/v1/images/jobs/{job.id}",
            }
        )

    try:
        image_path = await image_via_playwright(
            body.prompt,
            keep_conversation=body.keep_conversation,
            new_conversation=body.new_conversation,
            reference_images=reference_images,
        )
    except Exception as exc:  # noqa: BLE001
        detail = str(exc).encode("utf-8", "replace").decode("utf-8")
        raise HTTPException(status_code=502, detail=detail) from exc

    return JSONResponse(
        {
            "created": int(time.time()),
            "data": [image_file_urls(image_path, request)],
        }
    )


@app.post("/v1/images/generations/batch")
async def image_generations_batch(
    body: ImageBatchRequest,
    request: Request,
    _: None = Depends(verify_api_key),
) -> JSONResponse:
    jobs = await image_task_manager.submit_batch(
        body.prompts,
        keep_conversation=body.keep_conversation,
    )
    results = []
    for job in jobs:
        item = {
            "id": job.id,
            "prompt": job.prompt,
            "status": job.status.value,
        }
        if job.status == JobStatus.COMPLETED and job.result_path:
            item.update(image_file_urls(Path(job.result_path), request))
        if job.status == JobStatus.FAILED:
            item["error"] = job.error
        results.append(item)

    return JSONResponse({"created": int(time.time()), "data": results})


@app.post("/v1/images/save-from-page")
async def save_image_from_page(
    request: Request,
    _: None = Depends(verify_api_key),
) -> JSONResponse:
    """页面已有生成图但 output 未落盘时，强制从当前 ChatGPT 页面保存。"""
    from .image_client import _force_save_page_image
    from .playwright_session import ensure_logged_in, get_chatgpt_page

    page = await get_chatgpt_page()
    await ensure_logged_in(page)
    await protocol_client.ensure_image_conversation(page)
    try:
        image_path = await _force_save_page_image(
            page,
            OUTPUT_DIR,
            conversation_turn_before=-1,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return JSONResponse(
        {
            "created": int(time.time()),
            "data": [image_file_urls(image_path, request)],
        }
    )


class ImageRecoverRequest(BaseModel):
    prompt: str


@app.post("/v1/images/recover")
async def recover_image_from_conversation(
    body: ImageRecoverRequest,
    request: Request,
    _: None = Depends(verify_api_key),
) -> JSONResponse:
    """从当前/最近的 ChatGPT 对话里按提示词找回已生成图，不重新出图。"""
    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="缺少 prompt")
    try:
        job = await image_task_manager.recover(prompt)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not job.result_path:
        raise HTTPException(status_code=404, detail="对话中未找到对应图片")
    payload: dict[str, object] = {
        "id": job.id,
        "prompt": job.prompt,
        "status": job.status.value,
    }
    payload.update(image_file_urls(Path(job.result_path), request))
    return JSONResponse(payload)


@app.get("/v1/images/jobs")
async def list_image_jobs(
    request: Request,
    prompt: str | None = None,
    status: str | None = "completed",
    exact: bool = True,
    limit: int = 20,
    _: None = Depends(verify_api_key),
) -> JSONResponse:
    job_status = None
    if status:
        try:
            job_status = JobStatus(status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"无效 status: {status}") from exc
    jobs = image_task_manager.list_jobs(status=job_status, prompt=prompt, exact=exact, limit=limit)
    data = []
    for job in jobs:
        item: dict[str, object] = {
            "id": job.id,
            "prompt": job.prompt,
            "status": job.status.value,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
        }
        if job.result_path:
            item.update(image_file_urls(Path(job.result_path), request))
        if job.error:
            item["error"] = job.error
        data.append(item)
    return JSONResponse({"data": data})


@app.get("/v1/images/jobs/{job_id}")
async def image_job_status(
    job_id: str,
    request: Request,
    _: None = Depends(verify_api_key),
) -> JSONResponse:
    job = image_task_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    payload: dict[str, object] = {
        "id": job.id,
        "prompt": job.prompt,
        "status": job.status.value,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }
    if job.result_path:
        payload.update(image_file_urls(Path(job.result_path), request))
    if job.error:
        payload["error"] = job.error
    return JSONResponse(payload)


@app.get("/v1/images/queue/stats")
async def image_queue_stats(_: None = Depends(verify_api_key)) -> dict[str, object]:
    return {
        "max_concurrent": IMAGE_MAX_CONCURRENT,
        "jobs": image_task_manager.stats(),
    }


app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")


def main() -> None:
    import logging
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=False)


if __name__ == "__main__":
    main()


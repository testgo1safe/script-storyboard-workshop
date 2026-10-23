from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator

import httpx

from .config import CONVERSATION_URL, DEFAULT_USER_AGENT
from .session import ChatGPTSession


def _build_payload(
    messages: list[dict[str, str]],
    model: str,
    conversation_id: str | None = None,
    parent_message_id: str | None = None,
) -> dict[str, Any]:
    message_id = str(uuid.uuid4())
    return {
        "action": "next",
        "messages": [
            {
                "id": message_id,
                "author": {"role": msg["role"]},
                "content": {"content_type": "text", "parts": [msg["content"]]},
            }
            for msg in messages
            if msg.get("role") in {"user", "assistant", "system"}
        ],
        "model": model,
        "parent_message_id": parent_message_id or "00000000-0000-0000-0000-000000000000",
        "conversation_id": conversation_id,
        "timezone_offset_min": -480,
        "history_and_training_disabled": False,
        "force_paragen": False,
        "force_rate_limit": False,
    }


def _headers(session: ChatGPTSession) -> dict[str, str]:
    return {
        "Authorization": session.authorization,
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "Referer": "https://chatgpt.com/",
        "Origin": "https://chatgpt.com",
        "Cookie": session.cookie_header,
    }


def _extract_text(event: dict[str, Any]) -> str | None:
    message = event.get("message") or {}
    content = message.get("content") or {}
    parts = content.get("parts") or []
    if not parts:
        return None
    first = parts[0]
    if isinstance(first, str):
        return first
    return None


async def chat_completion(
    session: ChatGPTSession,
    messages: list[dict[str, str]],
    model: str = "auto",
    stream: bool = False,
    conversation_id: str | None = None,
) -> tuple[str, str | None]:
    payload = _build_payload(messages, model, conversation_id=conversation_id)

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST",
            CONVERSATION_URL,
            headers=_headers(session),
            json=payload,
        ) as response:
            if response.status_code >= 400:
                body = await response.aread()
                raise RuntimeError(
                    f"ChatGPT 返回 {response.status_code}: {body.decode('utf-8', 'ignore')[:500]}"
                )

            if stream:
                raise NotImplementedError("流式响应请使用 chat_completion_stream")

            text_parts: list[str] = []
            final_conversation_id = conversation_id

            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue

                if event.get("conversation_id"):
                    final_conversation_id = event["conversation_id"]

                extracted = _extract_text(event)
                if extracted:
                    text_parts = [extracted]

            answer = text_parts[-1] if text_parts else ""
            return answer, final_conversation_id


async def chat_completion_stream(
    session: ChatGPTSession,
    messages: list[dict[str, str]],
    model: str = "auto",
    conversation_id: str | None = None,
) -> AsyncIterator[str]:
    payload = _build_payload(messages, model, conversation_id=conversation_id)
    last_text = ""

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST",
            CONVERSATION_URL,
            headers=_headers(session),
            json=payload,
        ) as response:
            if response.status_code >= 400:
                body = await response.aread()
                raise RuntimeError(
                    f"ChatGPT 返回 {response.status_code}: {body.decode('utf-8', 'ignore')[:500]}"
                )

            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue

                extracted = _extract_text(event)
                if not extracted or extracted == last_text:
                    continue
                delta = extracted[len(last_text) :]
                last_text = extracted
                if delta:
                    yield delta


from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import (
    CHATGPT_ORIGIN,
    CONVERSATION_URL,
    DEFAULT_USER_AGENT,
    IMAGE_DELETE_CONVERSATION,
    IMAGE_MODE,
    IMAGE_REUSE_CONVERSATION,
    OUTPUT_DIR,
)
from .profile_session import WebSession, refresh_session_from_profile
from .sentinel import get_requirements_token, solve_pow
from .image_utils import unique_image_path


@dataclass
class ConversationResult:
    text: str
    conversation_id: str | None
    image_urls: list[str]


class ProtocolClient:
    _UI_CONVERSATION_FILE = OUTPUT_DIR.parent / ".ui-conversation-id"

    def __init__(self) -> None:
        self._session: WebSession | None = None
        self._ui_conversation_id: str | None = self._read_ui_conversation_id()

    @classmethod
    def _read_ui_conversation_id(cls) -> str | None:
        if cls._UI_CONVERSATION_FILE.exists():
            value = cls._UI_CONVERSATION_FILE.read_text(encoding="utf-8").strip()
            return value or None
        return None

    def _persist_ui_conversation_id(self, conversation_id: str | None) -> None:
        if conversation_id:
            self._UI_CONVERSATION_FILE.write_text(conversation_id, encoding="utf-8")
        elif self._UI_CONVERSATION_FILE.exists():
            self._UI_CONVERSATION_FILE.unlink(missing_ok=True)

    async def session(self, force: bool = False) -> WebSession:
        if force or self._session is None:
            self._session = await refresh_session_from_profile(force=force)
        elif self._session.expires_at < __import__("time").time() + 60:
            self._session = await refresh_session_from_profile(force=True)
        return self._session

    def _base_headers(self, session: WebSession) -> dict[str, str]:
        headers = {
            "Authorization": session.authorization,
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Referer": f"{CHATGPT_ORIGIN}/",
            "Origin": CHATGPT_ORIGIN,
            "Cookie": session.cookie_header,
        }
        if session.device_id:
            headers["OAI-Device-Id"] = session.device_id
        if session.account_id:
            headers["ChatGPT-Account-Id"] = session.account_id
        return headers

    async def _get_chat_page(self):
        from .playwright_session import ensure_logged_in, get_chatgpt_page

        page = await get_chatgpt_page()
        await ensure_logged_in(page)
        return page

    async def _fetch_sentinel_via_page(self, page) -> dict[str, str]:
        token_p = get_requirements_token(DEFAULT_USER_AGENT)
        data = await page.evaluate(
            """
            async (p) => {
              const res = await fetch('/backend-api/sentinel/chat-requirements', {
                method: 'POST',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json', 'Accept': '*/*' },
                body: JSON.stringify({ p }),
              });
              if (!res.ok) {
                const t = await res.text();
                throw new Error('sentinel HTTP ' + res.status + ': ' + t.slice(0, 300));
              }
              return await res.json();
            }
            """,
            token_p,
        )

        headers: dict[str, str] = {}
        chat_token = data.get("token", "")
        if chat_token:
            headers["OpenAI-Sentinel-Chat-Requirements-Token"] = chat_token

        pow_block = data.get("proofofwork") or {}
        if pow_block.get("required"):
            seed = pow_block.get("seed", "")
            diff = pow_block.get("difficulty", "")
            headers["OpenAI-Sentinel-Proof-Token"] = solve_pow(seed, diff, DEFAULT_USER_AGENT)
        else:
            headers["OpenAI-Sentinel-Proof-Token"] = ""

        return headers

    async def _sentinel_headers(self, session: WebSession) -> dict[str, str]:
        headers = self._base_headers(session)
        page = await self._get_chat_page()
        sentinel = await self._fetch_sentinel_via_page(page)
        return {**headers, **sentinel}

    def _build_payload(
        self,
        prompt: str,
        model: str = "auto",
        conversation_id: str | None = None,
        image_asset: dict[str, Any] | None = None,
        image_assets: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        message_id = str(uuid.uuid4())
        assets = image_assets or ([image_asset] if image_asset else [])
        if assets:
            content = {
                "content_type": "multimodal_text",
                "parts": [prompt, *assets],
            }
        else:
            content = {"content_type": "text", "parts": [prompt]}

        return {
            "action": "next",
            "messages": [
                {
                    "id": message_id,
                    "author": {"role": "user"},
                    "content": content,
                }
            ],
            "model": model,
            "parent_message_id": "00000000-0000-0000-0000-000000000000",
            "conversation_id": conversation_id,
            "timezone_offset_min": -480,
            "history_and_training_disabled": True,
            "force_paragen": False,
            "force_rate_limit": False,
        }

    def _extract_from_event(self, event: dict[str, Any], state: ConversationResult) -> None:
        if event.get("conversation_id"):
            state.conversation_id = event["conversation_id"]

        message = event.get("message") or {}
        content = message.get("content") or {}
        parts = content.get("parts") or []
        for part in parts:
            if isinstance(part, str) and part.strip():
                state.text = part
            elif isinstance(part, dict):
                if part.get("asset_pointer") or part.get("image_asset_pointer"):
                    pointer = part.get("asset_pointer") or part.get("image_asset_pointer")
                    if isinstance(pointer, dict) and pointer.get("url"):
                        state.image_urls.append(pointer["url"])
                if part.get("url"):
                    state.image_urls.append(part["url"])

        for key in ("image_url", "url", "download_url"):
            value = event.get(key)
            if isinstance(value, str) and value.startswith("http"):
                state.image_urls.append(value)

        nested = json.dumps(event)
        for match in re.finditer(r"https://[^\s\"']+\.(?:png|jpg|jpeg|webp)|https://files\.oaiusercontent\.com/[^\s\"']+", nested):
            state.image_urls.append(match.group(0))

    async def converse(
        self,
        prompt: str,
        *,
        model: str = "auto",
        conversation_id: str | None = None,
        reference_image: Any | None = None,
        reference_images: list[Any] | None = None,
    ) -> ConversationResult:
        from .image_utils import ReferenceImage

        refs: list[Any] = list(reference_images or [])
        if isinstance(reference_image, ReferenceImage):
            refs.insert(0, reference_image)

        await self.session()
        state = ConversationResult(text="", conversation_id=conversation_id, image_urls=[])

        image_assets: list[dict[str, Any]] = []
        for ref in refs:
            if isinstance(ref, ReferenceImage):
                image_assets.append(await self._upload_reference_image(ref))

        image_asset = image_assets[0] if len(image_assets) == 1 else None
        if len(image_assets) > 1:
            await self._stream_conversation_via_page(
                prompt, model, state, image_assets=image_assets
            )
        else:
            await self._stream_conversation_via_page(
                prompt, model, state, image_asset=image_asset
            )

        state.image_urls = list(dict.fromkeys(state.image_urls))
        return state

    async def _upload_reference_image(self, image: Any) -> dict[str, Any]:
        page = await self._get_chat_page()

        upload = await page.evaluate(
            """
            async ({ bytes, fileName, mimeType }) => {
              const blob = new Blob([new Uint8Array(bytes)], { type: mimeType });
              const form = new FormData();
              form.append('file', blob, fileName);
              const urls = [
                '/backend-api/files/process/upload?purpose=multimodal',
                '/backend-api/files/process/upload',
                '/backend-api/files',
              ];
              let lastError = '';
              for (const url of urls) {
                const res = await fetch(url, { method: 'POST', credentials: 'include', body: form });
                if (res.ok) return await res.json();
                lastError = url + ' HTTP ' + res.status + ': ' + (await res.text()).slice(0, 200);
              }
              throw new Error('参考图上传失败: ' + lastError);
            }
            """,
            {
                "bytes": list(image.data),
                "fileName": image.filename,
                "mimeType": image.mime_type,
            },
        )

        asset_pointer = (
            upload.get("asset_pointer")
            or upload.get("file_id")
            or (upload.get("file") or {}).get("asset_pointer")
            or (upload.get("file") or {}).get("id")
        )
        if not asset_pointer:
            raise RuntimeError(f"上传成功但未返回 asset_pointer: {upload}")

        if not str(asset_pointer).startswith(("file-service://", "sediment://", "file-")):
            asset_pointer = f"file-service://{asset_pointer}"

        width = upload.get("width") or (upload.get("file") or {}).get("width") or 1024
        height = upload.get("height") or (upload.get("file") or {}).get("height") or 1024
        size_bytes = upload.get("size") or (upload.get("file") or {}).get("size") or len(image.data)

        return {
            "content_type": "image_asset_pointer",
            "asset_pointer": asset_pointer,
            "size_bytes": size_bytes,
            "width": width,
            "height": height,
        }

    async def _stream_conversation(
        self,
        headers: dict[str, str],
        payload: dict[str, Any],
        state: ConversationResult,
    ) -> None:
        async with httpx.AsyncClient(timeout=180.0) as client:
            async with client.stream(
                "POST",
                CONVERSATION_URL,
                headers=headers,
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    raise RuntimeError(
                        f"conversation HTTP {response.status_code}: "
                        f"{body.decode('utf-8', 'ignore')[:500]}"
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
                    self._extract_from_event(event, state)

    async def _stream_conversation_via_page(
        self,
        prompt: str,
        model: str,
        state: ConversationResult,
        image_asset: dict[str, Any] | None = None,
        image_assets: list[dict[str, Any]] | None = None,
    ) -> None:
        """在已登录页面内用 fetch 发 backend-api 包（不键盘输入）。"""
        page = await self._get_chat_page()
        payload = self._build_payload(
            prompt,
            model=model,
            image_asset=image_asset,
            image_assets=image_assets,
        )
        errors: list[str] = []

        for mode in ("plain", "sentinel"):
            try:
                if mode == "sentinel":
                    sentinel = await self._fetch_sentinel_via_page(page)
                    raw = await page.evaluate(
                        """
                        async ({ body, sentinel }) => {
                          const res = await fetch('/backend-api/conversation', {
                            method: 'POST',
                            credentials: 'include',
                            headers: {
                              'Content-Type': 'application/json',
                              'Accept': 'text/event-stream',
                              'OpenAI-Sentinel-Chat-Requirements-Token': sentinel.chatToken || '',
                              'OpenAI-Sentinel-Proof-Token': sentinel.proofToken || '',
                            },
                            body: JSON.stringify(body),
                          });
                          if (!res.ok) {
                            const t = await res.text();
                            throw new Error('page conversation HTTP ' + res.status + ': ' + t.slice(0, 500));
                          }
                          return await res.text();
                        }
                        """,
                        {
                            "body": payload,
                            "sentinel": {
                                "chatToken": sentinel.get("OpenAI-Sentinel-Chat-Requirements-Token", ""),
                                "proofToken": sentinel.get("OpenAI-Sentinel-Proof-Token", ""),
                            },
                        },
                    )
                else:
                    raw = await page.evaluate(
                        """
                        async (body) => {
                          const res = await fetch('/backend-api/conversation', {
                            method: 'POST',
                            credentials: 'include',
                            headers: {
                              'Content-Type': 'application/json',
                              'Accept': 'text/event-stream',
                            },
                            body: JSON.stringify(body),
                          });
                          if (!res.ok) {
                            const t = await res.text();
                            throw new Error('page conversation HTTP ' + res.status + ': ' + t.slice(0, 500));
                          }
                          return await res.text();
                        }
                        """,
                        payload,
                    )

                for line in raw.splitlines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    self._extract_from_event(event, state)
                return
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{mode}: {exc}")

        raise RuntimeError("页面协议出图失败: " + " | ".join(errors))

    async def _extract_conversation_id(self, page) -> str | None:
        conversation_id = await page.evaluate(
            """
            () => {
              const pathMatch = location.pathname.match(/\\/c\\/([0-9a-f-]{36})/i);
              if (pathMatch) return pathMatch[1];
              const canonical = document.querySelector('link[rel="canonical"]')?.href || '';
              const canonicalMatch = canonical.match(/\\/c\\/([0-9a-f-]{36})/i);
              return canonicalMatch ? canonicalMatch[1] : null;
            }
            """
        )
        return conversation_id if isinstance(conversation_id, str) else None

    async def _wait_for_conversation_id(self, page, timeout: float = 30.0) -> str | None:
        import asyncio

        deadline = __import__("time").time() + timeout
        while __import__("time").time() < deadline:
            conversation_id = await self._extract_conversation_id(page)
            if conversation_id:
                return conversation_id
            await asyncio.sleep(1)
        return None

    async def _prepare_ui_conversation(self, page) -> str | None:
        import asyncio

        from .image_client import _start_new_chat

        if IMAGE_REUSE_CONVERSATION:
            if self._ui_conversation_id:
                target = f"{CHATGPT_ORIGIN}/c/{self._ui_conversation_id}"
                if self._ui_conversation_id not in page.url:
                    await page.goto(target, wait_until="domcontentloaded", timeout=90000)
                    await asyncio.sleep(2)
                return self._ui_conversation_id

            existing = await self._extract_conversation_id(page)
            if existing:
                self._ui_conversation_id = existing
                self._persist_ui_conversation_id(existing)
                return existing

            sidebar_id = await page.evaluate(
                """
                () => {
                  const links = document.querySelectorAll('nav a[href*="/c/"], aside a[href*="/c/"]');
                  for (const link of links) {
                    const m = (link.getAttribute('href') || '').match(/\\/c\\/([0-9a-f-]{36})/i);
                    if (m) return m[1];
                  }
                  return null;
                }
                """
            )
            if isinstance(sidebar_id, str) and sidebar_id:
                self._ui_conversation_id = sidebar_id
                self._persist_ui_conversation_id(sidebar_id)
                target = f"{CHATGPT_ORIGIN}/c/{sidebar_id}"
                if sidebar_id not in page.url:
                    await page.goto(target, wait_until="domcontentloaded", timeout=90000)
                    await asyncio.sleep(2)
                return sidebar_id
            return None

        await _start_new_chat(page)
        self._ui_conversation_id = None
        self._persist_ui_conversation_id(None)
        return None

    def reset_ui_conversation(self) -> None:
        self._ui_conversation_id = None
        self._persist_ui_conversation_id(None)

    async def ensure_image_conversation(self, page) -> str | None:
        return await self._prepare_ui_conversation(page)

    async def recover_image_from_conversation(
        self,
        prompt: str,
        output_dir: Path = OUTPUT_DIR,
    ) -> Path:
        import asyncio
        import logging

        from .image_client import recover_conversation_image
        from .playwright_session import ensure_logged_in, get_chatgpt_page

        logger = logging.getLogger(__name__)
        page = await get_chatgpt_page()
        await ensure_logged_in(page)
        try:
            return await self._recover_via_backend_api(page, prompt, output_dir)
        except Exception as api_exc:
            logger.info("backend conversation recover missed: %s", api_exc)

        # 不要新开对话，否则页面上的已出图会被清掉。
        if IMAGE_REUSE_CONVERSATION:
            await self._prepare_ui_conversation(page)
        await asyncio.sleep(1)
        return await recover_conversation_image(page, prompt, output_dir)

    async def _list_recent_conversation_ids(self, page, limit: int = 40) -> list[str]:
        ids = await page.evaluate(
            """
            async (limit) => {
              const urls = [
                '/backend-api/conversations?offset=0&limit=' + limit,
                '/backend-api/conversations?offset=0&limit=' + limit + '&order=updated',
              ];
              for (const url of urls) {
                const res = await fetch(url, {
                  credentials: 'include',
                  headers: { Accept: 'application/json' },
                });
                if (!res.ok) continue;
                const data = await res.json();
                const items = data.items || data.conversations || data.data || [];
                const found = items.map((item) => item.id || item.conversation_id).filter(Boolean);
                if (found.length) return found;
              }
              return [];
            }
            """,
            limit,
        )
        ordered: list[str] = []
        if self._ui_conversation_id:
            ordered.append(self._ui_conversation_id)
        for cid in ids or []:
            if isinstance(cid, str) and cid and cid not in ordered:
                ordered.append(cid)
        return ordered

    async def _conversation_image_turns_via_api(self, page, conversation_id: str) -> list[dict]:
        return await page.evaluate(
            """
            async (conversationId) => {
              const res = await fetch('/backend-api/conversation/' + conversationId, {
                credentials: 'include',
                headers: { Accept: 'application/json' },
              });
              if (!res.ok) return [];
              const data = await res.json();
              const mapping = data.mapping || {};
              const nodes = Object.values(mapping).filter((n) => n && n.message);
              nodes.sort((a, b) => (a.message.create_time || 0) - (b.message.create_time || 0));
              const extractText = (msg) => {
                const parts = (msg.content && msg.content.parts) || [];
                return parts.filter((p) => typeof p === 'string').join('\\n').trim();
              };
              const metaPrompt = (msg) => {
                const md = msg.metadata || {};
                const imageGen = md.image_gen || {};
                const dalle = md.dalle || {};
                return (
                  imageGen.prompt
                  || (imageGen.generation || {}).prompt
                  || dalle.prompt
                  || ''
                );
              };
              const extractImages = (msg) => {
                const urls = [];
                const fileIds = [];
                const dump = JSON.stringify(msg);
                const urlRe = /https:\\/\\/files\\.oaiusercontent\\.com\\/[^"\\\\\\s]+/g;
                let m;
                while ((m = urlRe.exec(dump))) {
                  urls.push(m[0].replace(/\\\\u0026/g, '&'));
                }
                const ptrRe = /(?:file-service|sediment):\\/\\/[A-Za-z0-9_-]+/g;
                while ((m = ptrRe.exec(dump))) {
                  fileIds.push(m[0].split('://')[1]);
                }
                return { urls: [...new Set(urls)], fileIds: [...new Set(fileIds)] };
              };
              const pairs = [];
              let lastUser = '';
              for (const node of nodes) {
                const msg = node.message;
                const role = ((msg.author || {}).role) || '';
                if (role === 'user') {
                  lastUser = extractText(msg);
                  continue;
                }
                if (role !== 'assistant') continue;
                const imgs = extractImages(msg);
                if (!imgs.urls.length && !imgs.fileIds.length) continue;
                const prompt = lastUser || extractText(msg) || metaPrompt(msg);
                if (!prompt) continue;
                pairs.push({
                  prompt,
                  genPrompt: metaPrompt(msg) || '',
                  src: imgs.urls[imgs.urls.length - 1] || '',
                  fileId: imgs.fileIds[imgs.fileIds.length - 1] || '',
                });
              }
              return pairs;
            }
            """,
            conversation_id,
        )

    async def _download_recovered_image(
        self,
        page,
        turn: dict,
        output_dir: Path,
    ) -> Path:
        from .image_client import _download_image

        src = str(turn.get("src") or "")
        file_id = str(turn.get("fileId") or "")
        if src.startswith(("http://", "https://", "data:")):
            saved = await _download_image(page, src, output_dir)
            if saved.exists() and saved.stat().st_size > 512:
                return saved
        if not file_id:
            raise RuntimeError("对话回合没有可下载的图片地址。")
        data = await page.evaluate(
            """
            async (fileId) => {
              const urls = [
                '/backend-api/files/' + fileId + '/download',
                '/backend-api/files/' + fileId,
              ];
              for (const url of urls) {
                const res = await fetch(url, { credentials: 'include', redirect: 'follow' });
                if (!res.ok) continue;
                const ctype = (res.headers.get('content-type') || '').toLowerCase();
                if (ctype.includes('application/json')) {
                  const js = await res.json();
                  const du = js.download_url || js.url || ((js.file || {}).download_url);
                  if (du) return { url: du };
                  continue;
                }
                const buf = await res.arrayBuffer();
                return { bytes: Array.from(new Uint8Array(buf)) };
              }
              throw new Error('file download failed: ' + fileId);
            }
            """,
            file_id,
        )
        if isinstance(data, dict) and data.get("url"):
            saved = await _download_image(page, str(data["url"]), output_dir)
            if saved.exists() and saved.stat().st_size > 512:
                return saved
        raw = (data or {}).get("bytes") if isinstance(data, dict) else None
        if not raw:
            raise RuntimeError(f"无法下载对话图片文件 {file_id}")
        output_dir.mkdir(parents=True, exist_ok=True)
        out = unique_image_path(output_dir, ".png")
        out.write_bytes(bytes(raw))
        return out

    async def _recover_via_backend_api(self, page, prompt: str, output_dir: Path) -> Path:
        import logging

        from .image_client import conversation_prompt_matches

        logger = logging.getLogger(__name__)
        conversation_ids = await self._list_recent_conversation_ids(page)
        if not conversation_ids:
            raise RuntimeError("侧边栏没有可扫描的 ChatGPT 对话。")
        scanned = 0
        last_count = 0
        for conversation_id in conversation_ids[:30]:
            try:
                turns = await self._conversation_image_turns_via_api(page, conversation_id)
            except Exception:
                continue
            scanned += 1
            last_count = len(turns or [])
            matched = [
                item
                for item in (turns or [])
                if conversation_prompt_matches(prompt, str(item.get("prompt") or ""))
                or conversation_prompt_matches(prompt, str(item.get("genPrompt") or ""))
            ]
            if not matched:
                continue
            logger.info(
                "recovered image from conversation %s (%s turns, prompt matched)",
                conversation_id,
                last_count,
            )
            return await self._download_recovered_image(page, matched[-1], output_dir)
        raise RuntimeError(
            f"扫了 {scanned} 个对话，最近对话里共看到约 {last_count} 张图，没有匹配到这张提示词。"
        )

    def remember_ui_conversation(self, conversation_id: str | None) -> None:
        if conversation_id:
            self._ui_conversation_id = conversation_id
            self._persist_ui_conversation_id(conversation_id)

    async def delete_conversation(self, conversation_id: str) -> bool:
        import asyncio
        import logging

        logger = logging.getLogger(__name__)
        page = await self._get_chat_page()
        deleted = await page.evaluate(
            """
            async (conversationId) => {
              const patch = await fetch('/backend-api/conversation/' + conversationId, {
                method: 'PATCH',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ is_visible: false }),
              });
              if (patch.ok) return { ok: true, method: 'patch', status: patch.status };
              const delRes = await fetch('/backend-api/conversation/' + conversationId, {
                method: 'DELETE',
                credentials: 'include',
              });
              return { ok: delRes.ok, method: 'delete', status: delRes.status };
            }
            """,
            conversation_id,
        )
        ok = bool(deleted and deleted.get("ok"))
        if ok:
            logger.info(
                "deleted conversation %s via %s (%s)",
                conversation_id,
                deleted.get("method"),
                deleted.get("status"),
            )
            if self._ui_conversation_id == conversation_id:
                self.reset_ui_conversation()
            if not IMAGE_REUSE_CONVERSATION:
                try:
                    await page.goto(f"{CHATGPT_ORIGIN}/", wait_until="domcontentloaded", timeout=60000)
                    await asyncio.sleep(1)
                except Exception:
                    pass
        else:
            logger.warning(
                "failed to delete conversation %s: %s",
                conversation_id,
                deleted,
            )
        return ok

    async def cleanup_conversations(self, limit: int = 100) -> int:
        page = await self._get_chat_page()
        conversation_ids = await page.evaluate(
            """
            async (limit) => {
              const urls = [
                '/backend-api/conversations?offset=0&limit=' + limit,
                '/backend-api/conversations?offset=0&limit=' + limit + '&order=updated',
              ];
              for (const url of urls) {
                const res = await fetch(url, {
                  credentials: 'include',
                  headers: { 'Accept': 'application/json' },
                });
                if (!res.ok) continue;
                const data = await res.json();
                const items = data.items || data.conversations || data.data || [];
                const ids = items.map((item) => item.id || item.conversation_id).filter(Boolean);
                if (ids.length) return ids;
              }
              return [];
            }
            """,
            limit,
        )
        deleted = 0
        for conversation_id in conversation_ids or []:
            if await self.delete_conversation(conversation_id):
                deleted += 1
        return deleted

    async def download_image(
        self,
        session: WebSession,
        image_url: str,
        output_dir: Path = OUTPUT_DIR,
    ) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(urlparse(image_url).path).suffix or ".png"
        out = unique_image_path(output_dir, suffix)

        try:
            page = await self._get_chat_page()
            data = await page.evaluate(
                """
                async (url) => {
                  const res = await fetch(url, { credentials: 'include' });
                  if (!res.ok) {
                    throw new Error('page download HTTP ' + res.status);
                  }
                  const buf = await res.arrayBuffer();
                  return Array.from(new Uint8Array(buf));
                }
                """,
                image_url,
            )
            out.write_bytes(bytes(data))
            return out
        except Exception:
            pass

        headers = self._base_headers(session)
        headers["Accept"] = "image/*,*/*"
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            response = await client.get(image_url, headers=headers)
            response.raise_for_status()
            suffix = Path(urlparse(str(response.url)).path).suffix or ".png"
            out = unique_image_path(output_dir, suffix)
            out.write_bytes(response.content)
            return out

    async def chat(self, messages: list[dict[str, str]], model: str = "auto") -> tuple[str, str | None]:
        user_messages = [m["content"] for m in messages if m.get("role") == "user"]
        if not user_messages:
            raise RuntimeError("缺少 user 消息")
        result = await self.converse(user_messages[-1], model=model)
        return result.text, result.conversation_id

    async def _generate_image_via_ui(
        self,
        prompt: str,
        *,
        output_dir: Path = OUTPUT_DIR,
        reference_images: list[Any] | None = None,
    ) -> tuple[Path, str | None]:
        import asyncio

        from .image_client import (
            _assistant_message_count,
            _auto_handle_image_ui,
            _collect_assistant_images,
            _conversation_turn_max,
            _dismiss_popups,
            _maybe_enable_image_tool,
            _save_last_assistant_image,
            _submit_prompt_on_page,
            _wait_until_not_generating,
        )
        from .image_utils import ReferenceImage

        refs = [r for r in (reference_images or []) if isinstance(r, ReferenceImage)]
        page = await self._get_chat_page()
        await asyncio.sleep(1)
        await _dismiss_popups(page)
        await self._prepare_ui_conversation(page)
        await _maybe_enable_image_tool(page)
        before = set(await _collect_assistant_images(page))
        assistant_turn_before = await _assistant_message_count(page)
        conversation_turn_before = await _conversation_turn_max(page)

        temp_paths: list[Path] = []
        if refs:
            plus = page.locator("button[data-testid='composer-plus-btn'], button[aria-label*='Add']")
            if await plus.count():
                await plus.first.click(timeout=3000)
                await asyncio.sleep(0.5)
            file_input = page.locator("input[type='file']")
            if not await file_input.count():
                raise RuntimeError("页面未找到文件上传控件，垫图失败。请确认 ChatGPT 支持附件上传。")

            upload_paths: list[str] = []
            for index, ref in enumerate(refs):
                if ref.source_path and Path(ref.source_path).exists():
                    upload_paths.append(str(Path(ref.source_path)))
                else:
                    suffix = Path(ref.filename).suffix or ".png"
                    temp_path = output_dir / f"_ref_{int(__import__('time').time())}_{index}{suffix}"
                    temp_path.write_bytes(ref.data)
                    temp_paths.append(temp_path)
                    upload_paths.append(str(temp_path))

            await file_input.first.set_input_files(upload_paths)
            await asyncio.sleep(2)

        await _submit_prompt_on_page(page, prompt, new_chat=False)
        for temp_path in temp_paths:
            temp_path.unlink(missing_ok=True)

        conversation_id = self._ui_conversation_id or await self._extract_conversation_id(page)
        if not conversation_id:
            conversation_id = await self._wait_for_conversation_id(page, timeout=30.0)

        # 上传参考图后、等待生成前也尝试消掉选项弹层
        for _ in range(5):
            if await _auto_handle_image_ui(page):
                await asyncio.sleep(1.0)
            else:
                break

        from .image_client import _force_save_page_image

        try:
            image_path = await _save_last_assistant_image(
                page,
                before,
                output_dir,
                timeout=360.0,
                assistant_turn_before=assistant_turn_before,
                conversation_turn_before=conversation_turn_before,
            )
        except RuntimeError:
            image_path = await _force_save_page_image(
                page,
                output_dir,
                conversation_turn_before=conversation_turn_before,
            )
        if not conversation_id:
            conversation_id = await self._extract_conversation_id(page)
        if conversation_id:
            self.remember_ui_conversation(conversation_id)
        return image_path, conversation_id

    async def generate_image(
        self,
        prompt: str,
        *,
        keep_conversation: bool = False,
        new_conversation: bool = False,
        output_dir: Path = OUTPUT_DIR,
        reference_image: Any | None = None,
        reference_images: list[Any] | None = None,
    ) -> Path:
        from .image_utils import ReferenceImage

        refs: list[ReferenceImage] = [
            r for r in (reference_images or []) if isinstance(r, ReferenceImage)
        ]
        if isinstance(reference_image, ReferenceImage):
            refs = [reference_image, *refs]

        if refs:
            normalized = prompt or "请基于参考图生成一张新图片"
        else:
            normalized = (
                prompt
                if any(k in prompt.lower() for k in ("image", "picture", "画", "图", "场景", "镜头"))
                else f"请生成一张图片：{prompt}"
            )

        conversation_id: str | None = None
        image_path: Path

        if new_conversation:
            self.reset_ui_conversation()

        try:
            if IMAGE_MODE == "ui":
                image_path, conversation_id = await self._generate_image_via_ui(
                    normalized,
                    output_dir=output_dir,
                    reference_images=refs,
                )
            elif IMAGE_MODE == "protocol":
                result = await self.converse(
                    normalized,
                    model="auto",
                    reference_images=refs,
                )
                conversation_id = result.conversation_id
                if not result.image_urls:
                    raise RuntimeError(
                        "协议模式未从响应中解析到图片 URL。"
                        f"conversation_id={result.conversation_id} text={result.text[:200]}"
                    )
                session = await self.session()
                image_path = await self.download_image(session, result.image_urls[-1], output_dir)
            else:
                try:
                    result = await self.converse(
                        normalized,
                        model="auto",
                        reference_images=refs,
                    )
                    conversation_id = result.conversation_id
                    if not result.image_urls:
                        raise RuntimeError(
                            "协议模式未从响应中解析到图片 URL。"
                            f"conversation_id={result.conversation_id} text={result.text[:200]}"
                        )

                    session = await self.session()
                    image_path = await self.download_image(session, result.image_urls[-1], output_dir)
                except Exception as protocol_exc:
                    try:
                        image_path, conversation_id = await self._generate_image_via_ui(
                            normalized,
                            output_dir=output_dir,
                            reference_images=refs,
                        )
                    except Exception as ui_exc:
                        raise RuntimeError(
                            f"协议出图失败({protocol_exc}); UI 出图也失败({ui_exc})"
                        ) from ui_exc
        finally:
            should_delete = (
                IMAGE_DELETE_CONVERSATION
                and not keep_conversation
                and not IMAGE_REUSE_CONVERSATION
            )
            if should_delete:
                if not conversation_id:
                    try:
                        page = await self._get_chat_page()
                        conversation_id = await self._extract_conversation_id(page)
                    except Exception:
                        conversation_id = None
                if conversation_id:
                    await self.delete_conversation(conversation_id)

        return image_path


protocol_client = ProtocolClient()


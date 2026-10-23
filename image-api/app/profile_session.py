from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from .config import CHATGPT_ORIGIN, DEFAULT_USER_AGENT, OUTPUT_DIR
from .playwright_session import PROFILE_DIR, _ensure_context

SESSION_CACHE = OUTPUT_DIR.parent / ".session-cache.json"


@dataclass
class WebSession:
    access_token: str
    cookie_header: str
    email: str | None
    expires_at: float
    device_id: str | None = None
    account_id: str | None = None

    @property
    def authorization(self) -> str:
        return f"Bearer {self.access_token}"


def _parse_cookie_header(cookie_header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in cookie_header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        out[name.strip()] = value.strip()
    return out


def _load_cache() -> WebSession | None:
    if not SESSION_CACHE.exists():
        return None
    try:
        data = json.loads(SESSION_CACHE.read_text(encoding="utf-8"))
        return WebSession(**data)
    except Exception:
        return None


def _save_cache(session: WebSession) -> None:
    SESSION_CACHE.write_text(
        json.dumps(asdict(session), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def refresh_session_from_profile(force: bool = False) -> WebSession:
    cached = None if force else _load_cache()
    now = time.time()
    if cached and cached.expires_at > now + 60:
        return cached

    try:
        context = await _ensure_context()
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(f"{CHATGPT_ORIGIN}/", wait_until="domcontentloaded", timeout=90000)

        payload = await page.evaluate(
            """
            async () => {
              const res = await fetch('/api/auth/session', { credentials: 'include' });
              const data = await res.json();
              return {
                accessToken: data.accessToken || null,
                expires: data.expires || null,
                email: data.user?.email || null,
              };
            }
            """
        )

        cookies = await context.cookies()
        cookie_parts = [f"{c['name']}={c['value']}" for c in cookies if "chatgpt.com" in c.get("domain", "")]
        cookie_header = "; ".join(cookie_parts)

        access_token = payload.get("accessToken")
        if not access_token:
            raise RuntimeError(
                "Playwright 配置未登录 ChatGPT。请在服务器 Chrome 窗口登录一次。"
                f"配置目录: {PROFILE_DIR}"
            )

        expires = payload.get("expires")
        expires_at = float(expires) if isinstance(expires, (int, float)) else now + 3600
        cookie_map = _parse_cookie_header(cookie_header)
        session = WebSession(
            access_token=access_token,
            cookie_header=cookie_header,
            email=payload.get("email"),
            expires_at=expires_at,
            device_id=cookie_map.get("oai-did"),
            account_id=cookie_map.get("_account"),
        )
        _save_cache(session)
        return session
    except Exception as exc:
        if cached:
            return cached
        raise RuntimeError(
            "无法刷新 ChatGPT 会话。请确认 API 服务正在运行且 Playwright Chrome 已登录。"
            f" 原始错误: {exc}"
        ) from exc


from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from .config import DEFAULT_USER_AGENT, SESSION_URL
from .cookie_extractor import cookie_header, extract_cookie_jar


@dataclass
class ChatGPTSession:
    access_token: str
    email: str | None
    cookie_header: str
    browser: str
    expires_at: float

    @property
    def authorization(self) -> str:
        return f"Bearer {self.access_token}"


class SessionManager:
    def __init__(self) -> None:
        self._session: ChatGPTSession | None = None

    async def get(self, force_refresh: bool = False) -> ChatGPTSession:
        now = time.time()
        if (
            not force_refresh
            and self._session
            and self._session.expires_at > now + 30
        ):
            return self._session

        jar, browser = extract_cookie_jar()
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json",
            "Referer": "https://chatgpt.com/",
            "Cookie": cookie_header(jar),
        }

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(SESSION_URL, headers=headers)
            response.raise_for_status()
            data = response.json()

        access_token = data.get("accessToken")
        if not access_token:
            raise RuntimeError(
                "浏览器 Cookie 已读取，但 session 无效。"
                "请重新在浏览器登录 chatgpt.com 后再试。"
            )

        expires = data.get("expires")
        expires_at = float(expires) if isinstance(expires, (int, float)) else now + 3600

        self._session = ChatGPTSession(
            access_token=access_token,
            email=(data.get("user") or {}).get("email"),
            cookie_header=headers["Cookie"],
            browser=browser,
            expires_at=expires_at,
        )
        return self._session


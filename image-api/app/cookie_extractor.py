from __future__ import annotations

import http.cookiejar
from typing import Callable, Iterable

import browser_cookie3

from .config import BROWSER

CHATGPT_DOMAINS = ("chatgpt.com", ".chatgpt.com", "chat.openai.com", ".openai.com")


def _iter_browser_loaders() -> Iterable[tuple[str, Callable[..., http.cookiejar.CookieJar]]]:
    order: list[tuple[str, Callable[..., http.cookiejar.CookieJar]]] = [
        ("chrome", browser_cookie3.chrome),
        ("edge", browser_cookie3.edge),
    ]
    if BROWSER == "edge":
        order = [item for item in order if item[0] == "edge"]
    elif BROWSER == "chrome":
        order = [item for item in order if item[0] == "chrome"]
    for item in order:
        yield item


def _load_with_rookiepy() -> tuple[http.cookiejar.CookieJar, str]:
    import rookiepy

    loaders = []
    if BROWSER in {"chrome", "auto"}:
        loaders.append(("chrome", rookiepy.chrome))
    if BROWSER in {"edge", "auto"}:
        loaders.append(("edge", rookiepy.edge))

    last_error: Exception | None = None
    for name, loader in loaders:
        try:
            cookies = loader(["chatgpt.com", "openai.com"])
            jar = http.cookiejar.CookieJar()
            for cookie in cookies:
                jar.set_cookie(cookie)
            if list(jar):
                return jar, f"{name}-rookiepy"
        except Exception as exc:  # noqa: BLE001
            last_error = exc

    if last_error:
        raise RuntimeError(f"rookiepy 读取失败: {last_error}") from last_error
    raise RuntimeError("rookiepy 未读到 Cookie")


def extract_cookie_jar() -> tuple[http.cookiejar.CookieJar, str]:
    """从本机已登录浏览器读取 ChatGPT 相关 Cookie。"""
    last_error: Exception | None = None

    try:
        return _load_with_rookiepy()
    except Exception as exc:  # noqa: BLE001
        last_error = exc

    for name, loader in _iter_browser_loaders():
        try:
            jar = loader(domain_name="chatgpt.com")
            cookies = list(jar)
            if not cookies:
                continue
            return jar, name
        except Exception as exc:  # noqa: BLE001
            last_error = exc

    hint = (
        "未能从 Chrome 读取 Cookie。请确认：\n"
        "1) 已在 Chrome 登录 chatgpt.com\n"
        "2) 完全关闭 Chrome 后再试\n"
        "3) 若仍失败，新版 Chrome 可能阻止 Cookie 导出，请改用 Playwright 持久化登录"
    )
    if last_error:
        raise RuntimeError(f"{hint}\n原始错误: {last_error}") from last_error
    raise RuntimeError(hint)


def cookie_header(jar: http.cookiejar.CookieJar) -> str:
    parts: list[str] = []
    for cookie in jar:
        if any(domain in (cookie.domain or "") for domain in CHATGPT_DOMAINS):
            parts.append(f"{cookie.name}={cookie.value}")
    return "; ".join(parts)


from __future__ import annotations

import asyncio
import time
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from .browser_helper import cdp_reachable
from .config import BROWSER_ATTACH, CDP_URL, CHATGPT_ORIGIN, OUTPUT_DIR, PLAYWRIGHT_CDP_URL

PROFILE_DIR = OUTPUT_DIR.parent / ".chrome-profile"
_lock = asyncio.Lock()
_playwright: Playwright | None = None
_browser: Browser | None = None
_context: BrowserContext | None = None
_cdp_attached: bool = False


def _profile_locked() -> bool:
    return any(
        path.exists()
        for path in (
            PROFILE_DIR / "SingletonLock",
            PROFILE_DIR / "SingletonCookie",
            PROFILE_DIR / "SingletonSocket",
            PROFILE_DIR / "Default" / "LOCK",
        )
    )


def _clear_profile_locks() -> None:
    if _profile_locked():
        return
    candidates = [
        PROFILE_DIR / name
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile")
    ]
    candidates.append(PROFILE_DIR / "Default" / "LOCK")
    for path in candidates:
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass


def _kill_stale_profile_browser() -> None:
    """仅结束占用本项目 profile 的 Chrome，便于带 CDP 端口重启。"""
    import subprocess

    marker = ".chrome-profile"
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{marker}*' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


async def _reset_playwright_handles() -> None:
    global _playwright, _browser, _context, _cdp_attached
    if _browser is not None:
        try:
            await _browser.close()
        except Exception:
            pass
    elif _context is not None and not _cdp_attached:
        try:
            await _context.close()
        except Exception:
            pass
    _browser = None
    _context = None
    _cdp_attached = False
    if _playwright is not None:
        try:
            await _playwright.stop()
        except Exception:
            pass
        _playwright = None


async def _close_context() -> None:
    await _reset_playwright_handles()


def _context_alive(context: BrowserContext) -> bool:
    try:
        _ = context.pages
        return True
    except Exception:
        return False


async def _connect_over_cdp(cdp_url: str) -> BrowserContext | None:
    global _playwright, _browser, _context, _cdp_attached
    if not cdp_reachable(cdp_url, timeout=1.5):
        return None
    try:
        if _playwright is None:
            _playwright = await async_playwright().start()
        browser = await _playwright.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        _browser = browser
        _context = context
        _cdp_attached = True
        return context
    except Exception:
        return None


async def _try_attach_existing_browser() -> BrowserContext | None:
    if not BROWSER_ATTACH:
        return None

    for cdp_url in (CDP_URL, PLAYWRIGHT_CDP_URL):
        context = await _connect_over_cdp(cdp_url)
        if context is not None:
            return context

    if _profile_locked():
        for _ in range(30):
            context = await _connect_over_cdp(PLAYWRIGHT_CDP_URL)
            if context is not None:
                return context
            await asyncio.sleep(0.5)
    return None


async def _launch_context() -> BrowserContext:
    global _playwright, _context, _cdp_attached

    attached = await _try_attach_existing_browser()
    if attached is not None:
        return attached

    if _profile_locked():
        _kill_stale_profile_browser()
        await asyncio.sleep(2)
        attached = await _try_attach_existing_browser()
        if attached is not None:
            return attached

    await _reset_playwright_handles()
    _clear_profile_locks()

    port = PLAYWRIGHT_CDP_URL.rsplit(":", 1)[-1]
    _playwright = await async_playwright().start()
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _context = await _playwright.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        channel="chrome",
        headless=False,
        viewport={"width": 1360, "height": 900},
        args=[
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=*",
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    )
    _cdp_attached = False
    return _context


async def _ensure_context() -> BrowserContext:
    global _context
    async with _lock:
        if _context is not None and _context_alive(_context):
            return _context

        await _reset_playwright_handles()
        return await _launch_context()


async def get_browser_context() -> BrowserContext:
    return await _ensure_context()


async def get_chatgpt_page(*, navigate_home: bool = False) -> Page:
    context = await _ensure_context()
    for page in context.pages:
        if "chatgpt.com/c/" in page.url and "auth" not in page.url:
            return page
    for page in context.pages:
        if "chatgpt.com" in page.url and "auth" not in page.url:
            if navigate_home:
                await page.goto(f"{CHATGPT_ORIGIN}/", wait_until="domcontentloaded", timeout=90000)
                await asyncio.sleep(2)
            return page
    page = context.pages[0] if context.pages else await context.new_page()
    if "chatgpt.com" not in page.url or "auth" in page.url:
        await page.goto(f"{CHATGPT_ORIGIN}/", wait_until="domcontentloaded", timeout=90000)
        await asyncio.sleep(2)
    return page


async def ensure_logged_in(page: Page, *, timeout: float = 45.0) -> None:
    deadline = time.time() + timeout
    last_state: dict | None = None

    while time.time() < deadline:
        last_state = await page.evaluate(
            """
            () => {
              const buttons = Array.from(document.querySelectorAll('button'));
              const hasLoginBtn = buttons.some((btn) => /log in|登录/i.test(btn.innerText || ''));
              const composer = document.querySelector(
                '#prompt-textarea, [data-testid="prompt-textarea"], textarea[name="prompt-textarea"], div.ProseMirror[contenteditable="true"], form textarea, [contenteditable="true"][data-placeholder]'
              );
              const sendBtn = document.querySelector(
                'button[data-testid="send-button"], button[aria-label*="Send"], button[aria-label*="发送"]'
              );
              const newChat = document.querySelector(
                '[data-testid="create-new-chat-button"], button[aria-label*="New chat"], button[aria-label*="新聊天"]'
              );
              const profile = document.querySelector(
                '[data-testid="profile-button"], img[alt*="User"], [aria-label*="Profile"], [aria-label*="个人"]'
              );
              const path = location.pathname || '';
              const onAuth = /auth|login|log-in|signin|sign-in/i.test(path + location.href);
              return {
                url: location.href,
                onAuth,
                hasComposer: !!composer,
                hasSend: !!sendBtn,
                hasNewChat: !!newChat,
                hasProfile: !!profile,
                hasLogin: !!document.querySelector('[data-testid="login-button"]') || hasLoginBtn,
              };
            }
            """
        )
        if last_state.get("onAuth") or last_state.get("hasLogin"):
            await asyncio.sleep(2)
            try:
                await page.goto(f"{CHATGPT_ORIGIN}/", wait_until="domcontentloaded", timeout=90000)
            except Exception:
                pass
            await asyncio.sleep(2)
            continue

        if (
            last_state.get("hasComposer")
            or last_state.get("hasSend")
            or (last_state.get("hasNewChat") and last_state.get("hasProfile"))
        ):
            return

        await asyncio.sleep(1.5)

    raise RuntimeError(
        "Playwright Chrome 尚未登录 ChatGPT。"
        f"请在服务器弹出的 Chrome 窗口完成登录（只需一次）。"
        f"配置目录: {PROFILE_DIR} | 当前页面: {(last_state or {}).get('url')}"
    )


async def read_last_assistant_text(page: Page, timeout: float = 120.0) -> str:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        generating = await page.evaluate(
            """
            () => !!document.querySelector('[data-testid="stop-button"], button[aria-label*="Stop"]')
            """
        )
        text = await page.evaluate(
            """
            () => {
              const nodes = document.querySelectorAll('[data-message-author-role="assistant"]');
              if (!nodes.length) return '';
              return nodes[nodes.length - 1].innerText || '';
            }
            """
        )
        if text and text != last:
            last = text
        if last and not generating:
            await asyncio.sleep(1)
            return last.strip()
        await asyncio.sleep(1.2)
    raise TimeoutError("等待 ChatGPT 回复超时。")


async def browser_session_info() -> dict[str, object]:
    return {
        "attach_enabled": BROWSER_ATTACH,
        "cdp_urls": [CDP_URL, PLAYWRIGHT_CDP_URL],
        "cdp_reachable": {
            CDP_URL: cdp_reachable(CDP_URL),
            PLAYWRIGHT_CDP_URL: cdp_reachable(PLAYWRIGHT_CDP_URL),
        },
        "profile_locked": _profile_locked(),
        "connected": _context is not None and _context_alive(_context) if _context else False,
        "attached_via_cdp": _cdp_attached,
        "profile_dir": str(PROFILE_DIR),
    }


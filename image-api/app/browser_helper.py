from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import httpx

from .config import BROWSER, CDP_URL


def _default_profile_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", ""))
    if BROWSER == "edge":
        return base / "chatgpt-web-api-edge"
    return base / "chatgpt-web-api-chrome"


BROWSER_PROFILE_DIR = Path(os.getenv("CHATGPT_BROWSER_PROFILE", _default_profile_dir()))


def _chrome_paths() -> list[Path]:
    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ]
    return [path for path in candidates if path.exists()]


def _edge_paths() -> list[Path]:
    candidates = [
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ]
    return [path for path in candidates if path.exists()]


def _browser_paths() -> list[Path]:
    if BROWSER == "edge":
        return _edge_paths()
    if BROWSER == "chrome":
        return _chrome_paths()
    return _chrome_paths() + _edge_paths()


def chrome_user_data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"


def cdp_reachable(cdp_url: str = CDP_URL, timeout: float = 2.0) -> bool:
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(f"{cdp_url.rstrip('/')}/json/version")
            return response.status_code == 200
    except Exception:
        return False


def launch_debug_browser(cdp_port: int = 9222, profile_dir: Path | None = None) -> Path:
    """启动 Chrome/Edge 调试窗口。默认使用独立 profile，避免和日常浏览器抢锁。"""
    browser_exe = next(iter(_browser_paths()), None)
    if browser_exe is None:
        raise RuntimeError("未找到 Chrome 或 Edge，请手动安装浏览器。")

    profile = profile_dir or BROWSER_PROFILE_DIR
    profile.mkdir(parents=True, exist_ok=True)
    args = [
        str(browser_exe),
        f"--remote-debugging-port={cdp_port}",
        "--remote-debugging-address=127.0.0.1",
        "--remote-allow-origins=*",
        f'--user-data-dir={profile}',
        "--no-first-run",
        "--no-default-browser-check",
        "https://chatgpt.com/",
    ]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return browser_exe


def ensure_cdp_ready(
    cdp_url: str = CDP_URL,
    wait_seconds: float = 20.0,
    auto_launch: bool = True,
) -> None:
    if cdp_reachable(cdp_url):
        return

    if not auto_launch:
        raise RuntimeError(_cdp_help_message(cdp_url))

    launch_debug_browser()
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if cdp_reachable(cdp_url):
            return
        time.sleep(0.5)

    raise RuntimeError(_cdp_help_message(cdp_url))


def _cdp_help_message(cdp_url: str) -> str:
    port = cdp_url.rsplit(":", 1)[-1]
    profile = BROWSER_PROFILE_DIR
    browser_name = "Chrome" if BROWSER != "edge" else "Edge"
    return (
        f"无法连接 CDP ({cdp_url})。图片生成需要 {browser_name} 调试窗口。\n"
        f"请先运行: .\\start-browser.ps1\n"
        f"若 Cookie 在你日常 Chrome 里，可试: .\\start-browser-existing.ps1\n"
        f"（需先完全退出 Chrome）\n"
        f"独立 profile 目录: {profile}\n"
        f"CDP 端口: {port}"
    )


def cdp_diagnostics(cdp_url: str = CDP_URL) -> dict[str, object]:
    info: dict[str, object] = {
        "cdp_url": cdp_url,
        "reachable": False,
        "browser": None,
        "configured_browser": BROWSER,
        "pages": [],
        "profile_dir": str(BROWSER_PROFILE_DIR),
        "chrome_user_data_dir": str(chrome_user_data_dir()),
    }
    try:
        with httpx.Client(timeout=3.0) as client:
            version = client.get(f"{cdp_url.rstrip('/')}/json/version").json()
            info["reachable"] = True
            info["browser"] = version.get("Browser")
            targets = client.get(f"{cdp_url.rstrip('/')}/json/list").json()
            info["pages"] = [
                {"title": t.get("title"), "url": t.get("url"), "type": t.get("type")}
                for t in targets
                if t.get("type") == "page"
            ]
    except Exception as exc:  # noqa: BLE001
        info["error"] = str(exc)
    return info


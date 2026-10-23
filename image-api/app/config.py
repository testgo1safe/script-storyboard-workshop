from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

HOST = os.getenv("CHATGPT_API_HOST", "0.0.0.0")
PORT = int(os.getenv("CHATGPT_API_PORT", "8765"))
API_KEY = os.getenv("CHATGPT_API_KEY", "local-dev-key")
# 局域网图片 URL 前缀；留空则按请求的 Host 自动推断
PUBLIC_BASE_URL = os.getenv("CHATGPT_PUBLIC_BASE_URL", "").rstrip("/")

# chrome | edge | auto  — 默认 chrome，因为常见登录在 Chrome
BROWSER = os.getenv("CHATGPT_BROWSER", "chrome").lower()

# 图片生成：连接已开启远程调试端口的 Chrome/Edge
CDP_URL = os.getenv("CHATGPT_CDP_URL", "http://127.0.0.1:9222")
# Playwright 独立 profile 的 CDP 端口（API 重启后复连同一 Chrome）
PLAYWRIGHT_CDP_PORT = int(os.getenv("CHATGPT_PLAYWRIGHT_CDP_PORT", "9333"))
PLAYWRIGHT_CDP_URL = os.getenv(
    "CHATGPT_PLAYWRIGHT_CDP_URL",
    f"http://127.0.0.1:{PLAYWRIGHT_CDP_PORT}",
)
# 默认优先连接已打开的浏览器，而不是每次新开
BROWSER_ATTACH = os.getenv("CHATGPT_BROWSER_ATTACH", "true").lower() in {
    "1",
    "true",
    "yes",
}

CHATGPT_ORIGIN = "https://chatgpt.com"
SESSION_URL = f"{CHATGPT_ORIGIN}/api/auth/session"
CONVERSATION_URL = f"{CHATGPT_ORIGIN}/backend-api/conversation"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/134.0.0.0 Safari/537.36"
)

# 同一会话连续出图，避免频繁新开会话触发 OpenAI 限流
IMAGE_REUSE_CONVERSATION = os.getenv("CHATGPT_IMAGE_REUSE", "true").lower() in {
    "1",
    "true",
    "yes",
}
# 出图后删除会话；复用会话模式下默认 false
IMAGE_DELETE_CONVERSATION = os.getenv("CHATGPT_IMAGE_DELETE", "false").lower() in {
    "1",
    "true",
    "yes",
}
# 网页端同时出图能力有限，建议 1；可设 2 开多标签并行（可能触发限流）
IMAGE_MAX_CONCURRENT = max(1, int(os.getenv("CHATGPT_IMAGE_MAX_CONCURRENT", "1")))
# ui: 浏览器 UI 出图（稳定） | auto: 先协议后 UI | protocol: 仅协议（易被风控）
IMAGE_MODE = os.getenv("CHATGPT_IMAGE_MODE", "ui").lower()


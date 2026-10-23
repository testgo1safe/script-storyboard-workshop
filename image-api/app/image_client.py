from __future__ import annotations

import asyncio
import base64
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from .browser_helper import cdp_reachable, ensure_cdp_ready
from .config import CDP_URL, CHATGPT_ORIGIN, OUTPUT_DIR
from .cookie_extractor import extract_cookie_jar
from .image_utils import unique_image_path

COMPOSER_SELECTORS = (
    "div.ProseMirror[contenteditable='true']",
    "#prompt-textarea",
    "textarea[name='prompt-textarea']",
    "[data-testid='prompt-textarea']",
    "textarea:not([disabled])",
)

NEW_CHAT_SELECTORS = (
    "[data-testid='create-new-chat-button']",
    "a[href='/']",
    "button:has-text('New chat')",
    "button:has-text('新聊天')",
)

IMAGE_TOOL_SELECTORS = (
    "button:has-text('Create image')",
    "button:has-text('创建图像')",
    "button:has-text('生成图像')",
    "button:has-text('图像')",
    "[data-testid='composer-action-create-image']",
    "[data-testid='image-gen-button']",
    "[aria-label*='Create image' i]",
    "[aria-label*='创建图像']",
    "[aria-label*='image' i]",
    "[aria-label*='图像']",
)


def _normalize_image_prompt(prompt: str) -> str:
    lowered = prompt.lower()
    keywords = ("image", "picture", "photo", "draw", "generate", "画", "图", "绘制")
    if any(word in lowered for word in keywords):
        return prompt
    return f"请生成一张图片：{prompt}"


async def _find_chatgpt_page(context: BrowserContext) -> Page:
    for page in context.pages:
        if "chatgpt.com" in page.url and "auth" not in page.url:
            return page

    page = await context.new_page()
    await page.goto(f"{CHATGPT_ORIGIN}/", wait_until="domcontentloaded", timeout=60000)
    return page


async def _dismiss_popups(page: Page) -> None:
    for selector in (
        "button:has-text('OK')",
        "button:has-text('Got it')",
        "button:has-text('知道了')",
        "button:has-text('Continue')",
    ):
        locator = page.locator(selector)
        if await locator.count():
            try:
                await locator.first.click(timeout=1500)
            except Exception:
                pass


async def _start_new_chat(page: Page) -> None:
    for selector in NEW_CHAT_SELECTORS:
        locator = page.locator(selector)
        if await locator.count():
            try:
                await locator.first.click(timeout=3000)
                await asyncio.sleep(1)
                return
            except Exception:
                continue

    await page.goto(f"{CHATGPT_ORIGIN}/", wait_until="domcontentloaded", timeout=60000)


async def _maybe_enable_image_tool(page: Page) -> None:
    for selector in ("button[data-testid='composer-plus-btn']", "button[aria-label*='Add']", "button[aria-label*='添加']"):
        plus = page.locator(selector)
        if await plus.count():
            try:
                await plus.first.click(timeout=2000)
                await asyncio.sleep(0.6)
                break
            except Exception:
                pass

    for selector in IMAGE_TOOL_SELECTORS:
        locator = page.locator(selector)
        if await locator.count():
            try:
                await locator.first.click(timeout=2000)
                await asyncio.sleep(0.5)
                return
            except Exception:
                continue


IMAGE_CHOICE_BUTTON_SELECTORS = (
    "button:has-text('Use this')",
    "button:has-text('Use image')",
    "button:has-text('Use this image')",
    "button:has-text('Select')",
    "button:has-text('Confirm')",
    "button:has-text('Continue')",
    "button:has-text('使用')",
    "button:has-text('选择')",
    "button:has-text('确认')",
    "button:has-text('继续')",
    "button:has-text('就这张')",
    "button:has-text('Generate')",
    "button:has-text('Create image')",
    "button:has-text('创建图像')",
    "button:has-text('生成图像')",
    "[data-testid='image-gen-button']",
    "[data-testid='composer-action-create-image']",
)

IMAGE_VARIANT_SELECTORS = (
    "[data-testid='image-gen-image']",
    "[data-testid='generated-image']",
    "[data-message-author-role='assistant']:last-child [data-testid*='image-gen']",
    "[data-message-author-role='assistant']:last-child button:has(img)",
    "[data-message-author-role='assistant']:last-child [role='button']:has(img)",
)

_BLOCKED_UI_TEXT = re.compile(
    r"pin|unpin|置顶|取消置顶|archive|归档|share|分享|rename|重命名|report|举报|delete chat|删除聊天|删除",
    re.I,
)


async def _click_main_locators(page: Page) -> bool:
    """Playwright 在主聊天区点击已知出图/选图按钮。"""
    main = page.locator("main")
    if not await main.count():
        main = page.locator('[data-testid="conversation-turns"]')
    if not await main.count():
        return False

    for selector in IMAGE_CHOICE_BUTTON_SELECTORS:
        loc = main.locator(selector)
        count = await loc.count()
        for i in range(min(count, 4)):
            btn = loc.nth(i)
            try:
                label = (
                    (await btn.inner_text(timeout=800))
                    + " "
                    + (await btn.get_attribute("aria-label") or "")
                ).strip()
                if label and _BLOCKED_UI_TEXT.search(label):
                    continue
                if not await btn.is_visible():
                    continue
                await btn.click(timeout=2000)
                await asyncio.sleep(1.2)
                return True
            except Exception:
                continue

    for selector in IMAGE_VARIANT_SELECTORS:
        loc = main.locator(selector)
        if not await loc.count():
            continue
        try:
            target = loc.first
            if not await target.is_visible():
                continue
            await target.click(timeout=2000)
            await asyncio.sleep(1.2)
            return True
        except Exception:
            continue
    return False


async def _auto_handle_image_ui(page: Page) -> bool:
    """在主聊天区自动确认出图选项 / 多图选一（不碰侧边栏）。"""
    if await _click_main_locators(page):
        return True

    clicked = await page.evaluate(
        """
        () => {
          const inSidebar = (el) => !!el?.closest?.(
            'nav, aside, [data-testid*="sidebar"], [class*="sidebar"], [aria-label*="Chat history"], [aria-label*="聊天历史"]'
          );
          const root = document.querySelector('[data-testid="conversation-turns"]')
            || document.querySelector('main')
            || document.querySelector('[role="dialog"]')
            || null;
          if (!root || inSidebar(root)) return '';

          const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const blocked = /pin|unpin|置顶|取消置顶|archive|归档|share|分享|rename|重命名|report|举报|delete chat|删除聊天|删除/i;
          const isVisible = (el) => {
            if (!el || inSidebar(el)) return false;
            const r = el.getBoundingClientRect();
            return r.width > 8 && r.height > 8;
          };
          const click = (el) => {
            if (!isVisible(el)) return false;
            el.click();
            return true;
          };
          const isGenImg = (src) => /oaiusercontent|files\\.oaiusercontent|blob:|\\/backend-api\\/files\\//i.test(src || '');
          const isBadImg = (src) => /avatar|logo|favicon|profile|emoji|icon/i.test(src || '');

          const btnPatterns = [
            /use this( image)?/i,
            /select( this)?( one)?/i,
            /choose( this)?( one)?/i,
            /^confirm$/i,
            /^continue$/i,
            /^ok$/i,
            /create image/i,
            /generate( image)?/i,
            /make (an )?image/i,
            /looks good/i,
            /this one/i,
            /使用(这张|此)?/,
            /选择(这张|此)?/,
            /^确认$/,
            /^继续$/,
            /就这张/,
            /创建图像/,
            /生成图像/,
            /生成图片/,
            /开始生成/,
            /是的/,
            /^好$/,
            /^行$/,
          ];

          for (const btn of root.querySelectorAll('button, [role="button"]')) {
            if (inSidebar(btn)) continue;
            const text = norm(btn.innerText || btn.getAttribute('aria-label') || '');
            if (!text || text.length > 64 || blocked.test(text)) continue;
            if (btnPatterns.some((re) => re.test(text))) {
              if (click(btn)) return 'button:' + text.slice(0, 40);
            }
          }

          for (const item of root.querySelectorAll('[role="menuitem"], [role="option"], [data-testid*="composer-action"]')) {
            if (inSidebar(item)) continue;
            const text = norm(item.innerText || item.getAttribute('aria-label') || '');
            if (blocked.test(text)) continue;
            if (/create image|生成图像|创建图像|image/i.test(text)) {
              if (click(item)) return 'menu:' + text.slice(0, 40);
            }
          }

          const msgs = root.querySelectorAll('[data-message-author-role="assistant"]');
          const last = msgs[msgs.length - 1];
          if (!last || inSidebar(last)) return '';

          const lastText = norm(last.innerText || '');
          const hintPick = /which|choose|select|prefer|pick|option|variant|version|style|哪|选|更喜欢|确认|版本|风格|张图|几张/i.test(lastText);

          const variantTargets = [];
          const seen = new Set();
          const collectVariant = (el) => {
            if (!el || inSidebar(el)) return;
            let img = el.tagName === 'IMG' ? el : el.querySelector?.('img');
            if (!img) return;
            const src = img.currentSrc || img.src || '';
            if (!isGenImg(src) || isBadImg(src)) return;
            const w = img.naturalWidth || img.width || img.getBoundingClientRect().width || 0;
            const h = img.naturalHeight || img.height || img.getBoundingClientRect().height || 0;
            if (w < 48 && h < 48) return;
            const key = src.slice(0, 160);
            if (seen.has(key)) return;
            seen.add(key);
            let target = el;
            if (el.tagName === 'IMG') {
              target = el.closest('button') || el.closest('[role="button"]') || el.closest('a') || el.parentElement || el;
            }
            variantTargets.push({ target, area: w * h });
          };

          last.querySelectorAll('[data-testid*="image-gen"], [data-testid*="generated"]').forEach(collectVariant);
          last.querySelectorAll('button, [role="button"], img').forEach(collectVariant);

          if (variantTargets.length >= 2 || (hintPick && variantTargets.length >= 1)) {
            const stillGenerating = !!document.querySelector('[data-testid="stop-button"], button[aria-label*="Stop"], button[aria-label*="停止"]');
            if (!stillGenerating || hintPick) {
              if (click(variantTargets[0].target)) return 'variant-tile';
            }
          }

          for (const tile of root.querySelectorAll('[data-testid="image-gen-image"], [data-testid="generated-image"]')) {
            if (inSidebar(tile)) continue;
            if (click(tile)) return 'testid-tile';
          }

          return '';
        }
        """
    )
    if clicked:
        await asyncio.sleep(1.2)
        return True
    return False


async def _conversation_turn_max(page: Page) -> int:
    return int(
        await page.evaluate(
            """
            () => {
              let max = -1;
              document.querySelectorAll('[data-testid^="conversation-turn-"]').forEach((el) => {
                const m = (el.getAttribute('data-testid') || '').match(/conversation-turn-(\\d+)/);
                if (m) max = Math.max(max, Number(m[1]));
              });
              return max;
            }
            """
        )
    )


async def _probe_best_assistant_image(
    page: Page,
    conversation_turn_before: int = -1,
) -> dict | None:
    return await page.evaluate(
        """
        (turnBefore) => {
          const pickSrc = (img) => {
            const srcset = img.getAttribute('srcset') || '';
            if (srcset) {
              let best = { url: '', w: 0 };
              for (const part of srcset.split(',')) {
                const bits = part.trim().split(/\\s+/);
                const url = bits[0];
                const w = parseInt(bits[1], 10) || 0;
                if (url && w >= best.w) best = { url, w };
              }
              if (best.url) return best.url;
            }
            return img.currentSrc || img.src || '';
          };

          let best = null;
          function consider(img, index) {
            const src = pickSrc(img);
            if (!src || src.startsWith('data:image/svg')) return;
            if (/avatar|logo|favicon|profile|emoji/i.test(src)) return;
            const alt = img.getAttribute('alt') || '';
            const buttonLabel = img.closest('button')?.getAttribute('aria-label') || '';
            if (/^_ref_|\\b_ref_|uploaded|上传/i.test(alt + ' ' + buttonLabel)) return;

            const turnNode = img.closest('[data-testid^="conversation-turn-"]');
            const turnMatch = (turnNode?.getAttribute('data-testid') || '')
              .match(/conversation-turn-(\\d+)/);
            const turn = turnMatch ? Number(turnMatch[1]) : -1;
            if (turn <= turnBefore) return;

            const rect = img.getBoundingClientRect();
            const w = img.naturalWidth || img.width || rect.width || 0;
            const h = img.naturalHeight || img.height || rect.height || 0;
            if (w < 32 && h < 32) return;
            const area = w * h;
            if (!best || turn > best.turn || (turn === best.turn && area > best.area)) {
              best = { src, w, h, area, index, turn, msgIndex: -1, complete: !!img.complete };
            }
          }

          const main = document.querySelector('[data-testid="conversation-turns"]')
            || document.querySelector('main');
          if (main) {
            main.querySelectorAll('img, picture img').forEach((img, index) => consider(img, index));
          }
          return best;
        }
        """,
        conversation_turn_before,
    )


async def _probe_last_assistant_image(page: Page) -> dict | None:
    return await _probe_best_assistant_image(page, -1)


async def _force_save_page_image(
    page: Page,
    output_dir: Path,
    *,
    conversation_turn_before: int = -1,
) -> Path:
    """页面已有图时强制落盘（跳过 is_new / stable 等门禁）。"""
    import logging

    logger = logging.getLogger(__name__)
    output_dir.mkdir(parents=True, exist_ok=True)
    probe = await _probe_best_assistant_image(page, conversation_turn_before)
    logger.info("force_save probe=%s", probe)

    if probe:
        saved = await _try_save_probe(page, output_dir, probe)
        if saved and saved.exists() and saved.stat().st_size > 512:
            logger.info("force_save ok via probe -> %s", saved)
            return saved

    saved = await _save_image_from_last_message(page, output_dir)
    if saved and saved.exists() and saved.stat().st_size > 512:
        logger.info("force_save ok via last_message -> %s", saved)
        return saved

    saved = await _screenshot_last_assistant_message(page, output_dir)
    if saved and saved.exists() and saved.stat().st_size > 512:
        logger.info("force_save ok via screenshot -> %s", saved)
        return saved

    raise RuntimeError(f"页面有图但强制保存失败，probe={probe}")


async def _try_browser_download(page: Page, output_dir: Path, img_index: int | None = None) -> Path | None:
    try:
        last_msg = page.locator('[data-message-author-role="assistant"]').last
        if await last_msg.count() == 0:
            return None
        imgs = last_msg.locator("img")
        img_count = await imgs.count()
        if img_count == 0:
            return None
        pick = img_index if img_index is not None and img_index < img_count else img_count - 1
        img = imgs.nth(pick)
        await img.scroll_into_view_if_needed(timeout=10000)
        await img.click(timeout=5000)
        await asyncio.sleep(0.3)
        await img.hover(timeout=5000)
        await asyncio.sleep(0.8)

        dl_in_msg = last_msg.locator(
            "button[aria-label*='Download' i], button[aria-label*='下载'], [data-testid*='download' i]"
        )
        dl_global = page.locator(
            "button[aria-label*='Download' i], button[aria-label*='下载'], [data-testid*='download' i]"
        )
        dl_btn = dl_in_msg if await dl_in_msg.count() else dl_global
        if await dl_btn.count() == 0:
            return None

        async with page.expect_download(timeout=60000) as download_info:
            await dl_btn.first.click(timeout=5000)
        download = await download_info.value
        suffix = Path(download.suggested_filename or "image.png").suffix or ".png"
        out = unique_image_path(output_dir, suffix)
        await download.save_as(str(out))
        if out.exists() and out.stat().st_size > 512:
            return out
    except Exception:
        return None
    return None


async def _save_image_from_last_message(page: Page, output_dir: Path) -> Path | None:
    probe = await _probe_best_assistant_image(page, -1)
    if not probe:
        return None

    src = str(probe.get("src") or "")
    imgs = page.locator("main img")
    index = await imgs.evaluate_all(
        "(els, wanted) => els.findIndex((el) => (el.currentSrc || el.src || '') === wanted)",
        src,
    )
    if index < 0:
        return None
    img = imgs.nth(index)
    await img.scroll_into_view_if_needed(timeout=10000)
    await asyncio.sleep(0.5)

    src = await img.evaluate("(el) => el.currentSrc || el.src || ''")
    if src and not src.startswith("data:image/svg"):
        try:
            return await _download_image(page, src, output_dir)
        except Exception:
            pass

    suffix = ".png"
    if src.startswith("data:image/"):
        match = re.match(r"data:image/([^;]+);base64,", src)
        if match:
            suffix = f".{match.group(1).split('/')[-1]}"
    out = unique_image_path(output_dir, suffix)
    await img.screenshot(path=str(out))
    if out.exists() and out.stat().st_size > 512:
        return out
    return None


async def _screenshot_last_assistant_message(page: Page, output_dir: Path) -> Path | None:
    last_msg = page.locator('[data-message-author-role="assistant"]').last
    if await last_msg.count() == 0:
        return None
    out = unique_image_path(output_dir, ".png")
    await last_msg.screenshot(path=str(out))
    if out.exists() and out.stat().st_size > 512:
        return out
    return None


async def _assistant_message_count(page: Page) -> int:
    return int(
        await page.evaluate(
            "() => document.querySelectorAll('[data-message-author-role=\"assistant\"]').length"
        )
        or 0
    )


async def _try_save_probe(
    page: Page,
    output_dir: Path,
    probe: dict,
) -> Path | None:
    src = str(probe.get("src") or "")
    if src and not src.startswith("data:image/svg"):
        try:
            saved = await _download_image(page, src, output_dir)
            if saved.exists() and saved.stat().st_size > 512:
                return saved
        except Exception:
            pass

    saved = await _save_image_from_last_message(page, output_dir)
    if saved:
        return saved
    saved = await _try_browser_download(page, output_dir, img_index=int(probe.get("index", 0)))
    if saved:
        return saved
    return None


async def _save_last_assistant_image(
    page: Page,
    before: set[str],
    output_dir: Path,
    *,
    timeout: float = 360.0,
    assistant_turn_before: int = 0,
    conversation_turn_before: int = -1,
) -> Path:
    import logging

    logger = logging.getLogger(__name__)
    output_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    last_probe: dict | None = None
    stable_hits = 0

    while time.time() < deadline:
        await _auto_handle_image_ui(page)
        turn_now = await _assistant_message_count(page)
        has_new_reply = turn_now > assistant_turn_before
        probe = await _probe_best_assistant_image(page, conversation_turn_before)
        if probe and probe.get("src"):
            last_probe = probe
            large_enough = int(probe.get("w") or 0) >= 48 or int(probe.get("h") or 0) >= 48
            is_new_src = not before or probe["src"] not in before
            should_save = has_new_reply or is_new_src
            if should_save and large_enough:
                stable_hits += 1
            else:
                stable_hits = 0
            need_stable = 1 if has_new_reply else 2
            if should_save and large_enough and stable_hits >= need_stable:
                saved = await _try_save_probe(page, output_dir, probe)
                if saved and saved.exists() and saved.stat().st_size > 512:
                    logger.info("saved image to %s (%s bytes)", saved, saved.stat().st_size)
                    return saved
                stable_hits = 0

        page_text = await page.evaluate("() => document.body.innerText.slice(0, 4000)")
        if any(
            token in page_text.lower()
            for token in (
                "something went wrong",
                "unable to generate",
                "content policy",
                "无法生成",
                "出错了",
                "内容政策",
            )
        ):
            raise RuntimeError(f"ChatGPT 页面报错: {page_text[:300]}")
        await asyncio.sleep(1.5)

    turn_now = await _assistant_message_count(page)
    has_new_reply = turn_now > assistant_turn_before
    if last_probe and (has_new_reply or not before or last_probe.get("src") not in before):
        saved = await _try_save_probe(page, output_dir, last_probe)
        if saved and saved.exists() and saved.stat().st_size > 512:
            logger.info("saved image (fallback) to %s", saved)
            return saved

    saved = await _save_image_from_last_message(page, output_dir)
    if saved and saved.exists() and saved.stat().st_size > 512:
        return saved

    saved = await _screenshot_last_assistant_message(page, output_dir)
    if saved and saved.exists() and saved.stat().st_size > 512:
        return saved

    raise RuntimeError(
        f"页面已有生成图，但无法保存到 output 目录: {output_dir} "
        f"(turn_before={assistant_turn_before}, turn_now={turn_now}, before={len(before)})"
    )


async def _collect_assistant_images(page: Page) -> list[str]:
    return await page.evaluate(
        """
        () => {
          const pickSrc = (img) => {
            if (!img) return '';
            const srcset = img.getAttribute('srcset') || '';
            if (srcset) {
              const parts = srcset.split(',').map(s => s.trim().split(' ')[0]).filter(Boolean);
              if (parts.length) return parts[parts.length - 1];
            }
            return img.currentSrc || img.src || '';
          };
          const out = [];
          const messages = document.querySelectorAll('[data-message-author-role="assistant"]');
          const scope = messages.length ? messages[messages.length - 1] : document;
          scope.querySelectorAll('img').forEach((img) => {
            const src = pickSrc(img);
            if (!src) return;
            if (src.startsWith('data:image/svg')) return;
            if (/avatar|logo|favicon|profile/i.test(src)) return;
            if (img.width && img.width < 80 && img.height && img.height < 80) return;
            out.push(src);
          });
          return [...new Set(out)];
        }
        """
    )


async def _collect_all_assistant_images(page: Page) -> list[str]:
    return await page.evaluate(
        """
        () => {
          const pickSrc = (img) => img.currentSrc || img.src || '';
          const out = [];
          document.querySelectorAll('[data-message-author-role="assistant"] img').forEach((img) => {
            const src = pickSrc(img);
            if (!src || src.startsWith('data:image/svg')) return;
            if (/avatar|logo|favicon|profile/i.test(src)) return;
            out.push(src);
          });
          return [...new Set(out)];
        }
        """
    )


async def _wait_until_not_generating(page: Page, timeout: float = 180.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        await _auto_handle_image_ui(page)
        generating = await page.evaluate(
            """
            () => {
              const txt = document.body.innerText || '';
              if (/generating image|正在生成图像|creating image/i.test(txt)) return true;
              const stopBtn = document.querySelector('[data-testid="stop-button"]');
              if (!stopBtn) return false;
              const rect = stopBtn.getBoundingClientRect();
              return rect.width > 0 && rect.height > 0;
            }
            """
        )
        if not generating:
            await asyncio.sleep(2)
            return
        await asyncio.sleep(1.5)
    raise TimeoutError("模型仍在生成中，等待超时。")


async def _wait_for_new_image(page: Page, before: set[str], timeout: float = 240.0) -> str:
    deadline = time.time() + timeout
    stable_hits = 0
    last_candidate = ""

    while time.time() < deadline:
        await _auto_handle_image_ui(page)
        current = set(await _collect_assistant_images(page))
        fresh = [src for src in current if src not in before]
        if fresh:
            # 优先完整分辨率 CDN 链接，避免抓到预览图
            fresh.sort(key=lambda src: (
                "oaiusercontent.com" not in src,
                "blur" in src or "preview" in src,
                len(src),
            ))
            candidate = fresh[0]
            if candidate == last_candidate:
                stable_hits += 1
            else:
                stable_hits = 0
                last_candidate = candidate
            if stable_hits >= 2:
                return candidate

        page_text = await page.evaluate("() => document.body.innerText.slice(0, 4000)")
        if any(
            token in page_text.lower()
            for token in (
                "something went wrong",
                "unable to generate",
                "content policy",
                "无法生成",
                "出错了",
                "内容政策",
            )
        ):
            raise RuntimeError(f"ChatGPT 页面报错: {page_text[:300]}")

        await asyncio.sleep(2)

    raise TimeoutError(
        "等待图片生成超时。请确认：1) 已在调试浏览器登录 ChatGPT；"
        "2) 账号支持网页端出图；3) 不是临时聊天窗口。"
    )


async def _fill_composer_text(page: Page, composer, text: str) -> None:
    """用 Playwright 原生 API 填 prompt，避免 evaluate/textContent 中文乱码。"""
    prose = page.locator("div.ProseMirror[contenteditable='true']")
    if await prose.count():
        target = prose.first
        await target.click()
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await page.keyboard.insert_text(text)
        return

    tag = await composer.evaluate("el => el.tagName")
    if tag == "TEXTAREA":
        await composer.evaluate(
            """
            (el, value) => {
              el.value = value;
              el.dispatchEvent(new Event('input', { bubbles: true }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
            }
            """,
            text,
        )
        return

    await composer.click()
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Backspace")
    await page.keyboard.insert_text(text)


async def _submit_prompt_on_page(page: Page, prompt: str, *, new_chat: bool = True) -> None:
    await _dismiss_popups(page)
    if new_chat:
        await _start_new_chat(page)
    await _maybe_enable_image_tool(page)

    composer = None
    for selector in COMPOSER_SELECTORS:
        try:
            await page.wait_for_selector(selector, state="visible", timeout=45000)
            locator = page.locator(selector)
            if await locator.count():
                composer = locator.first
                break
        except Exception:
            continue

    if composer is None:
        snippet = await page.evaluate("() => (document.body.innerText || '').slice(0, 500)")
        url = page.url
        raise RuntimeError(
            f"找不到 ChatGPT 输入框。当前页面: {url} | 内容摘要: {snippet[:200]}"
        )

    await composer.wait_for(state="visible", timeout=30000)
    await composer.click()
    normalized = _normalize_image_prompt(prompt)
    await _fill_composer_text(page, composer, normalized)
    await asyncio.sleep(0.4)
    send = page.locator(
        "button[data-testid='send-button'], button[aria-label*='Send'], button[aria-label*='发送']"
    )
    if await send.count():
        await send.first.click(timeout=5000)
    else:
        await page.keyboard.press("Enter")

    # 发送后可能出现「是否创建图像 / 请选择」等交互
    for _ in range(8):
        if await _auto_handle_image_ui(page):
            await asyncio.sleep(1.0)
        else:
            break


async def _submit_prompt(page: Page, prompt: str) -> None:
    await _submit_prompt_on_page(page, prompt, new_chat=True)


async def _download_image(page: Page, image_url: str, output_dir: Path) -> Path:
    if image_url.startswith("data:"):
        match = re.match(r"data:(image/[^;]+);base64,(.+)", image_url, re.S)
        if not match:
            raise RuntimeError("无法解析 base64 图片。")
        ext = match.group(1).split("/")[-1]
        raw = base64.b64decode(match.group(2))
        out = unique_image_path(output_dir, f".{ext}")
        out.write_bytes(raw)
        return out

    raw = await page.evaluate(
        """
        async (url) => {
          const res = await fetch(url, { credentials: 'include' });
          if (!res.ok) throw new Error('download failed: ' + res.status);
          const buf = await res.arrayBuffer();
          return Array.from(new Uint8Array(buf));
        }
        """,
        image_url,
    )
    suffix = Path(urlparse(image_url).path).suffix or ".png"
    out = unique_image_path(output_dir, suffix)
    out.write_bytes(bytes(raw))
    return out


def _normalize_conv_prompt(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def conversation_prompt_matches(expected: str, actual: str) -> bool:
    a = _normalize_conv_prompt(expected)
    b = _normalize_conv_prompt(actual)
    if not a or not b:
        return False
    if a == b:
        return True
    for prefix in ("请生成一张图片：", "请基于参考图生成一张新图片", "请根据以下描述生成一张图片："):
        if b == f"{prefix}{a}" or a == f"{prefix}{b}":
            return True
        if b.startswith(prefix):
            b2 = b[len(prefix) :].strip()
            if a == b2 or a in b2 or b2 in a:
                return True
        if a.startswith(prefix):
            a2 = a[len(prefix) :].strip()
            if a2 and (a2 == b or a2 in b or b in a2):
                return True
    if a in b or (len(a) >= 12 and b in a):
        return True
    compact_a = re.sub(r"[\s，。、“”‘’！？,.!?;:：；、]+", "", a)
    compact_b = re.sub(r"[\s，。、“”‘’！？,.!?;:：；、]+", "", b)
    if compact_a and compact_b:
        if compact_a == compact_b or compact_a in compact_b:
            return True
        if len(compact_b) >= 12 and compact_b in compact_a:
            return True
    head = a.split("。")[0].split(".")[0].strip()
    if len(head) >= 4 and head in b:
        return True
    return False


async def _scroll_conversation_history(page: Page) -> None:
    await page.evaluate(
        """
        async () => {
          const root = document.querySelector('[data-testid="conversation-turns"]')
            || document.querySelector('main');
          if (!root) return;
          for (let i = 0; i < 25; i++) {
            const before = root.scrollHeight;
            root.scrollTop = 0;
            root.scrollBy(0, -2500);
            await new Promise((r) => setTimeout(r, 250));
            if (root.scrollHeight <= before && root.scrollTop <= 2) break;
          }
        }
        """
    )
    await asyncio.sleep(0.6)


async def collect_conversation_image_turns(page: Page) -> list[dict]:
    await _scroll_conversation_history(page)
    return await page.evaluate(
        """
        () => {
          const pickSrc = (img) => {
            const srcset = img.getAttribute('srcset') || '';
            if (srcset) {
              let best = { url: '', w: 0 };
              for (const part of srcset.split(',')) {
                const bits = part.trim().split(/\\s+/);
                const url = bits[0];
                const w = parseInt(bits[1], 10) || 0;
                if (url && w >= best.w) best = { url, w };
              }
              if (best.url) return best.url;
            }
            return img.currentSrc || img.src || '';
          };
          const pairs = [];
          let lastUser = '';
          document.querySelectorAll('[data-message-author-role]').forEach((msg) => {
            const role = msg.getAttribute('data-message-author-role');
            if (role === 'user') {
              lastUser = (msg.innerText || '').trim();
              return;
            }
            if (role !== 'assistant' || !lastUser) return;
            const imgs = [];
            msg.querySelectorAll('img').forEach((img) => {
              const src = pickSrc(img);
              if (!src || src.startsWith('data:image/svg')) return;
              if (/avatar|logo|favicon|profile|emoji/i.test(src)) return;
              const alt = img.getAttribute('alt') || '';
              const label = img.closest('button')?.getAttribute('aria-label') || '';
              if (/^_ref_|uploaded|上传/i.test(alt + ' ' + label)) return;
              const w = img.naturalWidth || img.width || 0;
              const h = img.naturalHeight || img.height || 0;
              if (w && h && w < 48 && h < 48) return;
              imgs.push(src);
            });
            if (imgs.length) {
              pairs.push({ prompt: lastUser, src: imgs[imgs.length - 1] });
            }
          });
          return pairs;
        }
        """
    )


async def recover_conversation_image(
    page: Page,
    prompt: str,
    output_dir: Path,
) -> Path:
    turns = await collect_conversation_image_turns(page)
    matched = [item for item in turns if conversation_prompt_matches(prompt, str(item.get("prompt") or ""))]
    if not matched:
        raise RuntimeError(
            f"当前对话里没有匹配到这张提示词对应的图（对话中有 {len(turns)} 张已生成图）。"
        )
    src = str(matched[-1].get("src") or "")
    if not src:
        raise RuntimeError("匹配到对话回合，但没有可用图片地址。")
    saved = await _download_image(page, src, output_dir)
    if not saved.exists() or saved.stat().st_size <= 512:
        raise RuntimeError("从对话下载图片失败。")
    return saved


async def _generate_via_cdp(
    prompt: str,
    cdp_url: str,
    output_dir: Path,
) -> Path:
    async with async_playwright() as playwright:
        browser: Browser = await playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError("CDP 已连接，但没有浏览器上下文。")

        context = browser.contexts[0]
        page = await _find_chatgpt_page(context)

        if "auth" in page.url or "login" in page.url.lower():
            raise RuntimeError("调试浏览器尚未登录 ChatGPT。")

        before = set(await _collect_assistant_images(page))
        await _submit_prompt(page, prompt)
        await _wait_until_not_generating(page)
        return await _save_last_assistant_image(page, before, output_dir)


def _jar_to_playwright_cookies(jar) -> list[dict[str, object]]:
    cookies: list[dict[str, object]] = []
    for cookie in jar:
        domain = cookie.domain or "chatgpt.com"
        if "chatgpt.com" not in domain and "openai.com" not in domain:
            continue
        item: dict[str, object] = {
            "name": cookie.name,
            "value": cookie.value,
            "domain": domain,
            "path": cookie.path or "/",
        }
        if cookie.expires:
            item["expires"] = float(cookie.expires)
        if cookie.secure:
            item["secure"] = True
        cookies.append(item)
    return cookies


async def _generate_via_playwright_cookies(
    prompt: str,
    output_dir: Path,
) -> Path:
    """Chrome 无法开 CDP 时，读取 Chrome Cookie 并用 Playwright 启动 Chrome 出图。"""
    jar, browser_name = extract_cookie_jar()
    cookies = _jar_to_playwright_cookies(jar)
    if not cookies:
        raise RuntimeError(f"从 {browser_name} 读到 0 条 ChatGPT Cookie。")

    profile_dir = output_dir.parent / ".playwright-chrome-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=False,
            viewport={"width": 1360, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await context.add_cookies(cookies)
            await page.goto(f"{CHATGPT_ORIGIN}/", wait_until="domcontentloaded", timeout=90000)
            await asyncio.sleep(3)

            if "auth" in page.url or "login" in page.url.lower():
                raise RuntimeError(
                    "Playwright 已注入 Chrome Cookie，但页面仍未登录。"
                    "请先在 Chrome 登录 chatgpt.com，并完全退出 Chrome 后再试。"
                )

            before = set(await _collect_assistant_images(page))
            await _submit_prompt(page, prompt)
            await _wait_until_not_generating(page)
            return await _save_last_assistant_image(page, before, output_dir)
        finally:
            await context.close()


async def generate_image_via_browser(
    prompt: str,
    cdp_url: str = CDP_URL,
    output_dir: Path = OUTPUT_DIR,
    auto_launch_browser: bool = True,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)

    if cdp_reachable(cdp_url):
        try:
            return await _generate_via_cdp(prompt, cdp_url, output_dir)
        except Exception:
            pass
    elif auto_launch_browser:
        try:
            ensure_cdp_ready(cdp_url, auto_launch=auto_launch_browser)
            if cdp_reachable(cdp_url):
                return await _generate_via_cdp(prompt, cdp_url, output_dir)
        except Exception:
            pass

    return await _generate_via_playwright_cookies(prompt, output_dir)


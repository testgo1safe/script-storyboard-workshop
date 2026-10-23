from __future__ import annotations

import asyncio
import json

from playwright.async_api import async_playwright


async def main() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp("http://127.0.0.1:9333")
        for page in browser.contexts[0].pages:
            if "chatgpt.com" not in page.url:
                continue
            data = await page.evaluate(
                """
                () => [...document.querySelectorAll('main img')].map((img, index) => {
                  const assistant = img.closest('[data-message-author-role="assistant"]');
                  const article = img.closest('article');
                  const testNode = img.closest('[data-testid]');
                  const rect = img.getBoundingClientRect();
                  return {
                    index,
                    src: (img.currentSrc || img.src || '').slice(0, 240),
                    w: img.naturalWidth || img.width || rect.width || 0,
                    h: img.naturalHeight || img.height || rect.height || 0,
                    assistantIndex: assistant
                      ? [...document.querySelectorAll('[data-message-author-role="assistant"]')].indexOf(assistant)
                      : -1,
                    articleText: (article?.innerText || '').slice(0, 120),
                    testid: testNode?.getAttribute('data-testid') || '',
                    parent: img.parentElement?.outerHTML?.slice(0, 300) || '',
                  };
                })
                """
            )
            print(json.dumps({"url": page.url, "images": data}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())


import asyncio
import json

from app.playwright_session import get_chatgpt_page, ensure_logged_in


async def main() -> None:
    page = await get_chatgpt_page()
    await ensure_logged_in(page)
    state = await page.evaluate(
        """
        () => ({
          url: location.href,
          title: document.title,
          bodySnippet: (document.body.innerText || '').slice(0, 800),
          selectors: {
            prose: !!document.querySelector("div.ProseMirror[contenteditable='true']"),
            promptTextarea: !!document.querySelector('#prompt-textarea'),
            testidPrompt: !!document.querySelector('[data-testid="prompt-textarea"]'),
            textarea: !!document.querySelector('textarea'),
            login: !!document.querySelector('[data-testid="login-button"]'),
            composer: !!document.querySelector('#prompt-textarea, [data-testid="prompt-textarea"], div.ProseMirror'),
          },
        })
        """
    )
    print(json.dumps(state, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())


from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

API = "http://127.0.0.1:8765"
HEADERS = {"Authorization": "Bearer local-dev-key"}
OUTPUT = Path(__file__).resolve().parent.parent / "output"
PROMPT = "生成一张纯紫色背景中央有一个巨大亮黄色圆形的图片，简洁扁平风格，不要人物，不要文字"


def main() -> int:
    before = {p.name for p in OUTPUT.glob("image_*.png")}
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{API}/v1/images/generations",
            headers=HEADERS,
            json={
                "prompt": PROMPT,
                "keep_conversation": True,
                "async_mode": True,
            },
        )
        response.raise_for_status()
        submitted = response.json()
        print(json.dumps(submitted, ensure_ascii=False))
        poll_url = submitted["poll_url"]

        deadline = time.time() + 900
        while time.time() < deadline:
            time.sleep(3)
            job = client.get(f"{API}{poll_url}", headers=HEADERS).json()
            print(
                json.dumps(
                    {
                        "status": job.get("status"),
                        "error": job.get("error"),
                        "filename": job.get("filename"),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if job.get("status") == "failed":
                return 2
            if job.get("status") != "completed":
                continue

            new_files = [p for p in OUTPUT.glob("image_*.png") if p.name not in before]
            if not new_files:
                print("FAIL:no_new_output_file")
                return 3
            newest = max(new_files, key=lambda p: p.stat().st_mtime)
            url = job.get("url")
            fetched = client.get(url) if url else None
            result = {
                "file": str(newest),
                "bytes": newest.stat().st_size,
                "url": url,
                "url_status": fetched.status_code if fetched else None,
                "url_bytes": len(fetched.content) if fetched else None,
            }
            print("RESULT:" + json.dumps(result, ensure_ascii=False), flush=True)
            return 0

    print("FAIL:timeout")
    return 4


if __name__ == "__main__":
    sys.exit(main())


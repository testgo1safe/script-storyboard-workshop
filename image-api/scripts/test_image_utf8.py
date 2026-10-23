"""UTF-8 出图测试：验证中文 prompt 与 output 同步。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

API = "http://127.0.0.1:8765"
KEY = "local-dev-key"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
PROMPT = "古代庭院，白天，一名青衣男子站在院子中央，电影感写实"


def main() -> int:
    before = {p.name for p in OUTPUT_DIR.glob("*.png")}
    print(f"prompt: {PROMPT}")
    print(f"output before: {len(before)} png files")

    t0 = time.time()
    with httpx.Client(timeout=600.0) as client:
        r = client.post(
            f"{API}/v1/images/generations",
            headers={"Authorization": f"Bearer {KEY}"},
            json={"prompt": PROMPT, "keep_conversation": True},
        )
    elapsed = time.time() - t0
    print(f"status: {r.status_code} elapsed: {elapsed:.1f}s")

    if r.status_code != 200:
        print(r.text[:500])
        return 1

    data = r.json()
    print(json.dumps(data, ensure_ascii=False, indent=2))

    url = data.get("data", [{}])[0].get("url")
    if url:
        img = httpx.get(url, timeout=30)
        print(f"url fetch: {img.status_code} bytes={len(img.content)}")

    after = list(OUTPUT_DIR.glob("*.png"))
    new_files = [p for p in after if p.name not in before]
    print(f"new output files: {[p.name for p in new_files]}")
    for p in new_files:
        print(f"  {p.name} size={p.stat().st_size}")

    if not new_files:
        print("FAIL: no new png in output/")
        return 1
    if url and new_files:
        fname = new_files[-1].name
        if fname not in url:
            print(f"WARN: url may not match latest file ({fname})")
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())


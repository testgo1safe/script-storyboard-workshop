from __future__ import annotations

import base64
import hashlib
import json
import random
import time
import uuid
from datetime import datetime, timezone, timedelta

from .config import DEFAULT_USER_AGENT

_CORES = [8, 16, 24, 32]
_NAV_KEYS = [
    "webdriver−false",
    "language−en-US",
    "vendor−Google Inc.",
    "platform−Win32",
    "hardwareConcurrency−32",
]
_DOC_KEYS = ["_reactListeningo743lnnpvdg", "location"]
_WIN_KEYS = ["window", "document", "navigator", "location", "fetch"]


def _pick(items: list[str]) -> str:
    return random.choice(items)


def _parse_time() -> str:
    now = datetime.now(timezone(timedelta(hours=-5)))
    return now.strftime("%a %b %d %Y %H:%M:%S GMT-0500 (Eastern Standard Time)")


def _build_config(user_agent: str) -> list[object]:
    return [
        random.choice([1920 + 1080, 2560 + 1440, 1920 + 1200, 2560 + 1600]),
        _parse_time(),
        4294705152,
        0,
        user_agent,
        "",
        "",
        "en-US",
        "en-US,es-US,en,es",
        0,
        _pick(_NAV_KEYS),
        _pick(_DOC_KEYS),
        _pick(_WIN_KEYS),
        random.random() * 1000,
        str(uuid.uuid4()),
        "",
        _pick([str(c) for c in _CORES]),
        time.time() * 1000,
    ]


def _compact(arr: list[object], start: int = 0, end: int | None = None) -> str:
    end = len(arr) if end is None else end
    return json.dumps(arr[start:end], separators=(",", ":"))[1:-1]


def _generate_answer(seed: str, diff: str, config: list[object]) -> str:
    slice_len = len(diff)
    seed_encoded = seed.encode("utf-8")
    part1 = f"[{_compact(config, 0, 3)},".encode("utf-8")
    part2 = f",{_compact(config, 4, 9)},".encode("utf-8")
    part3 = f",{_compact(config, 10)}]".encode("utf-8")
    target = bytes.fromhex(diff)

    for i in range(500_000):
        dyn_i = str(i).encode("utf-8")
        dyn_j = str(i >> 1).encode("utf-8")
        final_bytes = part1 + dyn_i + part2 + dyn_j + part3
        base_encoded = base64.b64encode(final_bytes).decode("ascii")
        digest = hashlib.sha3_512(seed_encoded + base_encoded.encode("utf-8")).digest()
        if digest[:slice_len] <= target:
            return base_encoded
    fallback = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D" + base64.b64encode(f'"{seed}"'.encode()).decode()
    return fallback


def get_requirements_token(user_agent: str = DEFAULT_USER_AGENT) -> str:
    config = _build_config(user_agent)
    answer = _generate_answer(str(random.random()), "0fffff", config)
    return "gAAAAAC" + answer


def solve_pow(seed: str, difficulty: str, user_agent: str = DEFAULT_USER_AGENT) -> str:
    config = _build_config(user_agent)
    answer = _generate_answer(seed, difficulty, config)
    return "gAAAAAB" + answer


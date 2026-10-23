from __future__ import annotations

import re
from dataclasses import dataclass

from . import llm


@dataclass
class ParsedEpisode:
    source_index: int
    title: str
    script: str


_CN_DIGIT = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

# 行首集标记：【第1集：标题】 / 第一集：标题 / 第 2 集
_HEADING_RE = re.compile(
    r"(?m)^[ \t]*(?:#{1,3}[ \t]*)?"
    r"(?:"
    r"[【\[][ \t]*第[ \t]*(?P<bnum>[0-9]{1,3}|[一二三四五六七八九十两零〇百]{1,6})[ \t]*集"
    r"[ \t]*[：:·\-—]?[ \t]*(?P<btitle>[^】\]\r\n]{0,80})[ \t]*[】\]]"
    r"|"
    r"第[ \t]*(?P<nnum>[0-9]{1,3}|[一二三四五六七八九十两零〇百]{1,6})[ \t]*集"
    r"(?:[ \t]*[：:·\-—][ \t]*(?P<ntitle>[^\r\n]{0,80}))?"
    r")"
    r"[ \t]*(?P<rest>[^\r\n]*)\r?$"
)

_CUT_TO_BLACK = re.compile(r"[ \t]*[【\[]?\s*切黑\s*[】\]]?[ \t]*$")


def parse_cn_int(raw: str) -> int | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.isdigit():
        value = int(text)
        return value if 1 <= value <= 200 else None
    if text in _CN_DIGIT:
        value = _CN_DIGIT[text]
        return value if value >= 1 else None
    if text == "十":
        return 10
    if text.startswith("十"):
        tail = _CN_DIGIT.get(text[1:])
        return 10 + tail if tail is not None else None
    if text.endswith("十") and text[:-1] in _CN_DIGIT:
        return _CN_DIGIT[text[:-1]] * 10
    if "十" in text:
        head, tail = text.split("十", 1)
        left = _CN_DIGIT.get(head)
        right = _CN_DIGIT.get(tail, 0) if tail else 0
        if left is None:
            return None
        return left * 10 + right
    return None


def count_marked_episodes(script: str) -> int:
    return len(_HEADING_RE.findall(script or ""))


def _clean_title(title: str) -> str:
    text = (title or "").strip(" 　·-—:：【】[]")
    text = re.sub(r"^第\s*[0-9一二三四五六七八九十百]+\s*集\s*", "", text)
    return text.strip(" 　·-—:：")


def _clean_body(body: str) -> str:
    text = (body or "").strip()
    text = _CUT_TO_BLACK.sub("", text).strip()
    return text


def _attach_preamble(preamble: str, body: str) -> str:
    pre = preamble.strip()
    text = body.strip()
    if not pre:
        return text
    if text.startswith(pre[: min(48, len(pre))]):
        return text
    return f"{pre}\n\n{text}"


def parse_marked_episodes(script: str) -> list[ParsedEpisode]:
    text = (script or "").replace("\r\n", "\n").replace("\r", "\n")
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return []

    preamble = text[: matches[0].start()].strip()
    episodes: list[ParsedEpisode] = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        rest = (match.group("rest") or "").strip()
        body = text[match.end() : end].strip()
        if rest:
            body = f"{rest}\n{body}".strip() if body else rest
        body = _clean_body(body)
        if len(body) < 8:
            continue
        raw_num = match.group("bnum") or match.group("nnum") or ""
        source_index = parse_cn_int(raw_num) or (len(episodes) + 1)
        title = _clean_title(match.group("btitle") or match.group("ntitle") or "")
        episodes.append(
            ParsedEpisode(
                source_index=source_index,
                title=title,
                script=_attach_preamble(preamble, body),
            )
        )
    return episodes


def _from_llm_payload(items: list[dict], fallback: str) -> list[ParsedEpisode]:
    episodes: list[ParsedEpisode] = []
    for i, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        body = str(item.get("script") or "").strip()
        if len(body) < 8:
            continue
        title = _clean_title(str(item.get("title") or ""))
        episodes.append(ParsedEpisode(source_index=i, title=title, script=body))
    if episodes:
        return episodes
    return [ParsedEpisode(source_index=1, title="", script=fallback.strip())]


async def resolve_episodes(script: str, *, allow_llm: bool) -> tuple[list[ParsedEpisode], str]:
    text = (script or "").strip()
    marked = parse_marked_episodes(text)
    if len(marked) >= 2:
        return marked, "markers"
    if marked:
        return marked, "markers"
    if allow_llm and len(text) >= 80:
        payload = await llm.split_series_script(text)
        episodes = _from_llm_payload(payload, text)
        return episodes, "llm" if len(episodes) >= 2 else "single"
    return [ParsedEpisode(source_index=1, title="", script=text)], "single"


from __future__ import annotations

import json
import re
from typing import Any

import httpx

from .config import settings
from .clips import (
    apply_llm_prompts,
    apply_seedance_llm_prompts,
    build_video_clips,
    cast_roster_text,
    clip_briefs_text,
    clamp_episode_duration,
    enrich_character_aliases,
    estimate_episode_duration,
    join_episode_prompt,
    join_episode_seedance_prompt,
    pack_clips,
)
from .prompts import (
    ANALYSIS_SYSTEM,
    SEEDANCE_PROMPT_SYSTEM,
    SPLIT_SERIES_SYSTEM,
    VIDEO_PROMPT_SYSTEM,
    build_analysis_user_message,
    build_seedance_prompt_user_message,
    build_split_series_user_message,
    build_video_prompt_user_message,
)
from .schemas import Analysis, Character, PictureBinding, Prop, Scene, VideoClip


class LLMError(RuntimeError):
    pass


def _extract_json(text: str) -> dict[str, Any]:
    raw = text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise LLMError("模型没有返回 JSON 对象")
    return json.loads(raw[start : end + 1])


async def chat(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    json_mode: bool = False,
    temperature: float = 0.6,
    max_tokens: int = 8192,
) -> str:
    if not settings.deepseek_api_key:
        raise LLMError("未配置 DEEPSEEK_API_KEY")

    payload: dict[str, Any] = {
        "model": model or settings.deepseek_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    url = settings.deepseek_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise LLMError(f"DeepSeek 请求失败 {resp.status_code}: {resp.text[:800]}")
        data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise LLMError(f"DeepSeek 响应格式异常: {data}") from exc


async def split_series_script(script: str) -> list[dict[str, str]]:
    content = await chat(
        [
            {"role": "system", "content": SPLIT_SERIES_SYSTEM},
            {"role": "user", "content": build_split_series_user_message(script)},
        ],
        json_mode=True,
        temperature=0.2,
        max_tokens=8192,
    )
    payload = _extract_json(content)
    items = payload.get("episodes")
    if not isinstance(items, list):
        raise LLMError("拆集结果没有 episodes 数组")
    episodes: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        body = str(item.get("script") or "").strip()
        if len(body) < 8:
            continue
        episodes.append(
            {
                "title": str(item.get("title") or "").strip(),
                "script": body,
            }
        )
    if not episodes:
        raise LLMError("模型没有拆出可用的剧集")
    return episodes[:20]


async def analyze_script(
    script: str,
    duration_seconds: int,
    style: str,
    extra_notes: str,
    series_bible: str = "",
    aspect_ratio: str = "9:16",
    duration_auto: bool = True,
) -> Analysis:
    hint = estimate_episode_duration(script)
    user_msg = build_analysis_user_message(
        script,
        duration_seconds,
        style,
        extra_notes,
        series_bible,
        aspect_ratio,
        duration_auto=duration_auto,
        duration_hint=hint["reason"],
    )
    content = await chat(
        [
            {"role": "system", "content": ANALYSIS_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        json_mode=True,
        temperature=0.5,
        max_tokens=8192,
    )
    try:
        parsed = _extract_json(content)
    except (json.JSONDecodeError, LLMError):
        content = await chat(
            [
                {"role": "system", "content": ANALYSIS_SYSTEM},
                {
                    "role": "user",
                    "content": user_msg + "\n\n上次输出不是合法 JSON，请只返回 JSON 对象。",
                },
            ],
            json_mode=True,
            temperature=0.2,
        )
        parsed = _extract_json(content)

    parsed["style"] = parsed.get("style") or style
    parsed["aspect_ratio"] = parsed.get("aspect_ratio") or aspect_ratio
    parsed["duration_auto"] = duration_auto
    if duration_auto:
        rec = parsed.get("duration_seconds")
        try:
            rec = int(rec)
        except (TypeError, ValueError):
            rec = 0
        beats = len(parsed.get("complete_event") or []) or int(hint.get("beats") or 1)
        beats = max(1, min(6, beats))
        lo, hi = beats * 8, beats * 15
        if rec < 8 or rec > 90:
            rec = hint["seconds"]
        elif rec > hi + 6:
            rec = hi
        elif rec < lo:
            rec = lo
        parsed["duration_seconds"] = clamp_episode_duration(rec)
        if not parsed.get("duration_reason"):
            parsed["duration_reason"] = hint["reason"]
    else:
        parsed["duration_seconds"] = duration_seconds
        parsed["duration_reason"] = parsed.get("duration_reason") or f"按指定时长 {duration_seconds} 秒拆段。"
    analysis = Analysis.model_validate(parsed)
    analysis = await _ensure_reference_coverage(analysis, script, series_bible)
    _fill_ids(analysis)
    _normalize_shots(analysis, analysis.duration_seconds, stretch=not duration_auto)
    pack_clips(analysis)
    enrich_character_aliases(analysis, script)
    if duration_auto and analysis.clips:
        analysis.duration_seconds = sum(c.duration_seconds for c in analysis.clips)
    return analysis


def _fill_ids(analysis: Analysis) -> None:
    for i, item in enumerate(analysis.characters, start=1):
        if not item.id:
            item.id = f"c{i}"
    for i, item in enumerate(analysis.scenes, start=1):
        if not item.id:
            item.id = f"s{i}"
    for i, item in enumerate(analysis.props, start=1):
        if not item.id:
            item.id = f"p{i}"


async def _ensure_reference_coverage(
    analysis: Analysis, script: str, series_bible: str = ""
) -> Analysis:
    existing_chars = [c.name for c in analysis.characters]
    existing_scenes = [s.name for s in analysis.scenes]
    existing_props = [p.name for p in analysis.props]
    content = await chat(
        [
            {
                "role": "system",
                "content": (
                    "检查剧本是否还缺需要单独参考图的主体。只返回 JSON："
                    '{"missing_characters":[],"missing_scenes":[],"missing_props":[]}。'
                    "character 字段：id,name,role,age,gender,ethnicity,appearance,wardrobe,"
                    "signature_props,emotion_curve,image_prompt。"
                    "scene 字段：id,name,time,location,atmosphere,lighting,lock_elements,image_prompt。"
                    "prop 字段：id,name,description,lock_note,needs_reference,image_prompt。"
                    "规则：每个具名主演一张；剧本需要出镜的群演/士兵/路人也进 missing_characters，role=群演，"
                    "给一张代表性参考图；每个独立地点一张；只有特写/交接/信物类道具才进 missing_props，"
                    "服装配饰和环境元素不要当道具。已有设定中的同名主体不要放进 missing。没有缺失则对应数组为空。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"已有角色：{existing_chars or '无'}\n"
                    f"已有场景：{existing_scenes or '无'}\n"
                    f"已有道具：{existing_props or '无'}\n"
                    f"剧本：{script}\n"
                    f"logline：{analysis.logline}\n"
                    f"{('本剧已有设定：' + series_bible) if series_bible else ''}\n"
                ),
            },
        ],
        json_mode=True,
        temperature=0.2,
        max_tokens=3072,
    )
    try:
        payload = _extract_json(content)
    except (json.JSONDecodeError, LLMError):
        return analysis

    have_chars = {c.name for c in analysis.characters}
    next_c = len(analysis.characters) + 1
    for item in payload.get("missing_characters") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name in have_chars:
            continue
        item.setdefault("id", f"c{next_c}")
        item.setdefault("role", "群演" if re.search(r"群演|士兵|路人|群众|保镖", name) else "主演")
        try:
            analysis.characters.append(Character.model_validate(item))
            have_chars.add(name)
            next_c += 1
        except Exception:
            continue

    have_scenes = {s.name for s in analysis.scenes}
    next_s = len(analysis.scenes) + 1
    for item in payload.get("missing_scenes") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name in have_scenes:
            continue
        item.setdefault("id", f"s{next_s}")
        try:
            analysis.scenes.append(Scene.model_validate(item))
            have_scenes.add(name)
            next_s += 1
        except Exception:
            continue

    have_props = {p.name for p in analysis.props}
    next_p = len(analysis.props) + 1
    for item in payload.get("missing_props") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name in have_props:
            continue
        item.setdefault("id", f"p{next_p}")
        item.setdefault("needs_reference", True)
        try:
            analysis.props.append(Prop.model_validate(item))
            have_props.add(name)
            next_p += 1
        except Exception:
            continue
    return analysis


def _normalize_shots(analysis: Analysis, duration: int, stretch: bool = True) -> None:
    if not analysis.shots:
        return
    shots = sorted(analysis.shots, key=lambda s: s.start)
    shots[0].start = 0
    for i in range(1, len(shots)):
        if shots[i].start < shots[i - 1].end:
            shots[i].start = shots[i - 1].end
        if shots[i].end <= shots[i].start:
            shots[i].end = shots[i].start + 1
    if stretch:
        shots[-1].end = float(duration)
    analysis.shots = shots


async def write_clip_prompts(
    analysis: Analysis,
    pictures: list[PictureBinding],
    bindings_text: str,
    *,
    high_quality: bool = False,
) -> tuple[list[VideoClip], str]:
    clips = build_video_clips(analysis, pictures)
    model = settings.deepseek_prompt_model
    if high_quality:
        model = "deepseek-reasoner"
    try:
        content = await chat(
            [
                {"role": "system", "content": VIDEO_PROMPT_SYSTEM},
                {
                    "role": "user",
                    "content": build_video_prompt_user_message(
                        analysis.model_dump_json(indent=2, ensure_ascii=False),
                        bindings_text,
                        clip_briefs_text(clips, pictures, analysis),
                        cast_text=cast_roster_text(analysis),
                    ),
                },
            ],
            model=model,
            json_mode=not high_quality,
            temperature=0.45,
            max_tokens=8192,
        )
        payload = _extract_json(content)
        clips = apply_llm_prompts(clips, payload, analysis, pictures)
    except (json.JSONDecodeError, LLMError):
        pass
    try:
        sd_content = await chat(
            [
                {"role": "system", "content": SEEDANCE_PROMPT_SYSTEM},
                {
                    "role": "user",
                    "content": build_seedance_prompt_user_message(
                        analysis.model_dump_json(indent=2, ensure_ascii=False),
                        bindings_text,
                        clip_briefs_text(clips, pictures, analysis),
                        cast_text=cast_roster_text(analysis),
                    ),
                },
            ],
            model=model,
            json_mode=not high_quality,
            temperature=0.4,
            max_tokens=8192,
        )
        sd_payload = _extract_json(sd_content)
        clips = apply_seedance_llm_prompts(clips, sd_payload, analysis, pictures)
    except (json.JSONDecodeError, LLMError):
        pass
    return clips, join_episode_prompt(clips)


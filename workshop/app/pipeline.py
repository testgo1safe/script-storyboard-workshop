from __future__ import annotations

import asyncio
import io
import re
import shutil
import time
import traceback
from pathlib import Path
from typing import Awaitable, Callable

from PIL import Image

from . import clips as clip_lib
from . import edit, images, llm, seedance, storage, video
from .config import settings
from .schemas import Analysis, PictureBinding, ProjectEvent, ProjectState

EmitFn = Callable[[ProjectState], Awaitable[None]]
_video_locks: dict[str, asyncio.Lock] = {}
_active_project_ids: set[str] = set()
_batch_project_ids: set[str] = set()
IMAGE_GENERATION_TIMEOUT = 180
IMAGE_STALE_SECONDS = 210


def mark_project_active(project_id: str) -> None:
    _active_project_ids.add(project_id)


def mark_project_inactive(project_id: str) -> None:
    _active_project_ids.discard(project_id)


def mark_batch_active(project_ids: list[str]) -> None:
    _batch_project_ids.update(project_ids)


def mark_batch_inactive(project_ids: list[str]) -> None:
    _batch_project_ids.difference_update(project_ids)


def is_project_active(project_id: str) -> bool:
    return project_id in _active_project_ids or project_id in _batch_project_ids


def _mark_pic_generating(pic: PictureBinding, *, reset_job: bool = True) -> None:
    pic.status = "generating"
    pic.error = ""
    pic.verify_warning = ""
    if reset_job:
        pic.generated_prompt = ""
        pic.source_url = ""
        pic.source_path = ""
        pic.image_job_id = ""
        pic.image_stamp = ""
    pic.generating_since = time.time()



def _video_lock(project_id: str) -> asyncio.Lock:
    lock = _video_locks.get(project_id)
    if lock is None:
        lock = asyncio.Lock()
        _video_locks[project_id] = lock
    return lock


def _normalize_provider(provider: str) -> str:
    value = (provider or "minimax").strip().lower()
    return "seedance" if value == "seedance" else "minimax"


def _clip_provider(state: ProjectState, clip) -> str:
    return _normalize_provider(getattr(clip, "video_provider", "") or state.video_provider)


def _clip_seedance_model(state: ProjectState, clip) -> str:
    return seedance.normalize_model(getattr(clip, "seedance_model", "") or state.seedance_model)


def _provider_configured(provider: str) -> bool:
    if provider == "seedance":
        return bool(settings.seedance_api_key)
    return bool(settings.minimax_api_key)


def _provider_missing_message(provider: str) -> str:
    if provider == "seedance":
        return "未配置 SEEDANCE_API_KEY"
    return "未配置 MINIMAX_API_KEY"


def _task_video_url(task: dict, provider: str) -> str:
    if provider == "seedance":
        return seedance.video_url_from_task(task)
    content = task.get("content") or {}
    if isinstance(content, dict):
        return str(content.get("url") or content.get("video_url") or "")
    return ""


def _emit(state: ProjectState, stage: str, message: str, progress: int, **extra) -> None:
    state.status = stage if stage in {
        "queued",
        "analyzing",
        "storyboard",
        "imaging",
        "prompting",
        "generating_video",
        "editing",
        "done",
        "error",
    } else state.status
    state.message = message
    state.progress = progress
    state.events.append(
        ProjectEvent(ts=time.time(), stage=stage, message=message, progress=progress, extra=extra)
    )


def ordered_pictures(pictures: list[PictureBinding]) -> list[PictureBinding]:
    return sorted(pictures, key=lambda p: p.picture_index)


def _bindings_text(pictures: list[PictureBinding]) -> str:
    lines = []
    for pic in ordered_pictures(pictures):
        tag = f"<Picture {pic.picture_index}>"
        status = "已生成" if pic.status == "ready" else pic.status
        lines.append(
            f"参考图{pic.picture_index}：{tag} {pic.lock_text}（{status}，文件 {pic.filename or '无'}）"
        )
    lines.append(
        f"共 {len(pictures)} 张参考图。参考图只用于对应人物、场景和道具身份，不得交换参考图，不得新增清晰人物，不得镜像重复主演。"
    )
    return "\n".join(lines)


def _fallback_prompt(kind: str, name: str, details: str) -> str:
    extra = details.strip() or name
    if kind == "extra":
        return (
            f"电影级写实人像照片，单人群演半身至膝，{name}，{extra}，"
            "3/4侧面，视线看向画外，锁服装体态与脸型，"
            "禁止第二张清晰主演人脸、文字、水印、分屏、拼贴、镜像分身。"
        )
    if kind == "character":
        return (
            f"电影级写实人像照片，单人半身至膝，{name}，{extra}，"
            "3/4侧面，视线看向画外，不要直视镜头，真实皮肤纹理与衣物重量，"
            "禁止第二人、文字、水印、分屏、拼贴。"
        )
    if kind == "scene":
        return (
            f"电影级写实建立镜头，{name}，{extra}，锁定空间结构与光线，"
            "禁止清晰可辨人脸、文字、水印、分屏。"
        )
    return (
        f"电影级写实静物特写，单一道具：{name}，{extra}，材质与形态清晰，"
        "禁止人物面孔、文字、水印、分屏。"
    )


_SCENE_WORDS = re.compile(
    r"(客厅|房间|室内|室外|街道|别墅|办公室|审讯|停车场|医院|卧室|书房|走廊|厨房|阳台|天台|场景|空间|建立镜头|空镜)"
)
_PROP_STILL = re.compile(r"(静物特写|道具特写|物品特写)")
_PROP_OBJECT = re.compile(r"(手机|屏幕显示|合同|信件|纸条|证件|纽扣|倒计时)")
_PORTRAIT = re.compile(r"(单人半身|半身像|人像照片|半身至膝)")


def _looks_like_prop_still(text: str) -> bool:
    blob = text or ""
    if _PROP_STILL.search(blob):
        return True
    if "特写" in blob and _PROP_OBJECT.search(blob) and not _SCENE_WORDS.search(blob):
        return True
    return False


def _looks_like_portrait(text: str) -> bool:
    return bool(_PORTRAIT.search(text or ""))


def _prompt_fits_kind(kind: str, name: str, prompt: str) -> bool:
    text = (prompt or "").strip()
    if not text:
        return False
    label = (name or "").strip()
    named = bool(label) and label in text
    if kind == "scene":
        if _looks_like_prop_still(text) and not _SCENE_WORDS.search(text) and not named:
            return False
        if _looks_like_portrait(text) and not any(k in text for k in ("无人物", "空镜", "建立镜头")):
            return False
        if label and not named and not _SCENE_WORDS.search(text):
            return False
        return True
    if kind == "prop":
        if _looks_like_portrait(text) and not named:
            return False
        if _SCENE_WORDS.search(text) and "特写" not in text and not named:
            return False
        if label and not named and not _PROP_STILL.search(text) and "道具" not in text:
            return False
        return True
    if _looks_like_prop_still(text) and not named:
        return False
    if _SCENE_WORDS.search(text) and not any(k in text for k in ("无人物", "空镜")) and not named:
        return False
    if label and not named and not _looks_like_portrait(text):
        return False
    return True


def _picture_kind_key(pic: PictureBinding, analysis: Analysis | None) -> str:
    if pic.kind != "character" or not analysis:
        return pic.kind
    char = next((c for c in analysis.characters if c.name == pic.name or c.id == pic.ref_id), None)
    if char and clip_lib.is_extra_character(char):
        return "extra"
    return pic.kind


def fix_analysis_image_prompts(analysis: Analysis) -> None:
    chars = list(analysis.characters or [])
    for char in chars:
        prompt = (char.image_prompt or "").strip()
        if not prompt or not char.name or char.name in prompt:
            continue
        for other in chars:
            if other is char or not other.name:
                continue
            other_prompt = (other.image_prompt or "").strip()
            if other.name in prompt and char.name in other_prompt:
                char.image_prompt, other.image_prompt = other.image_prompt, char.image_prompt
                break
    for scene in analysis.scenes or []:
        prompt = (scene.image_prompt or "").strip()
        if not prompt or not scene.name or scene.name in prompt:
            continue
        if _looks_like_portrait(prompt) and "无人物" not in prompt and "空镜" not in prompt:
            scene.image_prompt = ""
    for prop in analysis.props or []:
        prompt = (prop.image_prompt or "").strip()
        if not prompt or not prop.name or prop.name in prompt:
            continue
        if _SCENE_WORDS.search(prompt) and "特写" not in prompt:
            prop.image_prompt = ""


def canonical_picture_prompt(pic: PictureBinding, analysis: Analysis | None) -> str:
    source, details = _source_prompt(analysis, pic)
    kind_key = _picture_kind_key(pic, analysis)
    if source and _prompt_fits_kind(pic.kind, pic.name, source):
        return _ensure_subject(pic.kind, pic.name, source)
    stored = (pic.prompt or "").strip()
    if stored and _prompt_fits_kind(pic.kind, pic.name, stored):
        return _ensure_subject(pic.kind, pic.name, stored)
    return _ensure_subject(
        pic.kind,
        pic.name,
        _fallback_prompt(kind_key, pic.name, details or pic.name),
    )


def _ensure_subject(kind: str, name: str, prompt: str) -> str:
    text = (prompt or "").strip()
    label = (name or "").strip()
    if not label:
        return text
    if label in text:
        return text
    if kind == "scene":
        return f"{label}，建立镜头空镜无人物。{text}".strip()
    if kind == "prop":
        return f"道具「{label}」静物特写。{text}".strip()
    return f"{label}。{text}".strip()


def _source_prompt(analysis: Analysis | None, pic: PictureBinding) -> tuple[str, str]:
    if not analysis:
        return "", pic.name
    if pic.kind == "scene":
        scene = next((s for s in analysis.scenes if s.name == pic.name or s.id == pic.ref_id), None)
        if scene:
            lock = "、".join(scene.lock_elements) if scene.lock_elements else scene.atmosphere
            details = "、".join(part for part in [scene.location, scene.time, lock] if part)
            return scene.image_prompt or "", details or pic.name
    elif pic.kind == "prop":
        prop = next((item for item in analysis.props if item.name == pic.name or item.id == pic.ref_id), None)
        if prop:
            details = "、".join(part for part in [prop.description, prop.lock_note] if part)
            return prop.image_prompt or "", details or pic.name
    else:
        char = next((c for c in analysis.characters if c.name == pic.name or c.id == pic.ref_id), None)
        if char:
            appearance = "、".join(part for part in [char.appearance, char.wardrobe, char.signature_props] if part)
            return char.image_prompt or "", appearance or pic.name
        return "", pic.name
    return "", pic.name


def bind_picture_prompt(
    pic: PictureBinding,
    analysis: Analysis | None,
    *,
    invalidate_ready: bool = False,
    force: bool = False,
    respect_reused: bool = True,
) -> bool:
    if respect_reused and pic.reused and pic.status == "ready" and not force:
        if _prompt_fits_kind(pic.kind, pic.name, pic.prompt or ""):
            return False
    current = (pic.prompt or "").strip()
    mismatched = not _prompt_fits_kind(pic.kind, pic.name, current)
    resolved = canonical_picture_prompt(pic, analysis)
    if not mismatched and not force and resolved == current:
        return False
    changed = resolved != current
    pic.prompt = resolved
    if invalidate_ready and pic.status == "ready" and changed:
        pic.status = "failed"
        pic.error = "提示词已更新，与当前图可能不一致，请点「按此提示词重出」。"
        pic.reused = False
        changed = True
    elif mismatched and pic.status == "failed" and not pic.error:
        pic.error = "提示词和画面类型对不上，已改回正确提示词，请点「按此提示词重出」。"
        changed = True
    return changed


def repair_picture_prompts(state: ProjectState) -> bool:
    if state.status in {"analyzing", "storyboard", "imaging", "prompting"} and is_project_active(state.id):
        return False
    if any(pic.status == "generating" for pic in state.pictures or []):
        return False
    changed = False
    for pic in state.pictures or []:
        if bind_picture_prompt(
            pic,
            state.analysis,
            invalidate_ready=not pic.reused,
            respect_reused=True,
        ):
            changed = True
    return changed


def build_picture_plan(analysis: Analysis) -> list[PictureBinding]:
    fix_analysis_image_prompts(analysis)
    pictures: list[PictureBinding] = []
    index = 1

    for char in analysis.characters:
        appearance = "、".join(
            p for p in [char.appearance, char.wardrobe, char.signature_props] if p
        )
        extra = clip_lib.is_extra_character(char)
        pictures.append(
            PictureBinding(
                picture_index=index,
                kind="character",
                ref_id=char.id or f"c{index}",
                name=char.name,
                lock_text=(
                    (
                        f"{char.name} 群演锁定（脸型、体态、服装）。当作角色代入，"
                        "禁止复制成镜像分身，禁止升格成新主演。"
                    )
                    if extra
                    else (
                        f"{char.name} 身份锁定（脸、发型、体态、服装）。"
                        + (f"别称：{'、'.join(char.aliases[:6])}，都是同一个人，禁止换演员。" if char.aliases else "")
                        + ("戴口罩仍是此人，摘口罩必须是这张脸。" if "口罩" in ((char.wardrobe or "") + (char.appearance or "")) else "")
                        + "不要继承这张图的背景和光线。"
                    )
                ),
                filename=f"picture_{index}_{char.id or 'char'}.png",
                prompt=char.image_prompt
                or _fallback_prompt("extra" if extra else "character", char.name, appearance),
            )
        )
        index += 1

    for scene in analysis.scenes:
        lock = "、".join(scene.lock_elements) if scene.lock_elements else scene.atmosphere
        details = "、".join(p for p in [scene.location, scene.time, lock] if p)
        pictures.append(
            PictureBinding(
                picture_index=index,
                kind="scene",
                ref_id=scene.id or f"s{index}",
                name=scene.name,
                lock_text=f"{scene.name} 场景锁定（空间结构、天气、光线）。不要出现清晰主演面孔。",
                filename=f"picture_{index}_{scene.id or 'scene'}.png",
                prompt=scene.image_prompt or _fallback_prompt("scene", scene.name, details),
            )
        )
        index += 1

    for i, prop in enumerate(analysis.props, start=1):
        if prop.needs_reference is False:
            continue
        if clip_lib.is_body_feature_prop(prop.name):
            continue
        prop_id = prop.id or f"p{i}"
        details = "、".join(p for p in [prop.description, prop.lock_note] if p)
        pictures.append(
            PictureBinding(
                picture_index=index,
                kind="prop",
                ref_id=prop_id,
                name=prop.name,
                lock_text=f"{prop.name} 道具锁定（外形、材质）。不要从这张图抄人物。",
                filename=f"picture_{index}_{prop_id}.png",
                prompt=prop.image_prompt or _fallback_prompt("prop", prop.name, details),
            )
        )
        index += 1
    for pic in pictures:
        pic.prompt = canonical_picture_prompt(pic, analysis)
    return pictures


def _apply_series_identity(analysis: Analysis, series) -> None:
    if not series:
        return
    clip_lib.enrich_character_aliases(analysis)
    clip_lib.enrich_prop_aliases(analysis)
    for char in analysis.characters:
        asset = storage.find_asset(series, "character", char.name, char.aliases)
        if not asset:
            continue
        char.name = asset.name
        merged = list(char.aliases or [])
        for item in getattr(asset, "aliases", None) or []:
            if item and item not in merged and item != char.name:
                merged.append(item)
        char.aliases = merged
        if asset.appearance:
            char.appearance = asset.appearance
        if asset.wardrobe:
            char.wardrobe = asset.wardrobe
        if asset.prompt and _prompt_fits_kind("character", char.name, asset.prompt):
            char.image_prompt = asset.prompt
    for scene in analysis.scenes:
        asset = storage.find_asset(series, "scene", scene.name)
        if not asset:
            continue
        scene.name = asset.name
        if asset.prompt and _prompt_fits_kind("scene", scene.name, asset.prompt):
            scene.image_prompt = asset.prompt
    for prop in analysis.props:
        asset = storage.find_asset(series, "prop", prop.name, prop.aliases)
        if not asset:
            continue
        prop.name = asset.name
        merged = list(prop.aliases or [])
        for item in getattr(asset, "aliases", None) or []:
            if item and item not in merged and item != prop.name:
                merged.append(item)
        prop.aliases = merged
        if asset.description:
            prop.description = asset.description
        if asset.prompt and _prompt_fits_kind("prop", prop.name, asset.prompt):
            prop.image_prompt = asset.prompt


def _reuse_or_plan(state: ProjectState, series) -> tuple[int, int]:
    reused = 0
    for pic in state.pictures:
        if not series:
            continue
        aliases = []
        if state.analysis and pic.kind == "character":
            char = next((c for c in state.analysis.characters if c.name == pic.name), None)
            if char:
                aliases = list(char.aliases or [])
        elif state.analysis and pic.kind == "prop":
            prop = next((p for p in state.analysis.props if p.name == pic.name), None)
            if prop:
                aliases = list(prop.aliases or [])
        asset = storage.find_asset(series, pic.kind, pic.name, aliases)
        if not asset or not asset.filename:
            continue
        dest = storage.image_dir(state.id) / pic.filename
        if storage.copy_asset_to_project(series.id, asset, dest):
            pic.status = "ready"
            pic.reused = True
            pic.lock_text = asset.lock_text or pic.lock_text
            if asset.prompt and _prompt_fits_kind(pic.kind, pic.name, asset.prompt):
                pic.prompt = _ensure_subject(pic.kind, pic.name, asset.prompt)
            else:
                pic.prompt = canonical_picture_prompt(pic, state.analysis)
                pic.error = "沿用本剧旧图，但缺少匹配的出图提示词，建议点「保存并重出」。"
            reused += 1
    return reused, len(state.pictures) - reused


def _remember_assets(state: ProjectState, series, *, force: bool = False) -> None:
    if not series:
        return
    analysis = state.analysis
    for pic in state.pictures:
        if pic.status != "ready":
            continue
        if not force and not _prompt_fits_kind(pic.kind, pic.name, pic.prompt):
            continue
        source = storage.image_dir(state.id) / pic.filename
        appearance = wardrobe = description = ""
        aliases: list[str] = []
        if analysis and pic.kind == "character":
            char = next((c for c in analysis.characters if c.name == pic.name), None)
            if char:
                appearance, wardrobe = char.appearance, char.wardrobe
                aliases = list(char.aliases or [])
        elif analysis and pic.kind == "prop":
            prop = next((p for p in analysis.props if p.name == pic.name), None)
            if prop:
                description = prop.description
                aliases = list(prop.aliases or [])
        storage.upsert_series_asset(
            series,
            kind=pic.kind,
            name=pic.name,
            lock_text=pic.lock_text,
            prompt=pic.prompt,
            source_image=source if source.exists() else None,
            appearance=appearance,
            wardrobe=wardrobe,
            description=description,
            aliases=aliases,
        )


def _picture_name_keys(state: ProjectState, pic: PictureBinding) -> set[str]:
    keys = {storage.normalize_name(pic.name)}
    if state.analysis and pic.kind == "character":
        char = next((c for c in state.analysis.characters if c.name == pic.name), None)
        if char:
            keys.update(storage.normalize_name(item) for item in (char.aliases or []))
    elif state.analysis and pic.kind == "prop":
        prop = next((p for p in state.analysis.props if p.name == pic.name), None)
        if prop:
            keys.update(storage.normalize_name(item) for item in (prop.aliases or []))
    keys.discard("")
    return keys


def _pictures_same_entity(left: ProjectState, a: PictureBinding, right: ProjectState, b: PictureBinding) -> bool:
    if a.kind != b.kind:
        return False
    return bool(_picture_name_keys(left, a) & _picture_name_keys(right, b))


def propagate_picture_across_series(
    origin: ProjectState,
    pic: PictureBinding,
    source: Path,
) -> int:
    """把本集刚换的角色/场景/道具图，同步到本剧其它已有集。"""
    if not origin.series_id or not source.exists():
        return 0
    try:
        series = storage.load_series(origin.series_id)
    except FileNotFoundError:
        return 0
    synced = 0
    for ep in series.episodes:
        if ep.project_id == origin.id:
            continue
        try:
            other = storage.load_state(ep.project_id)
        except FileNotFoundError:
            continue
        changed = False
        for other_pic in other.pictures or []:
            if not _pictures_same_entity(origin, pic, other, other_pic):
                continue
            dest = storage.image_dir(other.id) / (other_pic.filename or f"picture_{other_pic.picture_index}.png")
            dest.parent.mkdir(parents=True, exist_ok=True)
            if source.resolve() != dest.resolve():
                shutil.copy2(source, dest)
            other_pic.filename = dest.name
            other_pic.status = "ready"
            other_pic.error = ""
            other_pic.reused = True
            other_pic.image_stamp = str(dest.stat().st_mtime_ns)
            if pic.seedance_asset_id:
                other_pic.seedance_asset_id = pic.seedance_asset_id
            if pic.seedance_asset_hash:
                other_pic.seedance_asset_hash = pic.seedance_asset_hash
            skip_voice = other_pic.voice_source == "upload" and pic.voice_source != "upload"
            if not skip_voice and pic.voice_filename and pic.voice_filename != other_pic.voice_filename:
                voice_src = storage.image_dir(origin.id) / pic.voice_filename
                if voice_src.exists():
                    voice_dest = storage.image_dir(other.id) / f"voice_{other_pic.picture_index}.mp3"
                    shutil.copy2(voice_src, voice_dest)
                    other_pic.voice_filename = voice_dest.name
                    other_pic.voice_source = pic.voice_source or other_pic.voice_source
                    other_pic.voice_from_clip = pic.voice_from_clip or other_pic.voice_from_clip
            elif not skip_voice and pic.voice_filename:
                other_pic.voice_filename = pic.voice_filename
                other_pic.voice_source = pic.voice_source or other_pic.voice_source
                other_pic.voice_from_clip = pic.voice_from_clip or other_pic.voice_from_clip
            if not skip_voice and pic.seedance_audio_asset_id:
                other_pic.seedance_audio_asset_id = pic.seedance_audio_asset_id
            if not skip_voice and pic.seedance_audio_asset_hash:
                other_pic.seedance_audio_asset_hash = pic.seedance_audio_asset_hash
            changed = True
            synced += 1
        if changed:
            storage.save_state(other)
    return synced


async def run_pipeline_batch(project_ids: list[str], publish: EmitFn) -> None:
    mark_batch_active(project_ids)
    try:
        total = len(project_ids)
        for i, project_id in enumerate(project_ids, start=1):
            try:
                state = storage.load_state(project_id)
            except FileNotFoundError:
                continue
            _emit(state, "queued", f"全本拆集 {i}/{total}：开始生成 {state.episode_title}", 2)
            storage.save_state(state)
            await publish(state)
            await run_pipeline(state, publish)
    finally:
        mark_batch_inactive(project_ids)


async def run_pipeline(state: ProjectState, publish: EmitFn) -> None:
    mark_project_active(state.id)
    try:
        await _run_pipeline_body(state, publish)
    finally:
        mark_project_inactive(state.id)


async def _run_pipeline_body(state: ProjectState, publish: EmitFn) -> None:
    try:
        series = None
        if state.series_id:
            try:
                series = storage.load_series(state.series_id)
                state.series_name = series.name
            except FileNotFoundError:
                series = None

        _emit(state, "analyzing", "正在用 DeepSeek 拆解剧本与角色场景…", 8)
        storage.save_state(state)
        await publish(state)

        analysis = await llm.analyze_script(
            state.script,
            state.duration_seconds,
            state.style,
            state.extra_notes,
            series_bible=storage.bible_text(series),
            aspect_ratio=state.video_ratio or "9:16",
            duration_auto=state.duration_auto,
        )
        _apply_series_identity(analysis, series)
        state.analysis = analysis
        state.duration_seconds = analysis.duration_seconds
        state.duration_reason = analysis.duration_reason
        if not state.episode_title:
            state.episode_title = storage.episode_cn_label(state.episode_index or 1)
        state.pictures = build_picture_plan(analysis)
        reused, fresh = _reuse_or_plan(state, series)
        _emit(
            state,
            "storyboard",
            f"分镜已生成。整集 {analysis.duration_seconds} 秒 · {len(analysis.clips)} 段。"
            + (f" {analysis.duration_reason}" if analysis.duration_reason else "")
            + f" 参考图 {len(state.pictures)} 张（沿用 {reused} / 新增 {fresh}）。",
            28,
        )
        storage.save_state(state)
        await publish(state)

        pending = [p for p in state.pictures if not p.reused]
        if state.skip_images:
            for pic in pending:
                pic.status = "skipped"
            _emit(state, "imaging", f"已跳过新图。沿用 {reused} 张本剧参考图。", 55)
            storage.save_state(state)
            await publish(state)
        else:
            need_gen = [p for p in pending if p.status != "ready"]
            if not need_gen:
                _emit(state, "imaging", "本集角色与道具均已有参考图，无需再出图。", 82)
                storage.save_state(state)
                await publish(state)
            else:
                health = await images.health()
                if health.get("status") != "ok":
                    for pic in need_gen:
                        pic.status = "failed"
                        pic.error = health.get("error") or "本地出图服务不可用"
                    _emit(
                        state,
                        "imaging",
                        "出图服务未启动，已跳过新增参考图。已有角色/道具仍沿用本剧设定。",
                        55,
                    )
                    storage.save_state(state)
                    await publish(state)
                else:
                    total = max(len(need_gen), 1)
                    for i, pic in enumerate(need_gen):
                        _mark_pic_generating(pic)
                        pct = 30 + int(50 * i / total)
                        _emit(
                            state,
                            "imaging",
                            f"新增参考图 {i + 1}/{len(need_gen)}：{pic.name}",
                            pct,
                        )
                        storage.save_state(state)
                        await publish(state)
                        dest = storage.image_dir(state.id) / pic.filename
                        pic.prompt = canonical_picture_prompt(pic, state.analysis)
                        try:
                            await _generate_picture_file(pic, dest)
                            pic.status = "ready"
                            pic.generating_since = 0
                        except Exception as exc:
                            pic.status = "failed"
                            pic.error = str(exc)
                            pic.generating_since = 0
                        storage.save_state(state)
                        await publish(state)
                    failed = [p.name for p in need_gen if p.status == "failed"]
                    msg = "参考图阶段完成。"
                    if failed:
                        msg = f"参考图阶段结束。失败 {len(failed)} 张：{'、'.join(failed[:6])}，可在「参考图」重出。"
                    _emit(state, "imaging", msg, 82)
                    storage.save_state(state)
                    await publish(state)

        _remember_assets(state, series)
        if series:
            storage.append_episode(series, state.id, state.episode_title or analysis.title)
            series = storage.load_series(series.id)
            state.series_id = series.id
            state.series_name = series.name

        _emit(state, "prompting", "正在撰写 H3 / Seedance 分段提示词…", 88)
        storage.save_state(state)
        await publish(state)

        try:
            produced, joined = await llm.write_clip_prompts(
                analysis,
                state.pictures,
                _bindings_text(state.pictures),
                high_quality=state.high_quality,
            )
            state.clips = produced
            state.video_prompt = joined
            state.video_seedance_prompt = clip_lib.join_episode_seedance_prompt(produced)
        except Exception as exc:
            state.clips = clip_lib.build_video_clips(analysis, state.pictures)
            _sync_video_prompt_fields(state)
            _emit(state, "prompting", f"模型润色失败，已使用分镜装配稿：{exc}", 92)

        if state.auto_generate_video and _pictures_ready_for_video(state):
            await generate_videos(state, publish)
        else:
            block = _picture_block_reason(state)
            if block:
                _emit(state, "done", block, 100)
            else:
                _emit(
                    state,
                    "done",
                    f"完成。已切成 {len(state.clips)} 段 H3 提示词，可在「成片」里一键出片。",
                    100,
                )
            storage.save_state(state)
            await publish(state)
    except Exception as exc:
        state.error = str(exc)
        _emit(state, "error", f"失败：{exc}", state.progress, traceback=traceback.format_exc())
        storage.save_state(state)
        await publish(state)


async def regenerate_picture(
    state: ProjectState,
    picture_index: int,
    publish: EmitFn,
    prompt: str | None = None,
) -> ProjectState:
    mark_project_active(state.id)
    try:
        return await _regenerate_picture_body(state, picture_index, publish, prompt=prompt)
    finally:
        mark_project_inactive(state.id)


async def _regenerate_picture_body(
    state: ProjectState,
    picture_index: int,
    publish: EmitFn,
    prompt: str | None = None,
) -> ProjectState:
    pic = next((p for p in state.pictures if p.picture_index == picture_index), None)
    if not pic:
        raise KeyError(f"没有 Picture {picture_index}")
    if prompt and prompt.strip():
        pic.prompt = _ensure_subject(pic.kind, pic.name, prompt.strip())
        if not _prompt_fits_kind(pic.kind, pic.name, pic.prompt):
            pic.prompt = canonical_picture_prompt(pic, state.analysis)
    else:
        pic.prompt = canonical_picture_prompt(pic, state.analysis)
    _mark_pic_generating(pic)
    _emit(state, "retry", f"正在按新提示词重出 <Picture {picture_index}> {pic.name}", min(state.progress, 90))
    storage.save_state(state)
    await publish(state)
    dest = storage.image_dir(state.id) / pic.filename
    try:
        await _generate_picture_file(pic, dest)
        pic.status = "ready"
        pic.generating_since = 0
        pic.reused = False
        if state.series_id:
            try:
                series = storage.load_series(state.series_id)
                clone = state.model_copy(deep=True)
                clone.pictures = [pic]
                _remember_assets(clone, series)
            except FileNotFoundError:
                pass
        synced = propagate_picture_across_series(state, pic, dest)
        _refresh_clip_bindings(state)
        extra_sync = f"已同步到本剧另外 {synced} 处同名参考图。" if synced else ""
        if _pictures_ready_for_video(state):
            _emit(state, "retry", f"<Picture {picture_index}> 已更新。{extra_sync}参考图齐了，可在「成片」里出片。", state.progress)
        else:
            left = [p.name for p in state.pictures if p.status == "failed"]
            extra = f"还有失败参考图：{'、'.join(left)}，改提示词后重出即可。" if left else "成功后再出片。"
            _emit(state, "retry", f"<Picture {picture_index}> 已更新。{extra_sync}{extra}", state.progress)
    except Exception as exc:
        pic.status = "failed"
        pic.error = str(exc)
        pic.generating_since = 0
        _emit(state, "retry", f"<Picture {picture_index}> 仍然失败：{exc}", state.progress)
    _maybe_finish_imaging(state)
    storage.save_state(state)
    await publish(state)
    return state


async def refetch_picture(
    state: ProjectState,
    picture_index: int,
    publish: EmitFn,
    *,
    generate_if_missing: bool = True,
) -> ProjectState:
    mark_project_active(state.id)
    try:
        return await _refetch_picture_body(
            state, picture_index, publish, generate_if_missing=generate_if_missing
        )
    finally:
        mark_project_inactive(state.id)


async def _refetch_picture_body(
    state: ProjectState,
    picture_index: int,
    publish: EmitFn,
    *,
    generate_if_missing: bool = True,
) -> ProjectState:
    pic = next((p for p in state.pictures if p.picture_index == picture_index), None)
    if not pic:
        raise KeyError(f"没有 Picture {picture_index}")
    if not pic.prompt.strip():
        pic.prompt = canonical_picture_prompt(pic, state.analysis)
    _mark_pic_generating(pic, reset_job=False)
    _emit(
        state,
        "retry",
        f"正在从平台任务和 ChatGPT 对话里找回参考图 <Picture {picture_index}> {pic.name}",
        min(state.progress, 90),
    )
    storage.save_state(state)
    await publish(state)
    dest = storage.image_dir(state.id) / pic.filename
    try:
        result = await asyncio.wait_for(
            images.sync_image(
                pic.prompt,
                dest,
                job_id=pic.image_job_id,
                source_path=pic.source_path,
                source_url=pic.source_url,
                generated_prompt=pic.generated_prompt,
                allow_generate=generate_if_missing,
                poll_seconds=IMAGE_GENERATION_TIMEOUT,
            ),
            timeout=IMAGE_GENERATION_TIMEOUT + 15,
        )
        _apply_image_result(pic, result)
        pic.status = "ready"
        pic.generating_since = 0
        pic.reused = False
        if result.sync_mode == "generated":
            msg = f"<Picture {picture_index}> 未找到历史图，已重新生成。"
        elif result.sync_mode == "conversation":
            msg = f"<Picture {picture_index}> 已从 ChatGPT 对话里找回这张图，没有重新出图。"
        elif result.sync_mode in {"match", "source", "job"}:
            msg = f"<Picture {picture_index}> 已从平台重新拉取，没有重新出图。"
        else:
            msg = f"<Picture {picture_index}> 已同步参考图。"
        if pic.verify_warning:
            msg += f" {pic.verify_warning}"
        _emit(state, "retry", msg, state.progress)
    except Exception as exc:
        pic.status = "failed"
        pic.error = str(exc)
        pic.generating_since = 0
        _emit(state, "retry", f"<Picture {picture_index}> 同步失败：{exc}", state.progress)
    _maybe_finish_imaging(state)
    storage.save_state(state)
    await publish(state)
    return state


def _save_uploaded_image(data: bytes, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:
        raise ValueError(f"不是可用的图片文件：{exc}") from exc
    if image.mode not in {"RGB", "RGBA"}:
        image = image.convert("RGBA" if "A" in image.mode else "RGB")
    suffix = dest.suffix.lower() or ".png"
    if suffix in {".jpg", ".jpeg"}:
        if image.mode == "RGBA":
            image = image.convert("RGB")
        image.save(dest, "JPEG", quality=92)
    else:
        image.save(dest, "PNG")


async def replace_picture(
    state: ProjectState,
    picture_index: int,
    data: bytes,
    filename: str,
    publish: EmitFn,
) -> ProjectState:
    pic = next((p for p in state.pictures if p.picture_index == picture_index), None)
    if not pic:
        raise KeyError(f"没有 Picture {picture_index}")
    if not data:
        raise ValueError("没有收到图片")
    if len(data) > 12 * 1024 * 1024:
        raise ValueError("图片太大，请小于 12MB")
    dest = storage.image_dir(state.id) / (pic.filename or f"picture_{picture_index}.png")
    if not dest.suffix:
        dest = dest.with_suffix(".png")
        pic.filename = dest.name
    _save_uploaded_image(data, dest)
    pic.filename = dest.name
    pic.status = "ready"
    pic.error = ""
    pic.reused = False
    if state.series_id:
        try:
            series = storage.load_series(state.series_id)
            clone = state.model_copy(deep=True)
            clone.pictures = [pic]
            _remember_assets(clone, series, force=True)
        except FileNotFoundError:
            pass
    pic.image_stamp = str(dest.stat().st_mtime_ns)
    synced = propagate_picture_across_series(state, pic, dest)
    _refresh_clip_bindings(state, overwrite_prompt=False)
    left = [p.name for p in state.pictures if p.status == "failed"]
    extra = f"还有失败参考图：{'、'.join(left)}。" if left else "参考图齐了，可在「成片」里出片。"
    if synced:
        extra = f"已同步到本剧另外 {synced} 处同名参考图。" + extra
    source = Path(filename).name if filename else "本地图"
    _emit(
        state,
        "retry",
        f"<Picture {picture_index}> {pic.name} 已换成 {source}。{extra}",
        min(max(state.progress, 55), 90),
    )
    _maybe_finish_imaging(state)
    storage.save_state(state)
    await publish(state)
    return state


def _save_voice_sample(data: bytes, filename: str, dest: Path) -> Path:
    from . import edit

    dest.parent.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename or "voice.mp3").suffix.lower()
    if suffix not in {".mp3", ".wav", ".m4a", ".aac", ".ogg"}:
        suffix = ".mp3"
    raw = dest.with_name(dest.stem + "_src" + suffix)
    raw.write_bytes(data)
    if len(data) < 800:
        raise ValueError("音频文件太小")
    if len(data) > 15 * 1024 * 1024:
        raise ValueError("参考音频请小于 15MB，时长 2–15 秒")
    cmd = [
        edit.ffmpeg_bin(),
        "-y",
        "-i",
        str(raw),
        "-t",
        "12",
        "-ar",
        "24000",
        "-ac",
        "1",
        "-b:a",
        "96k",
        str(dest),
    ]
    try:
        edit._run_ffmpeg(cmd, timeout=60)
    except Exception as exc:
        raise ValueError(f"音频无法读取，请上传 MP3/WAV：{exc}") from exc
    duration = edit.probe_duration(dest)
    if duration < 1.6:
        raise ValueError("参考音频太短，请上传该角色说话 2–15 秒的样例")
    return dest


def _voice_is_user_locked(pic: PictureBinding) -> bool:
    if pic.voice_source == "extracted":
        return False
    if pic.voice_source == "upload":
        return True
    return bool(pic.voice_filename)


def _speaking_character_pictures(state: ProjectState, clip) -> list[PictureBinding]:
    analysis = state.analysis
    pictures = state.pictures or []
    shots = clip_lib.shots_for_clip(analysis, clip) if analysis else []
    names: list[str] = []
    for shot in shots:
        line = (shot.dialogue_line or "").replace("\\", "").strip()
        speaker = (shot.dialogue_speaker or "").strip()
        if not line or edit._NARRATION_RE.search(speaker):
            continue
        if analysis:
            speaker = clip_lib._canonicalize_cast(speaker, analysis, pictures)
        key = storage.normalize_name(speaker)
        if key and key not in names:
            names.append(key)
    out: list[PictureBinding] = []
    for key in names:
        pic = next(
            (
                p
                for p in pictures
                if p.kind == "character" and key in _picture_name_keys(state, p)
            ),
            None,
        )
        if pic and pic not in out:
            out.append(pic)
    return out


def _clip_speech_window(state: ProjectState, clip, pic: PictureBinding, duration: float) -> tuple[float, float]:
    analysis = state.analysis
    if not analysis:
        return 0.35, min(12.0, max(2.2, duration - 0.4))
    shots = clip_lib.shots_for_clip(analysis, clip)
    starts: list[float] = []
    ends: list[float] = []
    pic_keys = _picture_name_keys(state, pic)
    for shot in shots:
        line = (shot.dialogue_line or "").replace("\\", "").strip()
        speaker = clip_lib._canonicalize_cast((shot.dialogue_speaker or "").strip(), analysis, state.pictures)
        if not line or storage.normalize_name(speaker) not in pic_keys:
            continue
        starts.append(max(0.0, float(shot.start) - float(clip.start)))
        ends.append(max(0.4, float(shot.end) - float(clip.start)))
    if not starts:
        return 0.35, min(12.0, max(2.2, duration - 0.4))
    ss = max(0.0, min(starts) - 0.15)
    ee = min(duration, max(ends) + 0.25)
    dur = min(12.0, max(2.2, ee - ss))
    if ss + dur > duration:
        ss = max(0.0, duration - dur)
    return ss, dur


def _lock_voices_from_succeeded_clip(state: ProjectState, clip, video_path: Path) -> list[str]:
    from . import ark_assets

    if not video_path.exists() or not edit.video_has_audio(video_path):
        return []
    speakers = _speaking_character_pictures(state, clip)
    if len(speakers) != 1:
        return []
    pic = speakers[0]
    if _voice_is_user_locked(pic):
        return []
    if pic.voice_source == "extracted" and pic.voice_from_clip not in {0, int(clip.index)}:
        return []
    duration = edit.probe_duration(video_path) or float(clip.duration_seconds or 8)
    start, hold = _clip_speech_window(state, clip, pic, duration)
    dest = storage.image_dir(state.id) / f"voice_{pic.picture_index}.mp3"
    try:
        edit.extract_reference_audio(video_path, dest, start=start, duration=hold)
    except Exception:
        return []
    mean = edit.probe_mean_volume(dest)
    length = edit.probe_duration(dest)
    if length < 1.6 or (mean is not None and mean < -38):
        dest.unlink(missing_ok=True)
        return []
    pic.voice_filename = dest.name
    pic.voice_source = "extracted"
    pic.voice_from_clip = int(clip.index)
    pic.seedance_audio_asset_id = ""
    pic.seedance_audio_asset_hash = ark_assets.file_digest(dest)
    image_path = storage.image_dir(state.id) / pic.filename if pic.filename else dest
    if image_path.exists():
        propagate_picture_across_series(state, pic, image_path)
    return [pic.name]


def _harvest_voices_from_existing_clips(state: ProjectState) -> list[str]:
    names: list[str] = []
    vdir = storage.video_dir(state.id)
    for clip in sorted(state.clips or [], key=lambda c: c.index):
        if clip.video_status != "succeeded" or not clip.video_filename:
            continue
        path = vdir / clip.video_filename
        for name in _lock_voices_from_succeeded_clip(state, clip, path):
            if name not in names:
                names.append(name)
    return names


def _resolve_voice_ref(pic: PictureBinding, images_dir: Path):
    audio_uri = seedance.normalize_asset_uri(getattr(pic, "seedance_audio_asset_id", "") or "")
    if seedance.is_live_asset_uri(audio_uri):
        return audio_uri
    if pic.voice_filename:
        vpath = images_dir / pic.voice_filename
        if vpath.exists():
            return vpath
    return None


def _voice_refs_for_clip(state: ProjectState, clip, images_dir: Path):
    speakers = _speaking_character_pictures(state, clip)
    audios: list = []
    used: list[PictureBinding] = []
    for pic in speakers:
        ref = _resolve_voice_ref(pic, images_dir)
        if not ref:
            continue
        audios.append(ref)
        used.append(pic)
        if len(audios) >= 3:
            break
    return audios, used


async def replace_character_voice(
    state: ProjectState,
    picture_index: int,
    data: bytes,
    filename: str,
    publish: EmitFn,
) -> ProjectState:
    from . import ark_assets

    pic = next((p for p in state.pictures if p.picture_index == picture_index), None)
    if not pic:
        raise KeyError(f"没有 Picture {picture_index}")
    if pic.kind != "character":
        raise ValueError("只有角色可以绑定声线参考音频")
    if not data:
        raise ValueError("没有收到音频")
    dest = storage.image_dir(state.id) / f"voice_{pic.picture_index}.mp3"
    _save_voice_sample(data, filename, dest)
    pic.voice_filename = dest.name
    pic.voice_source = "upload"
    pic.voice_from_clip = 0
    pic.seedance_audio_asset_id = ""
    pic.seedance_audio_asset_hash = ark_assets.file_digest(dest)
    if seedance.assets_ready():
        uri = await seedance.ingest_character_audio(
            dest, owner=state.series_id or state.id, character=pic.name
        )
        pic.seedance_audio_asset_id = uri
    image_path = storage.image_dir(state.id) / pic.filename if pic.filename else dest
    if image_path.exists():
        propagate_picture_across_series(state, pic, image_path)
    _refresh_clip_bindings(state, overwrite_prompt=False)
    bound = pic.seedance_audio_asset_id or "已保存，出片时入库"
    _emit(state, "retry", f"{pic.name} 声线参考音频已绑定（{bound}）。", min(max(state.progress, 55), 90))
    storage.save_state(state)
    await publish(state)
    return state


def bind_seedance_asset(
    state: ProjectState,
    picture_index: int,
    asset_id: str,
) -> ProjectState:
    pic = next((p for p in state.pictures if p.picture_index == picture_index), None)
    if not pic:
        raise KeyError(f"没有 Picture {picture_index}")
    uri = seedance.normalize_asset_uri(asset_id)
    if asset_id.strip() and not uri:
        raise ValueError("素材 ID 格式不对。请粘贴控制台里的 asset://asset-xxxx。")
    pic.seedance_asset_id = uri
    dest = storage.image_dir(state.id) / pic.filename if pic.filename else None
    if dest and dest.exists():
        propagate_picture_across_series(state, pic, dest)
    else:
        if state.series_id:
            try:
                series = storage.load_series(state.series_id)
                for ep in series.episodes:
                    if ep.project_id == state.id:
                        continue
                    try:
                        other = storage.load_state(ep.project_id)
                    except FileNotFoundError:
                        continue
                    changed = False
                    for other_pic in other.pictures or []:
                        if _pictures_same_entity(state, pic, other, other_pic):
                            other_pic.seedance_asset_id = uri
                            changed = True
                    if changed:
                        storage.save_state(other)
            except FileNotFoundError:
                pass
    _emit(
        state,
        "retry",
        f"<Picture {picture_index}> {pic.name} 已绑定虚拟人像 {uri or '（已清空）'}。",
        min(max(state.progress, 55), 90),
    )
    storage.save_state(state)
    return state


def _ready_pictures(state: ProjectState) -> list[PictureBinding]:
    return [p for p in state.pictures if p.status == "ready"]


def _failed_pictures(state: ProjectState) -> list[PictureBinding]:
    return [p for p in state.pictures if p.status == "failed"]


def _pictures_ready_for_video(state: ProjectState) -> bool:
    return bool(_ready_pictures(state)) and not _failed_pictures(state)


def _picture_block_reason(state: ProjectState) -> str:
    failed = _failed_pictures(state)
    ready = _ready_pictures(state)
    if failed:
        names = "、".join(p.name for p in failed)
        return f"还有失败参考图（{names}）。请在「参考图」里改提示词重出，或点「换本地图」上传一张，成功后再出片。"
    if not ready:
        return "还没有成功的参考图。请先出图或重出失败图，成功后再出片，不会改走纯文生视频。"
    return ""


def _maybe_finish_imaging(state: ProjectState) -> bool:
    if state.status not in {"imaging", "prompting", "storyboard"}:
        return False
    pics = state.pictures or []
    if any(p.status == "generating" for p in pics):
        return False
    if any(p.status == "pending" and not p.reused for p in pics):
        return False
    if not state.analysis:
        return False
    if not state.clips:
        state.clips = clip_lib.build_video_clips(state.analysis, pics)
    else:
        _refresh_clip_bindings(state, overwrite_prompt=False)
    _sync_video_prompt_fields(state)
    failed = [p.name for p in pics if p.status == "failed"]
    state.status = "done"
    state.progress = max(state.progress, 82)
    if failed:
        state.message = f"参考图阶段完成。失败 {len(failed)} 张，可在「参考图」单独重出。"
    elif state.clips and any(c.h3_prompt or c.seedance_prompt for c in state.clips):
        state.message = "参考图已就绪。可在「成片」继续出片。"
    else:
        state.message = "参考图阶段已结束。"
    return True


def sync_loaded_project(state: ProjectState) -> bool:
    """加载项目时修复参考图提示词，并清理卡住的出图队列。"""
    changed = reconcile_stale_work(state)
    if repair_picture_prompts(state):
        changed = True
    if state.analysis and state.clips:
        before = [(c.index, c.seedance_prompt or "") for c in state.clips]
        _ensure_clips(state)
        after = [(c.index, c.seedance_prompt or "") for c in state.clips]
        if before != after:
            changed = True
    return changed


QUEUED_STALE_SECONDS = 20


def reconcile_stale_work(state: ProjectState) -> bool:
    if is_project_active(state.id):
        return False
    changed = False
    now = time.time()

    if state.edit_status == "running":
        edited = storage.video_dir(state.id) / (state.edit_filename or "episode_edit.mp4")
        if edited.exists() and edited.stat().st_size > 4096:
            state.edit_status = "succeeded"
            if "自动剪辑完成" not in (state.message or ""):
                state.message = "自动剪辑已完成。"
            state.progress = 100
            if state.status == "editing":
                state.status = "done"
        else:
            state.edit_status = "failed"
            state.edit_error = "剪辑任务已中断，请再点一次「自动剪辑」。"
            state.message = state.edit_error
            if state.status == "editing":
                state.status = "done"
        changed = True

    if state.status in {"queued", "analyzing"}:
        if state.analysis:
            if _maybe_finish_imaging(state):
                return True
            state.status = "done"
            state.progress = max(state.progress, 28)
            state.message = "拆解已完成。可继续出参考图或出片。"
        else:
            state.status = "error"
            state.message = "生成已中断。请点「重新生成本集分镜」。"
        return True

    for pic in state.pictures or []:
        if pic.status != "generating":
            continue
        since = float(getattr(pic, "generating_since", 0) or 0)
        if since and now - since < IMAGE_STALE_SECONDS:
            continue
        pic.status = "failed"
        pic.error = pic.error or "出图任务已中断（超时或服务重启），请点「按此提示词重出」。"
        pic.generating_since = 0
        changed = True

    if state.status not in {"imaging", "prompting", "storyboard"}:
        return changed
    if any(p.status == "generating" for p in state.pictures or []):
        return changed

    pending = [p for p in state.pictures or [] if p.status == "pending" and not p.reused]
    if pending:
        for pic in pending:
            pic.status = "failed"
            pic.error = "出图队列已中断，未开始生成。可改提示词后单独重出。"
            pic.generating_since = 0
        changed = True

    if _maybe_finish_imaging(state):
        changed = True
    elif state.status in {"imaging", "prompting", "storyboard"}:
        state.status = "error"
        state.message = "参考图生成中断，请重新生成分镜。"
        changed = True
    return changed


def cancel_pending_imaging(state: ProjectState) -> bool:
    changed = False
    for pic in state.pictures or []:
        if pic.status == "generating":
            pic.status = "failed"
            pic.error = "已取消出图。"
            pic.generating_since = 0
            changed = True
        elif pic.status == "pending" and not pic.reused:
            pic.status = "failed"
            pic.error = "出图队列已取消，可单独重出。"
            changed = True
    if state.status in {"imaging", "prompting", "storyboard"} and state.analysis:
        if not state.clips:
            state.clips = clip_lib.build_video_clips(state.analysis, state.pictures)
        else:
            _refresh_clip_bindings(state, overwrite_prompt=False)
        _sync_video_prompt_fields(state)
        state.status = "done"
        state.progress = max(state.progress, 82)
        state.message = "已停止参考图队列。失败的图可在「参考图」单独重出。"
        changed = True
    elif state.status == "imaging":
        state.status = "error"
        state.message = "已取消参考图生成。"
        changed = True
    return changed


def _apply_image_result(pic: PictureBinding, result: images.ImageGenerationResult) -> None:
    if result.job_id:
        pic.image_job_id = result.job_id
    if result.generated_prompt:
        pic.generated_prompt = result.generated_prompt
    if result.source_url:
        pic.source_url = result.source_url
    if result.source_path:
        pic.source_path = result.source_path
    pic.verify_warning = " ".join(result.warnings).strip()
    if result.stamp:
        pic.image_stamp = result.stamp
    elif result.path.exists():
        pic.image_stamp = str(result.path.stat().st_mtime_ns)


async def _generate_picture_file(pic: PictureBinding, dest: Path) -> images.ImageGenerationResult:
    result = await asyncio.wait_for(
        images.generate_image(pic.prompt, dest, poll_seconds=IMAGE_GENERATION_TIMEOUT),
        timeout=IMAGE_GENERATION_TIMEOUT + 15,
    )
    _apply_image_result(pic, result)
    return result


def _sync_video_prompt_fields(state: ProjectState) -> None:
    state.video_prompt = clip_lib.join_episode_prompt(state.clips)
    state.video_seedance_prompt = clip_lib.join_episode_seedance_prompt(state.clips)


def _refresh_clip_bindings(
    state: ProjectState, *, overwrite_prompt: bool = True, include_succeeded: bool = False
) -> None:
    if not state.analysis:
        return
    clip_lib.enrich_character_aliases(state.analysis, state.script)
    rebuilt = clip_lib.build_video_clips(state.analysis, state.pictures)
    if not state.clips:
        state.clips = rebuilt
        _sync_video_prompt_fields(state)
        return
    by_index = {c.index: c for c in rebuilt}
    for clip in state.clips:
        fresh = by_index.get(clip.index)
        if not fresh:
            continue
        if clip.video_status == "succeeded" and clip.video_filename and not include_succeeded:
            continue
        pics_changed = list(clip.picture_indices) != list(fresh.picture_indices)
        clip.picture_indices = fresh.picture_indices
        clip.director_notes = fresh.director_notes
        clip.camera_lock = fresh.camera_lock
        clip.headcount_lock = fresh.headcount_lock
        clip.scene_lock = fresh.scene_lock
        clip.lighting_lock = fresh.lighting_lock
        clip.position_lock = fresh.position_lock
        mismatch = not clip_lib.prompt_matches_clip(
            clip.h3_prompt or "", clip, state.analysis, state.pictures
        )
        sd_mismatch = not clip_lib.seedance_prompt_matches_clip(
            clip.seedance_prompt or "", clip, state.analysis, state.pictures
        )
        if overwrite_prompt or not clip.h3_prompt or pics_changed or mismatch:
            clip.h3_prompt = fresh.h3_prompt
        if overwrite_prompt or not clip.seedance_prompt or pics_changed or sd_mismatch:
            clip.seedance_prompt = fresh.seedance_prompt
    _sync_video_prompt_fields(state)


def _ensure_clips(state: ProjectState) -> None:
    if state.clips:
        _refresh_clip_bindings(state, overwrite_prompt=False)
        storage.adopt_existing_videos(state)
        return
    if not state.analysis:
        return
    state.clips = clip_lib.build_video_clips(state.analysis, state.pictures)
    _sync_video_prompt_fields(state)
    storage.adopt_existing_videos(state)


async def ingest_seedance_character_assets(
    state: ProjectState, publish: EmitFn, *, continue_generate: bool = True
) -> ProjectState:
    from . import ark_assets

    characters = [
        pic
        for pic in state.pictures
        if pic.kind == "character" and pic.status == "ready" and pic.filename
    ]
    if not characters:
        return state
    images_dir = storage.image_dir(state.id)
    pending: list[tuple[PictureBinding, Path, str]] = []
    for pic in characters:
        path = images_dir / pic.filename
        if not path.exists():
            continue
        digest = ark_assets.file_digest(path)
        uri = seedance.normalize_asset_uri(pic.seedance_asset_id or "")
        if uri and pic.seedance_asset_hash == digest and seedance.is_live_asset_uri(uri):
            continue
        pending.append((pic, path, digest))
    pending_voices: list[tuple[PictureBinding, Path, str]] = []
    for pic in state.pictures or []:
        if pic.kind != "character" or not pic.voice_filename:
            continue
        vpath = images_dir / pic.voice_filename
        if not vpath.exists():
            continue
        digest = ark_assets.file_digest(vpath)
        uri = seedance.normalize_asset_uri(pic.seedance_audio_asset_id or "")
        if uri and pic.seedance_audio_asset_hash == digest and seedance.is_live_asset_uri(uri):
            continue
        pending_voices.append((pic, vpath, digest))
    prev_status = state.status
    prev_progress = state.progress
    if not pending and not pending_voices:
        if not continue_generate:
            done_stage = prev_status if prev_status in {"done", "retry", "error"} else "done"
            _emit(state, done_stage, "角色图已经在 ToAPIs 虚拟人像库里。", prev_progress or 100)
            storage.save_state(state)
            await publish(state)
        return state
    if not seedance.assets_ready():
        names = "、".join(pic.name for pic, _, _ in pending or pending_voices)
        if seedance.is_toapis() or seedance.is_konjac():
            raise seedance.SeedanceError(
                f"本集还有未入库的角色图（{names}）。请先配置 SEEDANCE_API_KEY。"
            )
        raise ark_assets.ArkAssetError(
            f"本集还有未入库的角色图（{names}）。"
            "请在 .env 填写 VOLCENGINE_ACCESS_KEY 和 VOLCENGINE_SECRET_KEY 后点「一键入库角色图」，"
            "或到方舟控制台「我的 → 虚拟人像」入库后把 asset:// 填到卡片上。"
            "AK/SK 在控制台「访问控制 → 密钥管理」，和 Seedance 的 API Key 不是同一个。"
        )
    owner = state.series_id or state.id
    if not continue_generate:
        state.running = True
    _emit(
        state,
        "generating_video",
        f"正在把 {len(pending)} 张角色图上传到 ToAPIs 虚拟人像库…",
        min(state.progress, 92),
    )
    storage.save_state(state)
    await publish(state)
    for index, (pic, path, digest) in enumerate(pending, start=1):
        _emit(
            state,
            "generating_video",
            f"入库角色图 {index}/{len(pending)}：{pic.name}",
            min(state.progress, 93),
        )
        await publish(state)

        async def _progress(msg: str, name: str = pic.name) -> None:
            _emit(state, "generating_video", msg or f"入库角色图：{name}", min(state.progress, 93))
            await publish(state)

        uri = await seedance.ingest_character_image(
            path, owner=owner, character=pic.name, on_status=_progress
        )
        pic.seedance_asset_id = uri
        pic.seedance_asset_hash = digest
        storage.save_state(state)
        await publish(state)
    for index, (pic, path, digest) in enumerate(pending_voices, start=1):
        _emit(
            state,
            "generating_video",
            f"入库角色声线 {index}/{len(pending_voices)}：{pic.name}",
            min(state.progress, 93),
        )
        await publish(state)
        uri = await seedance.ingest_character_audio(path, owner=owner, character=pic.name)
        pic.seedance_audio_asset_id = uri
        pic.seedance_audio_asset_hash = digest
        storage.save_state(state)
        await publish(state)
    if continue_generate:
        _emit(state, "generating_video", "角色素材已入库，开始提交出片…", min(state.progress, 94))
    else:
        done_stage = prev_status if prev_status in {"done", "retry", "error"} else "done"
        _emit(state, done_stage, "角色图已入库 ToAPIs 虚拟人像库。", prev_progress or 100)
    storage.save_state(state)
    await publish(state)
    return state


def _clip_dest(state: ProjectState, clip) -> Path:
    return storage.video_dir(state.id) / f"clip_{clip.index:02d}.mp4"


def _task_matches_provider(task_id: str, provider: str) -> bool:
    tid = (task_id or "").strip()
    if not tid:
        return False
    seedance_like = tid.startswith("tsk_vid_") or tid.startswith("tsk_")
    if provider == "seedance":
        return seedance_like
    return not seedance_like


def _local_clip_claimed(state: ProjectState, clip) -> bool:
    dest = _clip_dest(state, clip)
    return (
        clip.video_filename == dest.name
        and dest.exists()
        and dest.stat().st_size > 4096
    )


def _discard_local_clip(state: ProjectState, clip) -> None:
    dest = _clip_dest(state, clip)
    if dest.exists():
        try:
            dest.unlink()
        except OSError:
            pass


async def _refresh_clip_from_remote(state: ProjectState, clip) -> dict | None:
    if not clip.task_id:
        return None
    provider = _clip_provider(state, clip)
    if not _task_matches_provider(clip.task_id, provider):
        return None
    dest = _clip_dest(state, clip)
    claimed = _local_clip_claimed(state, clip)
    if provider == "seedance":
        task = await seedance.query_task(clip.task_id)
        seedance.apply_remote_status(clip, task)
    else:
        task = await video.query_task(clip.task_id)
        video.apply_remote_status(clip, task)
    if clip.video_status != "succeeded":
        return task
    if claimed:
        clip.error = ""
        if provider == "seedance":
            _lock_voices_from_succeeded_clip(state, clip, dest)
        return task
    if provider == "seedance":
        await seedance.download_task_video(task, dest)
    else:
        url = _task_video_url(task, provider)
        if not url:
            raise video.MiniMaxError("任务成功但没有视频地址")
        await video.download_video(url, dest)
    clip.video_filename = dest.name
    clip.error = ""
    if provider == "seedance":
        _lock_voices_from_succeeded_clip(state, clip, dest)
    return task


def _needs_video_recover(clip) -> bool:
    if not clip.task_id:
        return False
    if clip.video_status in {"queued", "running", "submitting"}:
        return True
    if clip.video_status == "failed" and not clip.video_filename:
        return True
    if clip.video_status == "succeeded" and not clip.video_filename:
        return True
    return False


async def recover_videos(state: ProjectState, publish: EmitFn, clip_index: int | None = None) -> ProjectState:
    async with _video_lock(state.id):
        latest = storage.load_state(state.id)
        recovered: list[int] = []
        pending: list[int] = []
        for clip in latest.clips:
            if clip_index is not None and clip.index != clip_index:
                continue
            if not _needs_video_recover(clip) and not (
                clip.task_id and clip.video_status == "failed"
            ):
                continue
            try:
                await _refresh_clip_from_remote(latest, clip)
            except Exception as exc:
                clip.error = str(exc)
                _emit(latest, "retry", f"第 {clip.index} 段回捞失败：{exc}", latest.progress)
                continue
            if clip.video_status == "succeeded" and clip.video_filename:
                recovered.append(clip.index)
            elif clip.video_status in {"queued", "running", "submitting"}:
                pending.append(clip.index)
                clip.video_status = clip.video_status
        if recovered:
            _emit(
                latest,
                "done",
                f"已拉回第 { '、'.join(str(i) for i in recovered) } 段已生成视频。",
                100,
            )
            merge_episode_video(latest)
        elif pending:
            _emit(
                latest,
                "generating_video",
                f"第 {'、'.join(str(i) for i in pending)} 段还在中转生成，稍后会自动拉回。",
                90,
            )
        else:
            _emit(latest, "done", "没有可拉回的已完成视频。若中转已过期，需要重新出这一段。", 100)
        storage.save_state(latest)
        await publish(latest)
        return latest


async def _poll_existing_clip(state: ProjectState, clip, publish: EmitFn, dest: Path, provider: str) -> None:
    while True:
        await _refresh_clip_from_remote(state, clip)
        status = clip.video_status
        remain = clip.estimated_remaining_seconds
        if status == "queued":
            _emit(state, "generating_video", f"第 {clip.index} 段排队中…", state.progress)
        elif status == "running":
            extra = f"，约还要 {remain} 秒" if remain else ""
            _emit(state, "generating_video", f"第 {clip.index} 段生成中{extra}", state.progress)
        elif status == "succeeded":
            _emit(state, "generating_video", f"第 {clip.index} 段已完成。", min(98, state.progress + 2))
            storage.save_state(state)
            await publish(state)
            return
        elif status in {"failed", "cancelled"}:
            _emit(state, "generating_video", f"第 {clip.index} 段失败：{clip.error or '未知错误'}", state.progress)
            storage.save_state(state)
            await publish(state)
            return
        storage.save_state(state)
        await publish(state)
        await asyncio.sleep(4)


async def _run_one_clip(state: ProjectState, clip, publish: EmitFn, *, force: bool = False) -> None:
    provider = _normalize_provider(state.video_provider)
    clip.video_provider = provider
    seedance_model = seedance.normalize_model(state.seedance_model)
    clip.seedance_model = seedance_model
    provider_label = seedance.model_label(seedance_model) if provider == "seedance" else "MiniMax H3"

    reuse_task = (
        bool(clip.task_id)
        and not force
        and _task_matches_provider(clip.task_id, provider)
    )
    if reuse_task:
        _emit(
            state,
            "generating_video",
            f"正在回捞第 {clip.index} 段已有任务…",
            min(94, 86 + clip.index),
        )
        storage.save_state(state)
        await publish(state)
        try:
            await _refresh_clip_from_remote(state, clip)
        except Exception as exc:
            _emit(
                state,
                "generating_video",
                f"第 {clip.index} 段回捞失败（{exc}），将重新提交…",
                state.progress,
            )
        else:
            if clip.video_status == "succeeded" and clip.video_filename:
                _emit(state, "generating_video", f"第 {clip.index} 段已从中转拉回。", min(98, state.progress + 2))
                storage.save_state(state)
                await publish(state)
                return
            if clip.video_status in {"queued", "running", "submitting"}:
                dest = _clip_dest(state, clip)
                try:
                    await _poll_existing_clip(state, clip, publish, dest, provider)
                except Exception as exc:
                    clip.video_status = "running"
                    clip.error = f"本地等待中断，任务还在跑：{exc}"
                    _emit(
                        state,
                        "generating_video",
                        f"第 {clip.index} 段本地等待中断，稍后会自动拉回已完成视频。",
                        state.progress,
                    )
                    storage.save_state(state)
                    await publish(state)
                return

    if force or (clip.task_id and not _task_matches_provider(clip.task_id, provider)):
        _discard_local_clip(state, clip)
        clip.task_id = ""
    clip.error = ""
    clip.video_filename = ""
    clip.video_status = "submitting"
    clip.estimated_remaining_seconds = 0
    _emit(
        state,
        "generating_video",
        f"正在提交第 {clip.index} 段《{clip.title or '片段'}》（{provider_label}）…",
        min(94, 86 + clip.index),
    )
    storage.save_state(state)
    await publish(state)

    if provider == "seedance":
        try:
            await ingest_seedance_character_assets(state, publish)
        except Exception as exc:
            clip.video_status = "failed"
            clip.error = str(exc)
            _emit(state, "generating_video", f"第 {clip.index} 段失败：{clip.error}", state.progress)
            storage.save_state(state)
            await publish(state)
            return

    images_dir = storage.image_dir(state.id)
    ordered_pics = clip_lib.pictures_for_clip(state.pictures, clip)
    refs: list = []
    for pic in ordered_pics:
        asset_uri = seedance.normalize_asset_uri(getattr(pic, "seedance_asset_id", "") or "")
        if provider == "seedance" and pic.kind == "character" and seedance.is_live_asset_uri(asset_uri):
            refs.append(asset_uri)
            continue
        if pic.status != "ready" or not pic.filename:
            continue
        path = images_dir / pic.filename
        if path.exists():
            refs.append(path)
    audios: list = []
    audio_pics: list[PictureBinding] = []
    if provider == "seedance":
        audios, audio_pics = _voice_refs_for_clip(state, clip, images_dir)
    dest = storage.video_dir(state.id) / f"clip_{clip.index:02d}.mp4"
    prompt = (clip.seedance_prompt or clip.h3_prompt) if provider == "seedance" else clip.h3_prompt
    if provider == "seedance":
        missing = [
            pic.name
            for pic in ordered_pics
            if not (
                (pic.kind == "character" and seedance.is_live_asset_uri(pic.seedance_asset_id or ""))
                or (pic.status == "ready" and pic.filename and (images_dir / pic.filename).exists())
            )
        ]
        if missing:
            clip.video_status = "failed"
            clip.error = "参考图文件缺失，请回到「参考图」检查后再出片：" + "、".join(missing)
            storage.save_state(state)
            await publish(state)
            return
        prompt = clip_lib.finalize_seedance_prompt(prompt, ordered_pics, audio_pictures=audio_pics)
    prev_videos: list[Path] = []
    if state.analysis and provider == "minimax":
        prev_plan = clip_lib.previous_clip_plan(state.analysis, clip)
        prev_clip = next((c for c in state.clips if prev_plan and c.index == prev_plan.index), None)
        if (
            prev_plan
            and prev_clip
            and prev_clip.video_status == "succeeded"
            and prev_clip.video_filename
            and clip_lib.clips_share_scene(state.analysis, prev_plan, clip)
        ):
            src = storage.video_dir(state.id) / prev_clip.video_filename
            tail = video.extract_clip_tail(
                src, storage.video_dir(state.id) / f"clip_{clip.index:02d}_prev_tail.mp4"
            )
            if tail:
                prev_videos = [tail]
                prompt = clip_lib.with_prev_video_note(prompt)
                _emit(
                    state,
                    "generating_video",
                    f"第 {clip.index} 段将以上一段尾帧锁定人物站位…",
                    min(94, 86 + clip.index),
                )
    try:
        create_kwargs = {
            "prompt": prompt,
            "duration": clip.duration_seconds,
            "ratio": state.video_ratio or "9:16",
            "resolution": state.video_resolution or "768P",
            "images": refs,
            "idempotency_key": f"{state.id}-clip-{clip.index}-{int(time.time())}",
        }
        try:
            if provider == "seedance":
                task_id = await seedance.create_task(
                    **create_kwargs,
                    videos=prev_videos or None,
                    audios=audios or None,
                    model=seedance_model,
                )
            else:
                task_id = await video.create_task(
                    **create_kwargs,
                    videos=prev_videos,
                    use_context_ir=state.use_context_ir,
                )
        except (video.MiniMaxError, seedance.SeedanceError) as exc:
            ref_rejected = (
                video.video_ref_rejected(exc)
                if provider == "minimax"
                else seedance.video_ref_rejected(exc)
            )
            if not prev_videos or not ref_rejected:
                raise
            if provider == "seedance":
                task_id = await seedance.create_task(
                    clip.seedance_prompt or clip.h3_prompt,
                    duration=clip.duration_seconds,
                    ratio=state.video_ratio or "9:16",
                    resolution=state.video_resolution or "768P",
                    images=refs,
                    audios=audios or None,
                    idempotency_key=f"{state.id}-clip-{clip.index}-{int(time.time())}-novid",
                    model=seedance_model,
                )
            else:
                task_id = await video.create_task(
                    clip.h3_prompt,
                    duration=clip.duration_seconds,
                    ratio=state.video_ratio or "9:16",
                    resolution=state.video_resolution or "768P",
                    images=refs,
                    idempotency_key=f"{state.id}-clip-{clip.index}-{int(time.time())}-novid",
                    use_context_ir=state.use_context_ir,
                )
        clip.task_id = task_id
        clip.video_status = "queued"
        storage.save_state(state)
        await publish(state)
        await _poll_existing_clip(state, clip, publish, dest, provider)
    except Exception as exc:
        if clip.task_id:
            clip.video_status = "running"
            clip.error = f"本地等待中断，任务还在跑：{exc}"
            _emit(
                state,
                "generating_video",
                f"第 {clip.index} 段本地等待中断，稍后会自动拉回已完成视频。",
                state.progress,
            )
        else:
            clip.video_status = "failed"
            clip.error = (
                seedance.humanize_create_error(exc, ordered_pics)
                if provider == "seedance"
                else str(exc)
            )
            _emit(state, "generating_video", f"第 {clip.index} 段失败：{clip.error}", state.progress)
    storage.save_state(state)
    await publish(state)


async def reconcile_videos(state: ProjectState) -> ProjectState:
    lock = _video_lock(state.id)
    if lock.locked():
        storage.adopt_existing_videos(state)
        return state
    async with lock:
        latest = storage.load_state(state.id)
        if storage.adopt_existing_videos(latest):
            merge_episode_video(latest)
            storage.save_state(latest)
        return latest


def merge_episode_video(state: ProjectState) -> bool:
    ok = [
        c
        for c in sorted(state.clips, key=lambda x: x.index)
        if c.video_status == "succeeded" and c.video_filename
    ]
    paths = [storage.video_dir(state.id) / c.video_filename for c in ok]
    paths = [p for p in paths if p.exists()]
    if not paths:
        return False
    merged = storage.video_dir(state.id) / "episode.mp4"
    if video.concat_with_ffmpeg(paths, merged):
        state.episode_video_filename = merged.name
        return True
    return False


async def generate_videos(
    state: ProjectState,
    publish: EmitFn,
    clip_index: int | None = None,
    force: bool = False,
) -> ProjectState:
    async with _video_lock(state.id):
        latest = storage.load_state(state.id)
        incoming = {c.index: c for c in state.clips}
        for clip in latest.clips:
            src = incoming.get(clip.index)
            if src and src.h3_prompt:
                clip.h3_prompt = src.h3_prompt
            if src and src.seedance_prompt:
                clip.seedance_prompt = src.seedance_prompt
        latest.video_resolution = state.video_resolution or latest.video_resolution
        latest.video_ratio = state.video_ratio or latest.video_ratio
        latest.use_context_ir = state.use_context_ir
        latest.video_provider = _normalize_provider(state.video_provider or latest.video_provider)
        latest.seedance_model = seedance.normalize_model(state.seedance_model or latest.seedance_model)
        return await _generate_videos_locked(latest, publish, clip_index=clip_index, force=force)


async def _generate_videos_locked(
    state: ProjectState,
    publish: EmitFn,
    clip_index: int | None = None,
    force: bool = False,
) -> ProjectState:
    _ensure_clips(state)
    if not state.clips:
        raise RuntimeError("还没有可生成的片段")
    block = _picture_block_reason(state)
    if block:
        _emit(state, "done", block, 100)
        storage.save_state(state)
        await publish(state)
        return state
    provider = _normalize_provider(state.video_provider)
    if not _provider_configured(provider):
        missing = _provider_missing_message(provider)
        for clip in state.clips:
            if clip_index is not None and clip.index != clip_index:
                continue
            clip.video_status = "failed"
            clip.error = missing
        label = "Seedance" if provider == "seedance" else "MiniMax"
        _emit(state, "done", f"提示词已就绪，但未配置 {label} Key，无法自动出片。", 100)
        storage.save_state(state)
        await publish(state)
        return state

    targets = []
    for clip in state.clips:
        if clip_index is not None and clip.index != clip_index:
            continue
        if clip.video_status == "succeeded" and clip.video_filename and not force:
            continue
        targets.append(clip)
    if not targets:
        _emit(state, "done", "成片已全部就绪。", 100)
        storage.save_state(state)
        await publish(state)
        return state

    if provider == "seedance":
        harvested = _harvest_voices_from_existing_clips(state)
        if harvested:
            _emit(
                state,
                "generating_video",
                f"已从已完成片段锁定「{'、'.join(harvested)}」声线，后续同角色会跟这个声音。",
                86,
            )
            storage.save_state(state)
            await publish(state)

    total = len(targets)
    for i, clip in enumerate(targets, start=1):
        _emit(state, "generating_video", f"出片 {i}/{total}：{clip.title or f'第 {clip.index} 段'}", 88)
        storage.save_state(state)
        await publish(state)
        await _run_one_clip(state, clip, publish, force=force)

    ok = [c for c in state.clips if c.video_status == "succeeded" and c.video_filename]
    failed = [c for c in state.clips if c.video_status == "failed"]
    merge_episode_video(state)
    if failed and not ok:
        _emit(state, "done", f"出片未成功：{failed[0].error}", 100)
    elif failed:
        _emit(state, "done", f"完成 {len(ok)} 段，失败 {len(failed)} 段，可在成片里重试。", 100)
    else:
        _emit(state, "done", f"成片完成，共 {len(ok)} 段。正在拼接原片（保留原声音）…", 96)
        storage.save_state(state)
        await publish(state)
        await _generate_edit_locked(
            state, publish, dub=False, subtitles=False, remove_burned_subs=False, ambience=False
        )
        return state
    storage.save_state(state)
    await publish(state)
    return state


async def sync_videos(state: ProjectState, publish: EmitFn) -> ProjectState:
    async with _video_lock(state.id):
        latest = storage.load_state(state.id)
        return await _sync_videos_locked(latest, publish)


async def _sync_videos_locked(state: ProjectState, publish: EmitFn) -> ProjectState:
    if storage.adopt_existing_videos(state):
        merge_episode_video(state)
        storage.save_state(state)
        await publish(state)
    dirty = False
    for clip in state.clips:
        if not clip.task_id:
            continue
        if not _needs_video_recover(clip) and clip.video_status != "failed":
            continue
        try:
            await _refresh_clip_from_remote(state, clip)
            dirty = True
        except Exception as exc:
            clip.error = str(exc)
            dirty = True
    if dirty:
        storage.adopt_existing_videos(state)
        merge_episode_video(state)
        storage.save_state(state)
        await publish(state)
    return state


async def generate_edit(
    state: ProjectState,
    publish: EmitFn,
    *,
    dub: bool = True,
    subtitles: bool = True,
    remove_burned_subs: bool = True,
    ambience: bool = True,
) -> ProjectState:
    async with _video_lock(state.id):
        latest = storage.load_state(state.id)
        return await _generate_edit_locked(
            latest,
            publish,
            dub=dub,
            subtitles=subtitles,
            remove_burned_subs=remove_burned_subs,
            ambience=ambience,
        )


async def _generate_edit_locked(
    state: ProjectState,
    publish: EmitFn,
    *,
    dub: bool,
    subtitles: bool,
    remove_burned_subs: bool = True,
    ambience: bool = True,
) -> ProjectState:
    mark_project_active(state.id)
    state.edit_status = "running"
    state.edit_error = ""
    _emit(state, "editing", "正在自动剪辑：拼接片段…", 92)
    storage.save_state(state)
    await publish(state)

    async def on_progress(message: str) -> None:
        _emit(state, "editing", message, 94)
        storage.save_state(state)
        await publish(state)

    try:
        result = await edit.render_episode_edit(
            state,
            dub=dub,
            subtitles=subtitles,
            remove_burned_subs=remove_burned_subs,
            ambience=ambience,
            on_progress=on_progress,
        )
        state.edit_status = "succeeded"
        state.edit_filename = result.get("filename") or ""
        state.subtitle_filename = result.get("subtitle") or ""
        state.voiceover_filename = result.get("voiceover") or ""
        dubbed = int(result.get("dubbed") or 0)
        cues = int(result.get("cues") or 0)
        extra = f"配音 {dubbed} 句" if dubbed else "未配音"
        sfx = int(result.get("sfx") or 0)
        if sfx:
            extra += f"，环境音 {sfx} 条"
        if result.get("dub_error"):
            extra += f"（{result['dub_error']}）"
        wiped = "已去模型字幕，" if result.get("removed_burned") else ""
        _emit(state, "done", f"自动剪辑完成：{wiped}字幕 {cues} 条，{extra}。", 100)
    except Exception as exc:
        state.edit_status = "failed"
        state.edit_error = str(exc)
        _emit(state, "done", f"自动剪辑失败：{exc}", 100)
    finally:
        mark_project_inactive(state.id)
    storage.save_state(state)
    await publish(state)
    return state



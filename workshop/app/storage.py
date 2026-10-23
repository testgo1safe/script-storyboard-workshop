from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from pathlib import Path

from .config import PROJECTS_DIR, SERIES_DIR
from .schemas import Series, SeriesAsset, SeriesEpisode, ProjectState

_state_cache: dict[str, tuple[float, ProjectState]] = {}


def project_dir(project_id: str) -> Path:
    path = PROJECTS_DIR / project_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "images").mkdir(exist_ok=True)
    return path


def state_path(project_id: str) -> Path:
    return project_dir(project_id) / "state.json"


def load_state(project_id: str) -> ProjectState:
    path = state_path(project_id)
    if not path.exists():
        raise FileNotFoundError(project_id)
    mtime = path.stat().st_mtime
    cached = _state_cache.get(project_id)
    if cached and cached[0] == mtime:
        return cached[1].model_copy(deep=True)
    state = ProjectState.model_validate_json(path.read_text(encoding="utf-8"))
    _state_cache[project_id] = (mtime, state)
    return state.model_copy(deep=True)


def drop_state_cache(project_id: str = "") -> None:
    if project_id:
        _state_cache.pop(project_id, None)
    else:
        _state_cache.clear()


def save_state(state: ProjectState) -> None:
    path = state_path(state.id)
    payload = state.model_dump(exclude={"running"})
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _state_cache[state.id] = (path.stat().st_mtime, state.model_copy(deep=True))
    if state.video_prompt:
        (project_dir(state.id) / "video_prompt.txt").write_text(
            state.video_prompt, encoding="utf-8"
        )
    if state.clips:
        clips_dir = project_dir(state.id) / "clips"
        clips_dir.mkdir(exist_ok=True)
        for clip in state.clips:
            (clips_dir / f"clip_{clip.index:02d}.txt").write_text(
                clip.h3_prompt or "", encoding="utf-8"
            )
            if clip.seedance_prompt:
                (clips_dir / f"clip_{clip.index:02d}_seedance.txt").write_text(
                    clip.seedance_prompt, encoding="utf-8"
                )


def image_dir(project_id: str) -> Path:
    return project_dir(project_id) / "images"


def video_dir(project_id: str) -> Path:
    path = project_dir(project_id) / "videos"
    path.mkdir(parents=True, exist_ok=True)
    return path


def adopt_existing_videos(state: ProjectState) -> bool:
    """把磁盘上已有的 clip_XX.mp4 认回 clips，避免并行出片把状态写丢。"""
    if not state.clips:
        return False
    vdir = video_dir(state.id)
    dirty = False
    busy = {"submitting", "queued", "running"}
    now = time.time()
    for clip in state.clips:
        path = vdir / f"clip_{clip.index:02d}.mp4"
        if path.exists() and path.stat().st_size > 4096:
            age = now - path.stat().st_mtime
            if clip.video_status in busy:
                if age < 12:
                    continue
                if age > 120:
                    continue
            if clip.video_status != "succeeded" or clip.video_filename != path.name:
                clip.video_status = "succeeded"
                clip.video_filename = path.name
                clip.error = ""
                dirty = True
            continue
        if clip.video_status == "submitting" and not clip.task_id:
            clip.video_status = "idle"
            clip.video_filename = ""
            dirty = True
            continue
        if clip.video_status == "succeeded":
            named = vdir / (clip.video_filename or "")
            if not named.exists():
                clip.video_status = "idle"
                clip.video_filename = ""
                dirty = True
    episode = vdir / "episode.mp4"
    if episode.exists() and episode.stat().st_size > 4096:
        if state.episode_video_filename != episode.name:
            state.episode_video_filename = episode.name
            dirty = True
    return dirty


def normalize_name(name: str) -> str:
    return re.sub(r"[\s·・\.\-_,，。、\(\)（）\[\]【】「」]", "", (name or "").strip()).lower()


def safe_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]', "_", (name or "").strip())
    return cleaned[:48] or "asset"


def series_dir(series_id: str) -> Path:
    path = SERIES_DIR / series_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "assets").mkdir(exist_ok=True)
    return path


def series_asset_dir(series_id: str) -> Path:
    return series_dir(series_id) / "assets"


def series_video_dir(series_id: str) -> Path:
    path = series_dir(series_id) / "videos"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_series(series_id: str) -> Series:
    path = series_dir(series_id) / "series.json"
    if not path.exists():
        raise FileNotFoundError(series_id)
    return Series.model_validate_json(path.read_text(encoding="utf-8"))


def save_series(series: Series) -> None:
    path = series_dir(series.id) / "series.json"
    path.write_text(json.dumps(series.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")


def list_series() -> list[Series]:
    SERIES_DIR.mkdir(parents=True, exist_ok=True)
    items: list[Series] = []
    for path in SERIES_DIR.glob("*/series.json"):
        try:
            items.append(Series.model_validate_json(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    items.sort(key=lambda s: s.created_at, reverse=True)
    return items


def find_series_by_name(name: str) -> Series | None:
    key = normalize_name(name)
    if not key:
        return None
    for series in list_series():
        if normalize_name(series.name) == key:
            return series
    return None


def create_series(name: str) -> Series:
    series = Series(
        id=uuid.uuid4().hex[:12],
        name=name.strip() or "未命名剧",
        created_at=time.time(),
    )
    save_series(series)
    return series


def resolve_series(series_id: str = "", series_name: str = "") -> Series | None:
    if series_id:
        try:
            return load_series(series_id)
        except FileNotFoundError:
            pass
    name = (series_name or "").strip()
    if not name:
        return None
    existing = find_series_by_name(name)
    return existing or create_series(name)


def name_keys(name: str, aliases: list[str] | None = None) -> set[str]:
    keys: set[str] = set()
    for item in [name, *(aliases or [])]:
        text = (item or "").strip()
        if not text:
            continue
        keys.add(normalize_name(text))
        for part in re.split(r"[/／、,，;；|]", text):
            part = part.strip()
            if len(part) >= 2:
                keys.add(normalize_name(part))
    keys.discard("")
    return keys


def find_asset(series: Series, kind: str, name: str, aliases: list[str] | None = None) -> SeriesAsset | None:
    want_name = normalize_name(name)
    if not want_name:
        return None
    want_aliases = {normalize_name(item) for item in (aliases or []) if (item or "").strip()}
    want_aliases.discard("")
    alias_hit = None
    for asset in series.assets:
        if asset.kind != kind:
            continue
        have_name = normalize_name(asset.name)
        have_aliases = {
            normalize_name(item)
            for item in (getattr(asset, "aliases", None) or [])
            if (item or "").strip()
        }
        have_aliases.discard("")
        if have_name == want_name:
            return asset
        # 只认「本名 ↔ 别称」，禁止靠「手机」「客厅」这种公共词把不同东西配在一起。
        if want_name in have_aliases or have_name in want_aliases:
            if alias_hit is None:
                alias_hit = asset
    return alias_hit


def asset_image_path(series_id: str, filename: str) -> Path:
    return series_asset_dir(series_id) / filename


def copy_asset_to_project(series_id: str, asset: SeriesAsset, dest: Path) -> bool:
    source = asset_image_path(series_id, asset.filename)
    if not source.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return True


def upsert_series_asset(
    series: Series,
    *,
    kind: str,
    name: str,
    lock_text: str,
    prompt: str,
    source_image: Path | None,
    appearance: str = "",
    wardrobe: str = "",
    description: str = "",
    aliases: list[str] | None = None,
) -> Series:
    filename = f"{kind}_{safe_filename(name)}.png"
    existing = find_asset(series, kind, name, aliases)
    merged_aliases = []
    for item in list(aliases or []) + list(getattr(existing, "aliases", None) or []):
        if item and item not in merged_aliases and item != name:
            merged_aliases.append(item)
    if existing:
        existing.lock_text = lock_text or existing.lock_text
        existing.prompt = prompt or existing.prompt
        existing.appearance = appearance or existing.appearance
        existing.wardrobe = wardrobe or existing.wardrobe
        existing.description = description or existing.description
        existing.aliases = merged_aliases or existing.aliases
        filename = existing.filename or filename
        existing.filename = filename
    else:
        series.assets.append(
            SeriesAsset(
                kind=kind,  # type: ignore[arg-type]
                name=name,
                lock_text=lock_text,
                prompt=prompt,
                filename=filename,
                appearance=appearance,
                wardrobe=wardrobe,
                description=description,
                aliases=merged_aliases,
            )
        )
    if source_image and source_image.exists():
        dest = asset_image_path(series.id, filename)
        if source_image.resolve() != dest.resolve():
            shutil.copy2(source_image, dest)
    save_series(series)
    return series


def episode_cn_label(n: int) -> str:
    if n <= 0:
        n = 1
    chars = "一二三四五六七八九十"
    if n <= 10:
        return f"第{chars[n - 1]}集"
    if n < 20:
        return f"第十{chars[n - 11]}集"
    tens, ones = divmod(n, 10)
    head = chars[tens - 1] + "十"
    return f"第{head}{chars[ones - 1] if ones else ''}集"


def append_episode(series: Series, project_id: str, title: str) -> Series:
    if any(ep.project_id == project_id for ep in series.episodes):
        return series
    index = len(series.episodes) + 1
    series.episodes.append(
        SeriesEpisode(
            project_id=project_id,
            title=title.strip(),
            index=index,
            created_at=time.time(),
        )
    )
    save_series(series)
    return series


def rename_series(series_id: str, name: str) -> Series:
    series = load_series(series_id)
    series.name = name.strip() or series.name
    save_series(series)
    return series


def delete_project_files(project_id: str) -> None:
    drop_state_cache(project_id)
    path = PROJECTS_DIR / project_id
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _reindex_episodes(series: Series) -> None:
    for i, ep in enumerate(series.episodes, start=1):
        ep.index = i
        try:
            state = load_state(ep.project_id)
        except FileNotFoundError:
            continue
        state.episode_index = i
        save_state(state)


def delete_episode(series_id: str, project_id: str) -> Series:
    series = load_series(series_id)
    before = len(series.episodes)
    series.episodes = [ep for ep in series.episodes if ep.project_id != project_id]
    if len(series.episodes) == before:
        raise FileNotFoundError(project_id)
    _reindex_episodes(series)
    save_series(series)
    delete_project_files(project_id)
    return series


def delete_episode_everywhere(project_id: str) -> None:
    matched = False
    for series in list_series():
        if any(ep.project_id == project_id for ep in series.episodes):
            delete_episode(series.id, project_id)
            matched = True
            break
    path = PROJECTS_DIR / project_id
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
        matched = True
    if not matched:
        raise FileNotFoundError(project_id)


def delete_series(series_id: str) -> None:
    series = load_series(series_id)
    for ep in list(series.episodes):
        delete_project_files(ep.project_id)
    path = SERIES_DIR / series_id
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _state_summary(project_id: str) -> dict:
    path = state_path(project_id)
    if not path.exists():
        raise FileNotFoundError(project_id)
    raw = json.loads(path.read_text(encoding="utf-8"))
    analysis = raw.get("analysis") or {}
    return {
        "status": raw.get("status", "unknown"),
        "progress": int(raw.get("progress") or 0),
        "message": raw.get("message") or "",
        "episode_title": raw.get("episode_title") or "",
        "analysis_title": analysis.get("title") or "",
        "edit_filename": raw.get("edit_filename") or "",
        "episode_video_filename": raw.get("episode_video_filename") or "",
    }


def episode_payload(ep: SeriesEpisode, index: int) -> dict:
    label = episode_cn_label(ep.index or index)
    subtitle = (ep.title or "").strip()
    subtitle = re.sub(r"^第\s*[0-9一二三四五六七八九十百]+\s*集\s*[·\-—:：]?\s*", "", subtitle)
    if subtitle in {label, f"第 {index} 集", f"第{index}集"}:
        subtitle = ""
    item = {
        "id": ep.project_id,
        "index": ep.index or index,
        "label": label,
        "title": subtitle,
        "created_at": ep.created_at,
        "status": "unknown",
        "progress": 0,
        "message": "",
        "has_edit": False,
        "has_video": False,
    }
    try:
        summary = _state_summary(ep.project_id)
        item["status"] = summary["status"]
        item["progress"] = summary["progress"]
        item["message"] = summary["message"]
        extra = summary["episode_title"].strip()
        extra = re.sub(r"^第\s*[0-9一二三四五六七八九十百]+\s*集\s*[·\-—:：]?\s*", "", extra)
        if extra and extra not in {label, f"第 {index} 集"}:
            item["title"] = extra
        elif summary["analysis_title"] and not item["title"]:
            item["title"] = summary["analysis_title"]
        item["has_edit"] = bool(summary["edit_filename"])
        item["has_video"] = bool(summary["edit_filename"] or summary["episode_video_filename"])
        vdir = video_dir(ep.project_id)
        if (vdir / "episode_edit.mp4").exists():
            item["has_edit"] = True
            item["has_video"] = True
        elif (vdir / "episode.mp4").exists():
            item["has_video"] = True
    except FileNotFoundError:
        item["status"] = "missing"
    return item


def series_payload(series: Series) -> dict:
    episodes = []
    for i, ep in enumerate(series.episodes, start=1):
        episodes.append(episode_payload(ep, i))
    return {
        "id": series.id,
        "name": series.name,
        "created_at": series.created_at,
        "episode_count": len(series.episodes),
        "assets": [
            {
                "kind": a.kind,
                "name": a.name,
                "filename": a.filename,
                "lock_text": a.lock_text,
            }
            for a in series.assets
        ],
        "episodes": episodes,
        "compile_status": series.compile_status,
        "compile_filename": series.compile_filename,
        "compile_error": series.compile_error,
        "compile_episodes": list(series.compile_episodes or []),
    }


def bible_text(series: Series | None) -> str:
    if not series or not series.assets:
        return ""
    lines = [f"本剧《{series.name}》已有设定，新一集必须沿用同名主体，不要改脸型、发型、服装和道具外形。"]
    for kind, label in (("character", "已有角色"), ("scene", "已有场景"), ("prop", "已有道具")):
        items = [a for a in series.assets if a.kind == kind]
        if not items:
            continue
        lines.append(label + "：")
        for asset in items:
            extra = asset.lock_text or asset.description or asset.appearance
            aka = "、".join(getattr(asset, "aliases", None) or [])
            line = f"- {asset.name}"
            if aka:
                line += f"（别称：{aka}）"
            if extra:
                line += f"：{extra}"
            lines.append(line)
    lines.append("同名或别称相同的角色必须沿用同一张脸。外卖员、父亲、本名如果是同一个人，不要拆成两个角色。")
    lines.append("已有设定只用于外形沿用。禁止把其他集已经发生的情节写进本集分镜、对白或视频提示词。")
    return "\n".join(lines)


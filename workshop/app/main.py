from __future__ import annotations

import asyncio
import io
import re
import time
import uuid
import zipfile

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import clips as clip_lib, edit, episodesplit, images, pipeline, seedance, storage, video
from .config import WEB_DIR, VIDEO_ENV_PATH, settings
from .schemas import (
    BindSeedanceAssetRequest,
    CreateProjectRequest,
    CreateSeriesRequest,
    EstimateDurationRequest,
    GenerateEditRequest,
    GenerateVideoRequest,
    ProjectState,
    RenameSeriesRequest,
    RefetchPictureRequest,
    RegeneratePictureRequest,
    SplitEpisodeOut,
    SplitSeriesRequest,
    SplitSeriesResponse,
)

app = FastAPI(title="剧本分镜工坊", version="1.0.0")

_subscribers: dict[str, list[asyncio.Queue]] = {}
_lock = asyncio.Lock()
_tasks: set[asyncio.Task] = set()
_video_syncing: set[str] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _publish(state: ProjectState) -> None:
    async with _lock:
        queues = list(_subscribers.get(state.id, []))
    for q in queues:
        await q.put(state.model_dump())


def _load_project(project_id: str) -> ProjectState:
    state = storage.load_state(project_id)
    if pipeline.sync_loaded_project(state):
        storage.save_state(state)
    state.running = pipeline.is_project_active(state.id)
    return state


def _recover_series_episodes(series) -> None:
    for ep in series.episodes or []:
        try:
            state = storage.load_state(ep.project_id)
        except FileNotFoundError:
            continue
        if pipeline.sync_loaded_project(state):
            storage.save_state(state)


def _needs_reconcile(state: ProjectState) -> bool:
    return any(
        c.task_id
        and (
            c.video_status in {"submitting", "queued", "running"}
            or (c.video_status in {"failed", "succeeded"} and not c.video_filename)
        )
        for c in state.clips
    )


@app.get("/api/series")
async def list_series():
    items = []
    for series in storage.list_series():
        _recover_series_episodes(series)
        items.append(storage.series_payload(series))
    return items


@app.post("/api/series")
async def create_series(body: CreateSeriesRequest):
    name = body.name.strip()
    existing = storage.find_series_by_name(name)
    if existing:
        return storage.series_payload(existing)
    return storage.series_payload(storage.create_series(name))


@app.get("/api/series/{series_id}")
async def get_series(series_id: str):
    try:
        return storage.series_payload(storage.load_series(series_id))
    except FileNotFoundError:
        raise HTTPException(404, "项目不存在")


@app.patch("/api/series/{series_id}")
async def rename_series(series_id: str, body: RenameSeriesRequest):
    try:
        return storage.series_payload(storage.rename_series(series_id, body.name))
    except FileNotFoundError:
        raise HTTPException(404, "项目不存在")


@app.delete("/api/series/{series_id}")
async def delete_series(series_id: str):
    try:
        storage.delete_series(series_id)
    except FileNotFoundError:
        raise HTTPException(404, "项目不存在")
    return {"ok": True, "id": series_id}


@app.get("/api/series/{series_id}/assets/{filename}")
async def get_series_asset(series_id: str, filename: str):
    path = storage.asset_image_path(series_id, filename)
    if not path.exists():
        raise HTTPException(404, "图片不存在")
    return FileResponse(path)


@app.post("/api/series/{series_id}/compile")
async def compile_series(series_id: str):
    try:
        series = storage.load_series(series_id)
    except FileNotFoundError:
        raise HTTPException(404, "项目不存在")
    if series.compile_status == "running":
        return storage.series_payload(series)
    ready = edit.collect_series_parts(series_id)
    if not ready:
        raise HTTPException(400, "还没有可拼接的剧集成片。请先把各集出片或自动剪辑。")
    series.compile_status = "running"
    series.compile_error = ""
    storage.save_series(series)

    async def _run() -> None:
        latest = storage.load_series(series_id)
        try:
            result = await asyncio.to_thread(edit.compile_series, series_id)
            latest = storage.load_series(series_id)
            latest.compile_status = "succeeded"
            latest.compile_filename = result.get("filename") or "series_full.mp4"
            latest.compile_episodes = list(result.get("episodes") or [])
            latest.compile_error = ""
        except Exception as exc:
            latest.compile_status = "failed"
            latest.compile_error = str(exc)
        storage.save_series(latest)

    _spawn(_run())
    return storage.series_payload(series)


@app.get("/api/series/{series_id}/videos/{filename}")
async def get_series_video(series_id: str, filename: str):
    path = storage.series_video_dir(series_id) / filename
    if not path.exists() or path.parent.resolve() != storage.series_video_dir(series_id).resolve():
        raise HTTPException(404, "视频不存在")
    media = {".mp4": "video/mp4"}.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media)


@app.get("/api/health")
async def health():
    image = await images.health()
    minimax = await video.health()
    seedance_info = await seedance.health()
    return {
        "app": "ok",
        "deepseek_configured": bool(settings.deepseek_api_key),
        "minimax_configured": bool(settings.minimax_api_key),
        "minimax": minimax,
        "seedance_configured": bool(settings.seedance_api_key),
        "seedance": seedance_info,
        "seedance_assets_configured": seedance.assets_ready(),
        "image_api": image,
        "image_api_base": settings.image_api_base,
        "doubao_configured": bool(settings.doubao_api_key),
        "doubao_source": str(VIDEO_ENV_PATH) if VIDEO_ENV_PATH else "",
        "ffmpeg": edit.ffmpeg_available(),
    }


@app.post("/api/duration-estimate")
async def duration_estimate(body: EstimateDurationRequest):
    return clip_lib.estimate_episode_duration(body.script)


def _episode_titles(episode_index: int, subtitle: str) -> tuple[str, str]:
    label = storage.episode_cn_label(episode_index)
    extra = (subtitle or "").strip()
    extra = re.sub(r"^第\s*[0-9一二三四五六七八九十百]+\s*集\s*[·\-—:：]?\s*", "", extra)
    if extra in {label, f"第 {episode_index} 集", f"第{episode_index}集"}:
        extra = ""
    display = f"{label} · {extra}" if extra else label
    return display, extra


def _make_episode_state(body: CreateProjectRequest, series, episode_index: int, subtitle: str) -> ProjectState:
    project_id = uuid.uuid4().hex[:12]
    display_title, extra = _episode_titles(episode_index, subtitle)
    return ProjectState(
        id=project_id,
        status="queued",
        progress=1,
        message="任务已创建，即将开始拆解。",
        created_at=time.time(),
        script=body.script.strip(),
        duration_seconds=body.duration_seconds,
        duration_auto=body.duration_auto,
        style=body.style.strip() or "电影级写实叙事",
        extra_notes=body.extra_notes.strip(),
        skip_images=body.skip_images,
        high_quality=body.high_quality,
        auto_generate_video=body.auto_generate_video,
        video_resolution=body.video_resolution or settings.minimax_resolution,
        video_ratio=body.video_ratio or settings.minimax_ratio,
        use_context_ir=body.use_context_ir if body.use_context_ir is not None else settings.minimax_use_context_ir,
        series_id=series.id,
        series_name=series.name,
        episode_index=episode_index,
        episode_title=display_title,
    )


@app.post("/api/projects", response_model=ProjectState)
async def create_project(body: CreateProjectRequest):
    if not settings.deepseek_api_key:
        raise HTTPException(500, "未配置 DeepSeek API Key")
    series = storage.resolve_series(body.series_id.strip(), body.series_name.strip())
    if not series:
        raise HTTPException(400, "请先创建或选择一个项目")
    episode_index = len(series.episodes) + 1
    state = _make_episode_state(body, series, episode_index, body.episode_title)
    pipeline.mark_project_active(state.id)
    storage.save_state(state)
    storage.append_episode(series, state.id, body.episode_title.strip() or storage.episode_cn_label(episode_index))

    async def _run():
        try:
            latest = storage.load_state(state.id)
            await pipeline.run_pipeline(latest, _publish)
        finally:
            pipeline.mark_project_inactive(state.id)

    _spawn(_run())
    return state


@app.post("/api/series/split", response_model=SplitSeriesResponse)
async def split_series(body: SplitSeriesRequest):
    if not settings.deepseek_api_key:
        raise HTTPException(500, "未配置 DeepSeek API Key")
    series = storage.resolve_series(body.series_id.strip(), body.series_name.strip())
    if not series:
        raise HTTPException(400, "请先创建或选择一个项目")
    try:
        parsed, source = await episodesplit.resolve_episodes(body.script, allow_llm=body.allow_llm)
    except Exception as exc:
        raise HTTPException(400, f"拆集失败：{exc}") from exc
    if not parsed:
        raise HTTPException(400, "没有拆出可用的剧集")

    created: list[ProjectState] = []
    for item in parsed:
        series = storage.load_series(series.id)
        episode_index = len(series.episodes) + 1
        ep_body = body.model_copy(
            update={
                "script": item.script,
                "duration_auto": True,
                "episode_title": item.title,
            }
        )
        state = _make_episode_state(ep_body, series, episode_index, item.title)
        state.message = f"全本已拆成 {len(parsed)} 集，排队中（{state.episode_title}）"
        storage.save_state(state)
        storage.append_episode(series, state.id, item.title or storage.episode_cn_label(episode_index))
        created.append(state)

    ids = [s.id for s in created]
    pipeline.mark_batch_active(ids)

    async def _run():
        try:
            await pipeline.run_pipeline_batch(ids, _publish)
        finally:
            pipeline.mark_batch_inactive(ids)

    _spawn(_run())
    return SplitSeriesResponse(
        series_id=series.id,
        source=source,
        episodes=[
            SplitEpisodeOut(
                id=s.id,
                episode_index=s.episode_index,
                episode_title=s.episode_title,
                title=parsed[i].title,
            )
            for i, s in enumerate(created)
        ],
    )


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str):
    try:
        storage.delete_episode_everywhere(project_id)
    except FileNotFoundError:
        raise HTTPException(404, "剧集不存在")
    return {"ok": True, "id": project_id}


@app.get("/api/projects/{project_id}", response_model=ProjectState)
async def get_project(project_id: str):
    try:
        state = _load_project(project_id)
    except FileNotFoundError:
        raise HTTPException(404, "项目不存在")
    if _needs_reconcile(state):
        state = await pipeline.reconcile_videos(state)
    busy = any(
        c.task_id
        and (
            c.video_status in {"queued", "running", "submitting"}
            or (c.video_status == "failed" and not c.video_filename)
        )
        for c in state.clips
    )
    if busy and project_id not in _video_syncing:
        async def _sync():
            _video_syncing.add(project_id)
            try:
                await pipeline.sync_videos(storage.load_state(project_id), _publish)
            finally:
                _video_syncing.discard(project_id)

        _spawn(_sync())
    return state


@app.get("/api/projects/{project_id}/events")
async def project_events(project_id: str):
    try:
        state = _load_project(project_id)
    except FileNotFoundError:
        raise HTTPException(404, "项目不存在")

    queue: asyncio.Queue = asyncio.Queue()
    async with _lock:
        _subscribers.setdefault(project_id, []).append(queue)

    async def gen():
        import json

        yield f"data: {json.dumps(state.model_dump(), ensure_ascii=False)}\n\n"
        try:
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    if payload.get("status") in {"done", "error"}:
                        pics = payload.get("pictures") or []
                        clips = payload.get("clips") or []
                        pic_busy = any(p.get("status") == "generating" for p in pics)
                        clip_busy = any(
                            c.get("video_status") in {"submitting", "queued", "running"}
                            for c in clips
                        )
                        if not pic_busy and not clip_busy:
                            break
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: {}\n\n"
        finally:
            async with _lock:
                subs = _subscribers.get(project_id, [])
                if queue in subs:
                    subs.remove(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/projects/{project_id}/retry")
async def retry_project(project_id: str):
    try:
        state = storage.load_state(project_id)
    except FileNotFoundError:
        raise HTTPException(404, "项目不存在")
    if state.status not in {"error", "queued", "analyzing"}:
        raise HTTPException(400, "当前状态不能重新生成")
    if pipeline.is_project_active(project_id) and state.status in {"queued", "analyzing"}:
        raise HTTPException(409, "本集正在生成，请稍候")
    state.status = "queued"
    state.error = ""
    state.progress = 2
    state.message = "正在重新拆解本集…"
    state.analysis = None
    state.pictures = []
    state.clips = []
    state.video_prompt = ""
    storage.drop_state_cache(project_id)
    storage.save_state(state)

    async def _run():
        latest = storage.load_state(project_id)
        await pipeline.run_pipeline(latest, _publish)

    _spawn(_run())
    return {"ok": True, "id": project_id}


@app.post("/api/projects/{project_id}/pictures/cancel")
async def cancel_imaging(project_id: str):
    try:
        state = storage.load_state(project_id)
    except FileNotFoundError:
        raise HTTPException(404, "项目不存在")
    if pipeline.is_project_active(project_id):
        now = time.time()
        stale = any(
            pic.status == "generating"
            and (
                not float(getattr(pic, "generating_since", 0) or 0)
                or now - float(pic.generating_since) >= pipeline.IMAGE_STALE_SECONDS
            )
            for pic in state.pictures or []
        )
        if not stale:
            raise HTTPException(409, "当前有一张参考图正在出图，请等待完成或超时后再停止队列")
        pipeline.mark_project_inactive(project_id)
    changed = pipeline.cancel_pending_imaging(state)
    if not changed:
        raise HTTPException(400, "当前没有可取消的参考图队列")
    storage.save_state(state)
    await _publish(state)
    return storage.load_state(project_id)


@app.post("/api/projects/{project_id}/pictures/{picture_index}/refetch")
async def refetch_picture(
    project_id: str,
    picture_index: int,
    body: RefetchPictureRequest = RefetchPictureRequest(),
):
    try:
        storage.load_state(project_id)
    except FileNotFoundError:
        raise HTTPException(404, "项目不存在")

    async def _run():
        latest = storage.load_state(project_id)
        await pipeline.refetch_picture(
            latest,
            picture_index,
            _publish,
            generate_if_missing=body.generate_if_missing,
        )

    _spawn(_run())
    return {"ok": True, "picture_index": picture_index}


@app.post("/api/projects/{project_id}/pictures/{picture_index}/regenerate")
async def regenerate_picture(project_id: str, picture_index: int, body: RegeneratePictureRequest = RegeneratePictureRequest()):
    try:
        storage.load_state(project_id)
    except FileNotFoundError:
        raise HTTPException(404, "项目不存在")
    prompt = body.prompt or ""

    async def _run():
        latest = storage.load_state(project_id)
        await pipeline.regenerate_picture(latest, picture_index, _publish, prompt=prompt)

    _spawn(_run())
    return {"ok": True, "picture_index": picture_index}


@app.post("/api/projects/{project_id}/pictures/{picture_index}/upload")
async def upload_picture(
    project_id: str,
    picture_index: int,
    file: UploadFile = File(...),
):
    try:
        storage.load_state(project_id)
    except FileNotFoundError:
        raise HTTPException(404, "项目不存在")
    data = await file.read()
    try:
        state = storage.load_state(project_id)
        await pipeline.replace_picture(
            state, picture_index, data, file.filename or "", _publish
        )
    except KeyError:
        raise HTTPException(404, "没有这张参考图")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return storage.load_state(project_id)


@app.post("/api/projects/{project_id}/pictures/{picture_index}/voice")
async def upload_character_voice(
    project_id: str,
    picture_index: int,
    file: UploadFile = File(...),
):
    try:
        storage.load_state(project_id)
    except FileNotFoundError:
        raise HTTPException(404, "项目不存在")
    data = await file.read()
    try:
        state = storage.load_state(project_id)
        await pipeline.replace_character_voice(
            state, picture_index, data, file.filename or "", _publish
        )
    except KeyError:
        raise HTTPException(404, "没有这张参考图")
    except Exception as exc:
        raise HTTPException(400, str(exc))
    return storage.load_state(project_id)


@app.post("/api/projects/{project_id}/pictures/{picture_index}/seedance-asset")
async def bind_seedance_asset(
    project_id: str,
    picture_index: int,
    body: BindSeedanceAssetRequest,
):
    try:
        state = storage.load_state(project_id)
    except FileNotFoundError:
        raise HTTPException(404, "项目不存在")
    try:
        state = pipeline.bind_seedance_asset(state, picture_index, body.asset_id)
        await _publish(state)
    except KeyError:
        raise HTTPException(404, "没有这张参考图")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return state


@app.post("/api/projects/{project_id}/seedance-assets")
async def ingest_seedance_assets(project_id: str):
    try:
        storage.load_state(project_id)
    except FileNotFoundError:
        raise HTTPException(404, "项目不存在")

    pipeline.mark_project_active(project_id)

    async def _run():
        latest = storage.load_state(project_id)
        latest.running = True
        try:
            await pipeline.ingest_seedance_character_assets(
                latest, _publish, continue_generate=False
            )
        except Exception as exc:
            latest = storage.load_state(project_id)
            pipeline._emit(latest, "retry", str(exc), latest.progress)
            storage.save_state(latest)
            await _publish(latest)
        finally:
            pipeline.mark_project_inactive(project_id)
            try:
                latest = storage.load_state(project_id)
                latest.running = False
                await _publish(latest)
            except FileNotFoundError:
                pass

    _spawn(_run())
    return {"ok": True}


@app.post("/api/projects/{project_id}/pictures/{picture_index}/voice")
async def upload_character_voice(
    project_id: str,
    picture_index: int,
    file: UploadFile = File(...),
):
    try:
        storage.load_state(project_id)
    except FileNotFoundError:
        raise HTTPException(404, "项目不存在")
    data = await file.read()
    try:
        state = storage.load_state(project_id)
        await pipeline.replace_character_voice(
            state, picture_index, data, file.filename or "", _publish
        )
    except KeyError:
        raise HTTPException(404, "没有这张参考图")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(400, str(exc))
    return storage.load_state(project_id)


@app.post("/api/projects/{project_id}/videos")
async def generate_videos(project_id: str, body: GenerateVideoRequest):
    try:
        state = storage.load_state(project_id)
    except FileNotFoundError:
        raise HTTPException(404, "项目不存在")
    if body.resolution:
        state.video_resolution = body.resolution
    if body.ratio:
        state.video_ratio = body.ratio
    if body.use_context_ir is not None:
        state.use_context_ir = body.use_context_ir
    if body.provider.strip():
        provider = body.provider.strip().lower()
        state.video_provider = "seedance" if provider == "seedance" else "minimax"
    if body.seedance_model.strip():
        state.seedance_model = seedance.normalize_model(body.seedance_model)
    if body.prompt.strip() and body.clip_index is not None:
        for clip in state.clips:
            if clip.index == body.clip_index:
                provider = (body.provider.strip().lower() or state.video_provider or "minimax")
                if provider == "seedance":
                    clip.seedance_prompt = body.prompt.strip()
                else:
                    clip.h3_prompt = body.prompt.strip()
                break
    storage.save_state(state)

    async def _run():
        latest = storage.load_state(project_id)
        await pipeline.generate_videos(
            latest, _publish, clip_index=body.clip_index, force=body.force
        )

    _spawn(_run())
    return {"ok": True, "clip_index": body.clip_index}


@app.post("/api/projects/{project_id}/videos/recover")
async def recover_videos(project_id: str, clip_index: int | None = None):
    try:
        storage.load_state(project_id)
    except FileNotFoundError:
        raise HTTPException(404, "项目不存在")

    async def _run():
        latest = storage.load_state(project_id)
        await pipeline.recover_videos(latest, _publish, clip_index=clip_index)

    _spawn(_run())
    return {"ok": True, "clip_index": clip_index}


@app.post("/api/projects/{project_id}/edit")
async def generate_edit(project_id: str, body: GenerateEditRequest = GenerateEditRequest()):
    try:
        storage.load_state(project_id)
    except FileNotFoundError:
        raise HTTPException(404, "项目不存在")

    async def _run():
        latest = storage.load_state(project_id)
        await pipeline.generate_edit(
            latest,
            _publish,
            dub=body.dub,
            subtitles=body.subtitles,
            remove_burned_subs=body.remove_burned_subs,
            ambience=body.ambience,
        )

    _spawn(_run())
    return {"ok": True, "dub": body.dub, "subtitles": body.subtitles}


@app.post("/api/projects/{project_id}/videos/{clip_index}/cancel")
async def cancel_clip_video(project_id: str, clip_index: int):
    try:
        state = storage.load_state(project_id)
    except FileNotFoundError:
        raise HTTPException(404, "项目不存在")
    clip = next((c for c in state.clips if c.index == clip_index), None)
    if not clip or not clip.task_id:
        raise HTTPException(404, "没有进行中的视频任务")
    provider = (clip.video_provider or state.video_provider or "minimax").strip().lower()
    try:
        if provider == "seedance":
            await seedance.cancel_task(clip.task_id)
        else:
            await video.cancel_task(clip.task_id)
    except Exception as exc:
        raise HTTPException(400, str(exc))
    clip.video_status = "cancelled"
    storage.save_state(state)
    return {"ok": True, "clip_index": clip_index}


@app.get("/api/projects/{project_id}/videos/{filename}")
async def get_video(project_id: str, filename: str):
    path = storage.video_dir(project_id) / filename
    if not path.exists() or path.parent.resolve() != storage.video_dir(project_id).resolve():
        raise HTTPException(404, "视频不存在")
    suffix = path.suffix.lower()
    media = {".mp4": "video/mp4", ".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".srt": "text/plain; charset=utf-8"}.get(suffix)
    return FileResponse(
        path,
        media_type=media,
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/api/projects/{project_id}/images/{filename}")
async def get_image(project_id: str, filename: str):
    path = storage.image_dir(project_id) / filename
    if not path.exists():
        raise HTTPException(404, "图片不存在")
    return FileResponse(path, headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/api/projects/{project_id}/export.zip")
async def export_zip(project_id: str):
    try:
        state = storage.load_state(project_id)
    except FileNotFoundError:
        raise HTTPException(404, "项目不存在")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("video_prompt.txt", state.video_prompt or "")
        for clip in state.clips:
            zf.writestr(f"clips/clip_{clip.index:02d}.txt", clip.h3_prompt or "")
        zf.writestr(
            "storyboard.json",
            state.model_dump_json(indent=2),
        )
        img_dir = storage.image_dir(project_id)
        for pic in state.pictures:
            path = img_dir / pic.filename
            if path.exists():
                zf.write(path, arcname=f"images/Picture_{pic.picture_index}_{pic.name}.png")
        vid_dir = storage.video_dir(project_id)
        for clip in state.clips:
            if clip.video_filename:
                vpath = vid_dir / clip.video_filename
                if vpath.exists():
                    zf.write(vpath, arcname=f"videos/{clip.video_filename}")
        if state.episode_video_filename:
            ep = vid_dir / state.episode_video_filename
            if ep.exists():
                zf.write(ep, arcname=f"videos/{state.episode_video_filename}")
        for name in (state.edit_filename, state.subtitle_filename, state.voiceover_filename):
            if not name:
                continue
            extra = vid_dir / name
            if extra.exists():
                zf.write(extra, arcname=f"videos/{name}")
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="storyboard_{project_id}.zip"'},
    )


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


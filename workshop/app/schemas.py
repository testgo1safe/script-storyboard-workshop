from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        parts = [_coerce_str(item).strip() for item in value]
        return "、".join(p for p in parts if p)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _coerce_str_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            text = _coerce_str(item).strip()
            if text:
                out.append(text)
        return out
    text = str(value).strip()
    return [text] if text else []


LooseStr = Annotated[str, BeforeValidator(_coerce_str)]
LooseStrList = Annotated[list[str], BeforeValidator(_coerce_str_list)]


class CreateEpisodeRequest(BaseModel):
    script: str = Field(..., min_length=8, description="剧本正文")
    duration_seconds: int = Field(30, ge=8, le=120)
    duration_auto: bool = True
    style: str = Field("竖屏短剧 · 电影级写实")
    extra_notes: str = ""
    skip_images: bool = False
    high_quality: bool = False
    auto_generate_video: bool = True
    video_resolution: str = "768P"
    video_ratio: str = "9:16"
    use_context_ir: bool = False
    series_id: str = ""
    series_name: str = ""
    episode_title: str = ""


class CreateProjectRequest(CreateEpisodeRequest):
    """兼容旧接口：一次提交即创建剧集。"""


class SplitSeriesRequest(CreateEpisodeRequest):
    allow_llm: bool = True


class SplitEpisodeOut(BaseModel):
    id: str
    episode_index: int
    episode_title: str
    title: str = ""


class SplitSeriesResponse(BaseModel):
    series_id: str
    source: str = "markers"
    episodes: list[SplitEpisodeOut] = Field(default_factory=list)


class CreateSeriesRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)


class RenameSeriesRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)


class RegeneratePictureRequest(BaseModel):
    prompt: str = ""


class RefetchPictureRequest(BaseModel):
    generate_if_missing: bool = False


class EstimateDurationRequest(BaseModel):
    script: str = Field(..., min_length=8)


class GenerateEditRequest(BaseModel):
    dub: bool = True
    subtitles: bool = True
    remove_burned_subs: bool = True
    ambience: bool = True


class BindSeedanceAssetRequest(BaseModel):
    asset_id: str = ""


class GenerateVideoRequest(BaseModel):
    clip_index: int | None = None
    resolution: str = ""
    ratio: str = ""
    use_context_ir: bool | None = None
    force: bool = False
    prompt: str = ""
    provider: str = ""
    seedance_model: str = ""


class Character(BaseModel):
    id: str
    name: str
    role: LooseStr = "主演"
    age: LooseStr = "成年"
    gender: LooseStr = ""
    ethnicity: LooseStr = ""
    appearance: LooseStr = ""
    wardrobe: LooseStr = ""
    signature_props: LooseStr = ""
    aliases: LooseStrList = Field(default_factory=list)
    emotion_curve: LooseStrList = Field(default_factory=list)
    image_prompt: LooseStr = ""


class Scene(BaseModel):
    id: str
    name: str
    time: LooseStr = ""
    location: LooseStr = ""
    atmosphere: LooseStr = ""
    lighting: LooseStr = ""
    lock_elements: LooseStrList = Field(default_factory=list)
    image_prompt: LooseStr = ""


class Prop(BaseModel):
    id: str = ""
    name: str
    description: LooseStr = ""
    lock_note: LooseStr = ""
    needs_reference: bool = True
    image_prompt: LooseStr = ""
    aliases: LooseStrList = Field(default_factory=list)


class PositionMark(BaseModel):
    name: LooseStr = ""
    side: LooseStr = ""
    facing: LooseStr = ""
    anchor: LooseStr = ""
    posture: LooseStr = ""


class Shot(BaseModel):
    start: float
    end: float
    beat: str
    shot_size: str = ""
    camera: str = ""
    camera_move: str = ""
    camera_speed: str = "slow"
    cut: str = "hard_cut"
    clip_index: int = 1
    start_state: LooseStr = ""
    process: LooseStr = ""
    result_state: LooseStr = ""
    performance: LooseStr = ""
    sound: LooseStrList = Field(default_factory=list)
    continuity: LooseStr = ""
    blocking: LooseStr = ""
    marks: list[PositionMark] = Field(default_factory=list)
    dialogue_speaker: str = ""
    dialogue_line: str = ""


class ClipPlan(BaseModel):
    index: int = 1
    title: str = ""
    hook: str = ""
    start: float = 0
    end: float = 15
    duration_seconds: int = 15
    marks: list[PositionMark] = Field(default_factory=list)


class VideoClip(BaseModel):
    index: int = 1
    title: str = ""
    hook: str = ""
    start: float = 0
    end: float = 15
    duration_seconds: int = 10
    picture_indices: list[int] = Field(default_factory=list)
    h3_prompt: str = ""
    seedance_prompt: str = ""
    director_notes: str = ""
    camera_lock: str = ""
    headcount_lock: str = ""
    scene_lock: str = ""
    lighting_lock: str = ""
    position_lock: str = ""
    video_status: Literal[
        "idle",
        "submitting",
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelled",
    ] = "idle"
    task_id: str = ""
    video_filename: str = ""
    error: str = ""
    estimated_remaining_seconds: int = 0
    resolution: str = ""
    ratio: str = ""
    video_provider: str = ""
    seedance_model: str = ""


class VisualBible(BaseModel):
    look: str = ""
    lens: str = "35mm 电影镜头"
    lighting: str = ""
    color: str = ""
    depth: str = "浅景深但保持人物空间关系"
    continuity_rules: LooseStrList = Field(default_factory=list)
    camera_grammar: LooseStrList = Field(default_factory=list)
    spatial_lock: LooseStr = ""
    marks: list[PositionMark] = Field(default_factory=list)


class Analysis(BaseModel):
    title: str = ""
    logline: str = ""
    duration_seconds: int = 30
    duration_reason: str = ""
    duration_auto: bool = True
    style: str = "竖屏短剧 · 电影级写实"
    aspect_ratio: str = "9:16"
    genre_anchor: str = ""
    entity_lock: LooseStr = ""
    complete_event: LooseStrList = Field(default_factory=list)
    visual: VisualBible = Field(default_factory=VisualBible)
    characters: list[Character] = Field(default_factory=list)
    scenes: list[Scene] = Field(default_factory=list)
    props: list[Prop] = Field(default_factory=list)
    shots: list[Shot] = Field(default_factory=list)
    clips: list[ClipPlan] = Field(default_factory=list)
    dialogue: LooseStrList = Field(default_factory=list)
    soundscape: LooseStrList = Field(default_factory=list)
    negatives: LooseStrList = Field(default_factory=list)


class PictureBinding(BaseModel):
    picture_index: int
    kind: Literal["character", "scene", "prop"]
    ref_id: str
    name: str
    lock_text: str
    filename: str = ""
    prompt: str = ""
    status: Literal["pending", "generating", "ready", "failed", "skipped"] = "pending"
    error: str = ""
    reused: bool = False
    generating_since: float = 0
    image_job_id: str = ""
    generated_prompt: str = ""
    source_url: str = ""
    source_path: str = ""
    verify_warning: str = ""
    image_stamp: str = ""
    seedance_asset_id: str = ""
    seedance_asset_hash: str = ""
    voice_filename: str = ""
    voice_source: Literal["", "upload", "extracted"] = ""
    voice_from_clip: int = 0
    seedance_audio_asset_id: str = ""
    seedance_audio_asset_hash: str = ""


class SeriesAsset(BaseModel):
    kind: Literal["character", "scene", "prop"]
    name: str
    lock_text: str = ""
    prompt: str = ""
    filename: str = ""
    appearance: str = ""
    wardrobe: str = ""
    description: str = ""
    aliases: list[str] = Field(default_factory=list)


class SeriesEpisode(BaseModel):
    project_id: str
    title: str = ""
    index: int = 0
    created_at: float = 0


class Series(BaseModel):
    id: str
    name: str
    created_at: float = 0
    assets: list[SeriesAsset] = Field(default_factory=list)
    episodes: list[SeriesEpisode] = Field(default_factory=list)
    compile_status: Literal["idle", "running", "succeeded", "failed"] = "idle"
    compile_filename: str = ""
    compile_error: str = ""
    compile_episodes: list[str] = Field(default_factory=list)


class ProjectEvent(BaseModel):
    ts: float
    stage: str
    message: str
    progress: int = 0
    extra: dict[str, Any] = Field(default_factory=dict)


class ProjectState(BaseModel):
    id: str
    status: Literal[
        "queued",
        "analyzing",
        "storyboard",
        "imaging",
        "prompting",
        "generating_video",
        "editing",
        "done",
        "error",
    ] = "queued"
    running: bool = False
    progress: int = 0
    message: str = ""
    created_at: float = 0
    script: str = ""
    duration_seconds: int = 30
    duration_auto: bool = True
    duration_reason: str = ""
    style: str = ""
    extra_notes: str = ""
    skip_images: bool = False
    high_quality: bool = False
    auto_generate_video: bool = True
    video_resolution: str = "768P"
    video_ratio: str = "9:16"
    use_context_ir: bool = False
    video_provider: str = "minimax"
    seedance_model: str = ""
    episode_video_filename: str = ""
    edit_status: Literal["idle", "running", "succeeded", "failed"] = "idle"
    edit_filename: str = ""
    edit_error: str = ""
    subtitle_filename: str = ""
    voiceover_filename: str = ""
    clips: list[VideoClip] = Field(default_factory=list)
    series_id: str = ""
    series_name: str = ""
    episode_index: int = 0
    episode_title: str = ""
    analysis: Analysis | None = None
    pictures: list[PictureBinding] = Field(default_factory=list)
    video_prompt: str = ""
    video_seedance_prompt: str = ""
    events: list[ProjectEvent] = Field(default_factory=list)
    error: str = ""


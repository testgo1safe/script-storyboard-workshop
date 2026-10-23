from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from . import clips as clip_lib
from .config import settings
from .schemas import Analysis, ProjectState, VideoClip


class EditError(RuntimeError):
    pass


EDIT_WIDTH = 720
EDIT_HEIGHT = 1280
_SUB_WRAP = 12


_NARRATION_RE = re.compile(r"系统|旁白|内心|^OS$|^VO$", re.I)
_MALE_RE = re.compile(r"男|male|叔|爷|哥|爸|父|弟|夫|少侠", re.I)
_FEMALE_RE = re.compile(r"女|female|姐|妈|母|姨|妹|奶|婆|嫂|姑娘", re.I)
_CHILD_RE = re.compile(r"童|孩|幼|少年|少女|小学生|初中|十几", re.I)
_ELDER_RE = re.compile(r"老|中年|大叔|大妈|五十|六十|七十", re.I)


@dataclass
class SoundEvent:
    start: float
    duration: float
    kind: str


@dataclass
class Cue:
    start: float
    end: float
    speaker: str
    text: str
    narration: bool = False


@dataclass
class VoiceStyle:
    speaker: str
    pitch: int = 0
    speech_rate: int = 0


def ffmpeg_bin() -> str:
    if settings.ffmpeg_path:
        return settings.ffmpeg_path
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def ffprobe_bin() -> str:
    found = shutil.which("ffprobe")
    return found or "ffprobe"


def probe_duration(path: Path) -> float:
    probe = ffprobe_bin()
    if shutil.which("ffprobe") or Path(probe).exists():
        cmd = [
            probe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ]
        try:
            out = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=20)
            return max(0.1, float((out.stdout or "0").strip() or 0))
        except Exception:
            pass
    cmd = [ffmpeg_bin(), "-i", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        blob = (out.stderr or "") + (out.stdout or "")
        match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", blob)
        if match:
            h, m, s = match.groups()
            return max(0.1, int(h) * 3600 + int(m) * 60 + float(s))
    except Exception:
        pass
    return 0.0


def video_has_audio(path: Path) -> bool:
    cmd = [ffmpeg_bin(), "-i", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        blob = (out.stderr or "") + (out.stdout or "")
        return bool(re.search(r"Audio:\s", blob))
    except Exception:
        return False


def probe_mean_volume(path: Path) -> float | None:
    cmd = [
        ffmpeg_bin(),
        "-i",
        str(path),
        "-af",
        "volumedetect",
        "-f",
        "null",
        "-",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        blob = (out.stderr or "") + (out.stdout or "")
        match = re.search(r"mean_volume:\s*([-\d.]+)\s*dB", blob)
        if match:
            return float(match.group(1))
    except Exception:
        return None
    return None


def extract_reference_audio(
    src: Path,
    dest: Path,
    *,
    start: float = 0.0,
    duration: float = 12.0,
) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    ss = max(0.0, float(start or 0))
    dur = max(2.0, min(12.0, float(duration or 12)))
    cmd = [
        ffmpeg_bin(),
        "-y",
        "-ss",
        f"{ss:.3f}",
        "-t",
        f"{dur:.3f}",
        "-i",
        str(src),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "24000",
        "-af",
        "highpass=f=80,dynaudnorm=f=150:g=7",
        "-b:a",
        "96k",
        str(dest),
    ]
    _run_ffmpeg(cmd, timeout=60)
    if not dest.exists() or dest.stat().st_size < 800:
        raise EditError("抽出的音轨是空的")
    return dest


def succeeded_clips(state: ProjectState) -> list[VideoClip]:
    return [
        c
        for c in sorted(state.clips, key=lambda x: x.index)
        if c.video_status == "succeeded" and c.video_filename
    ]


def clip_media_paths(state: ProjectState) -> list[Path]:
    from . import storage

    vdir = storage.video_dir(state.id)
    paths = []
    for clip in succeeded_clips(state):
        path = vdir / clip.video_filename
        if path.exists() and path.stat().st_size > 4096:
            paths.append(path)
    return paths


def _find_character(analysis: Analysis | None, name: str):
    if not analysis or not name:
        return None
    needle = name.strip()
    for char in analysis.characters:
        keys = {str(char.name or "").strip(), *[str(a).strip() for a in (char.aliases or []) if str(a).strip()]}
        if needle in keys:
            return char
        if any(needle in key or key in needle for key in keys if key):
            return char
    return None


def _char_blob(analysis: Analysis | None, name: str) -> str:
    char = _find_character(analysis, name)
    parts = [name or ""]
    if char:
        parts.extend(
            [
                str(char.role or ""),
                str(char.age or ""),
                str(char.gender or ""),
                str(char.appearance or ""),
                " ".join(str(a) for a in (char.aliases or [])),
            ]
        )
    return " ".join(parts)


def _age_bucket(blob: str) -> str:
    nums = [int(n) for n in re.findall(r"(\d{1,2})", blob) if 1 <= int(n) <= 90]
    if nums:
        age = nums[0]
        if age <= 14:
            return "child"
        if age >= 50:
            return "elder"
        if age <= 22:
            return "young"
    if _CHILD_RE.search(blob):
        return "child"
    if _ELDER_RE.search(blob):
        return "elder"
    if re.search(r"青年|大学|二十", blob):
        return "young"
    return "adult"


def doubao_configured() -> bool:
    return bool(settings.doubao_api_key)


_FEMALE_VOICES = (
    "zh_female_vv_uranus_bigtts",
    "zh_female_xiaohe_uranus_bigtts",
    "zh_female_xiaoxue_uranus_bigtts",
)
_MALE_VOICES = (
    "zh_male_m191_uranus_bigtts",
    "zh_male_taocheng_uranus_bigtts",
    "zh_male_ruyayichen_uranus_bigtts",
)


def voice_style_for(name: str, analysis: Analysis | None) -> VoiceStyle | None:
    if _NARRATION_RE.search(name or ""):
        return None
    blob = _char_blob(analysis, name)
    female = bool(_FEMALE_RE.search(blob))
    male = bool(_MALE_RE.search(blob))
    bucket = _age_bucket(blob)
    if female and not male:
        if bucket == "child":
            speaker = "zh_female_xiaoxue_uranus_bigtts"
        else:
            speaker = settings.doubao_tts_speaker or _FEMALE_VOICES[0]
    else:
        if bucket == "young" or bucket == "child":
            speaker = "zh_male_taocheng_uranus_bigtts"
        elif bucket == "elder":
            speaker = "zh_male_ruyayichen_uranus_bigtts"
        else:
            speaker = settings.doubao_tts_speaker_male or _MALE_VOICES[0]
    pitch = 0
    rate = 0
    if bucket == "child":
        pitch = 4
        rate = 6
    elif bucket == "young":
        pitch = 1
        rate = 4
    elif bucket == "elder":
        pitch = -2
        rate = -8
    return VoiceStyle(speaker=speaker, pitch=pitch, speech_rate=rate)


def speaker_id_for(name: str, analysis: Analysis | None) -> str:
    style = voice_style_for(name, analysis)
    return style.speaker if style else ""


_SPEECH_LEAD = 0.55
_SPEECH_GAP = 0.12
_SPEECH_TAIL = 0.22


def _line_hold(text: str) -> float:
    body = re.sub(r"\s+", "", text or "")
    return max(0.75, min(4.2, 0.38 + len(body) * 0.17))


def build_cues(state: ProjectState, clip_durations: list[float] | None = None) -> list[Cue]:
    analysis = state.analysis
    if not analysis:
        return []
    from . import storage

    vdir = storage.video_dir(state.id)
    cues: list[Cue] = []
    offset = 0.0
    clips = succeeded_clips(state)
    for i, clip in enumerate(clips):
        path = vdir / clip.video_filename
        duration = 0.0
        if clip_durations and i < len(clip_durations):
            duration = float(clip_durations[i] or 0)
        if duration <= 0.2:
            duration = probe_duration(path) or float(clip.duration_seconds or 0) or 8.0
        shots = clip_lib.shots_for_clip(analysis, clip)
        lines: list[tuple[str, str, bool]] = []
        for shot in shots:
            line = (shot.dialogue_line or "").replace("\\", "").strip()
            if not line:
                continue
            speaker = (shot.dialogue_speaker or "").strip()
            lines.append((speaker, line, bool(_NARRATION_RE.search(speaker))))
        if lines:
            holds = [_line_hold(text) for _speaker, text, _nar in lines]
            lead = min(max(_SPEECH_LEAD, duration * 0.1), 0.9)
            usable = max(0.7, duration - lead - _SPEECH_TAIL)
            need = sum(holds) + _SPEECH_GAP * max(0, len(holds) - 1)
            scale = usable / need if need > usable else 1.0
            t = lead
            if scale >= 1.0:
                t = lead + min(0.45, (usable - need) * 0.22)
            for (speaker, text, narration), hold in zip(lines, holds):
                dur = max(0.6, hold * scale)
                start = offset + min(duration - 0.35, t)
                end = offset + min(duration - 0.05, t + dur)
                if end - start < 0.5:
                    end = min(offset + duration, start + 0.8)
                cues.append(
                    Cue(
                        start=round(start, 3),
                        end=round(end, 3),
                        speaker=speaker,
                        text=text,
                        narration=narration,
                    )
                )
                t += dur + _SPEECH_GAP
        offset += duration
    return cues


_SOUND_KIND_RE = (
    ("alarm", re.compile(r"闹钟|铃声|闹铃|alarm|bell|ringtone", re.I)),
    ("footstep", re.compile(r"脚步|走路|奔跑|footstep", re.I)),
    ("ding", re.compile(r"电梯|提示音|ding", re.I)),
    ("engine", re.compile(r"引擎|发动机|轰鸣|engine", re.I)),
    ("tire", re.compile(r"轮胎|摩擦|刹车|tire|skid", re.I)),
    ("sizzle", re.compile(r"滋滋|油锅|早餐摊", re.I)),
    ("door", re.compile(r"开门|关门|门响|door", re.I)),
    ("wind", re.compile(r"风声|环境风|wind", re.I)),
    ("traffic", re.compile(r"车流|马路|街道|主干道|traffic", re.I)),
)


def classify_sound(text: str) -> str | None:
    raw = (text or "").strip()
    if not raw or raw in {"环境音", "环境声", "ambient"}:
        return None
    for kind, pattern in _SOUND_KIND_RE:
        if pattern.search(raw):
            return kind
    return None


def collect_sound_events(state: ProjectState, clip_durations: list[float] | None = None) -> list[SoundEvent]:
    analysis = state.analysis
    if not analysis:
        return []
    from . import storage

    vdir = storage.video_dir(state.id)
    events: list[SoundEvent] = []
    offset = 0.0
    clips = succeeded_clips(state)
    for i, clip in enumerate(clips):
        path = vdir / clip.video_filename
        duration = 0.0
        if clip_durations and i < len(clip_durations):
            duration = float(clip_durations[i] or 0)
        if duration <= 0.2:
            duration = probe_duration(path) or float(clip.duration_seconds or 0) or 8.0
        shots = clip_lib.shots_for_clip(analysis, clip)
        clip_start = shots[0].start if shots else 0.0
        for shot in shots:
            rel = max(0.0, float(shot.start or 0) - float(clip_start or 0))
            shot_len = max(0.4, float(shot.end or 0) - float(shot.start or 0) or duration)
            for raw in shot.sound or []:
                kind = classify_sound(str(raw))
                if not kind:
                    continue
                hold = {
                    "alarm": 2.6,
                    "footstep": min(3.2, shot_len),
                    "ding": 0.7,
                    "door": 0.4,
                    "tire": 0.8,
                }.get(kind, min(shot_len, duration - rel))
                start = offset + min(max(0.0, rel), max(0.0, duration - 0.12))
                events.append(SoundEvent(start=round(start, 3), duration=round(max(0.2, hold), 3), kind=kind))
        offset += duration
    deduped: list[SoundEvent] = []
    seen: set[tuple[str, int]] = set()
    for event in events:
        key = (event.kind, int(round(event.start * 5)))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(event)
    return deduped[:48]


def _srt_ts(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def write_srt(cues: list[Cue], dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    blocks = []
    for i, cue in enumerate(cues, start=1):
        text = _wrap_zh(cue.text, _SUB_WRAP, newline="\n")
        blocks.append(f"{i}\n{_srt_ts(cue.start)} --> {_srt_ts(cue.end)}\n{text}\n")
    dest.write_text("\n".join(blocks).strip() + "\n", encoding="utf-8")
    return dest


def _ass_ts(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h}:{m:02d}:{s:02d}.{milli // 10:02d}"


def _wrap_zh(text: str, width: int = 12, newline: str = r"\N") -> str:
    body = re.sub(r"\s+", "", (text or "").replace("\\", ""))
    if not body:
        return ""
    if len(body) <= width:
        return body
    if len(body) <= width * 2:
        mid = (len(body) + 1) // 2
        for i in range(mid, max(3, mid - 6), -1):
            if body[i - 1] in "，。！？、；,.:":
                return body[:i] + newline + body[i:]
        return body[:mid] + newline + body[mid:]
    lines = [body[i : i + width] for i in range(0, len(body), width)]
    return newline.join(lines[:2])


def _ass_escape(text: str) -> str:
    return (text or "").replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def write_ass(cues: list[Cue], dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {EDIT_WIDTH}
PlayResY: {EDIT_HEIGHT}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,微软雅黑,64,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0.4,0,1,5,1,2,40,40,120,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for cue in cues:
        if cue.narration:
            continue
        # 先转义原文，再插入 ASS 换行 \N；若先 wrap 再 escape，\N 会变成 \\N，字幕里会露出反斜杠。
        text = _wrap_zh(_ass_escape((cue.text or "").replace("\\", "")), _SUB_WRAP)
        if not text:
            continue
        events.append(
            f"Dialogue: 0,{_ass_ts(cue.start)},{_ass_ts(cue.end)},Default,,0,0,0,,{text}"
        )
    dest.write_text("\ufeff" + header + "\n".join(events) + "\n", encoding="utf-8")
    return dest


def parse_doubao_sse(payload: str) -> bytes:
    chunks: list[bytes] = []
    for line in payload.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            continue
        code = message.get("code", 0)
        if code not in (0, 20_000_000, None):
            if code == 45_000_030:
                raise EditError("豆包语音资源未开通，请在控制台开通语音合成 2.0")
            raise EditError(f"豆包语音错误 {code}: {message.get('message') or 'unknown'}")
        data = message.get("data")
        if data:
            chunks.append(base64.b64decode(data))
    if not chunks:
        raise EditError("豆包语音没有返回音频")
    return b"".join(chunks)


async def synthesize_line(text: str, speaker: str, dest: Path, *, pitch: int = 0, speech_rate: int = 0) -> Path:
    if not settings.doubao_api_key:
        raise EditError("未配置 DOUBAO_API_KEY")
    dest.parent.mkdir(parents=True, exist_ok=True)
    request_id = str(uuid.uuid4())
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "X-Api-Key": settings.doubao_api_key,
        "X-Api-Resource-Id": settings.doubao_tts_resource_id,
        "X-Api-Request-Id": request_id,
    }
    body = {
        "user": {"uid": "chaifen-studio"},
        "req_params": {
            "text": text,
            "speaker": speaker or settings.doubao_tts_speaker,
            "sample_rate": 24000,
            "audio_params": {
                "format": "mp3",
                "speech_rate": int(speech_rate or 0),
                "loudness_rate": 0,
                "bit_rate": 64000,
            },
            "additions": json.dumps(
                {
                    "post_process": {"pitch": int(pitch or 0)},
                    "disable_markdown_filter": False,
                    "enable_latex_tn": False,
                },
                ensure_ascii=False,
            ),
        },
    }
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(settings.doubao_tts_endpoint, headers=headers, json=body)
        if resp.status_code >= 400:
            raise EditError(f"豆包语音请求失败 ({resp.status_code}): {resp.text[:400]}")
        audio = parse_doubao_sse(resp.text)
    dest.write_bytes(audio)
    return dest


def _voice_fallbacks(style: VoiceStyle) -> list[str]:
    primary = style.speaker or settings.doubao_tts_speaker
    female = primary in _FEMALE_VOICES or "female" in primary
    pool = list(_FEMALE_VOICES if female else _MALE_VOICES)
    ordered = [primary, *pool, settings.doubao_tts_speaker, *_FEMALE_VOICES]
    seen: set[str] = set()
    result: list[str] = []
    for item in ordered:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


async def synthesize_line_resilient(text: str, style: VoiceStyle, dest: Path) -> Path:
    last: Exception | None = None
    for speaker in _voice_fallbacks(style):
        try:
            return await synthesize_line(
                text, speaker, dest, pitch=style.pitch, speech_rate=style.speech_rate
            )
        except Exception as exc:
            last = exc
            msg = str(exc).lower()
            if "55000000" in msg or "mismatched" in msg:
                continue
            raise
    raise last or EditError("豆包配音失败")


def _run_ffmpeg(cmd: list[str], timeout: int = 240) -> None:
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or b"").decode("utf-8", errors="ignore")[-800:]
        raise EditError(err or str(exc)) from exc
    except FileNotFoundError as exc:
        raise EditError("未找到 ffmpeg，请先安装并加入 PATH") from exc


def _fit_vertical_filter() -> str:
    return (
        f"scale={EDIT_WIDTH}:{EDIT_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={EDIT_WIDTH}:{EDIT_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,"
        "setsar=1,fps=30"
    )


def normalize_clip(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin(),
        "-y",
        "-i",
        str(src),
        "-vf",
        _fit_vertical_filter(),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        str(dest),
    ]
    try:
        _run_ffmpeg(cmd)
    except EditError:
        cmd = [
            ffmpeg_bin(),
            "-y",
            "-i",
            str(src),
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-vf",
            _fit_vertical_filter(),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(dest),
        ]
        _run_ffmpeg(cmd)
    return dest


def concat_reencode(paths: list[Path], dest: Path) -> tuple[Path, list[float]]:
    if not paths:
        raise EditError("没有可剪辑的成片段")
    dest.parent.mkdir(parents=True, exist_ok=True)
    work = dest.parent
    normalized: list[Path] = []
    durations: list[float] = []
    for i, path in enumerate(paths, start=1):
        out = work / f"norm_{i:02d}.mp4"
        normalize_clip(path, out)
        normalized.append(out)
        durations.append(probe_duration(out) or probe_duration(path) or 8.0)
    if len(normalized) == 1:
        shutil.copy2(normalized[0], dest)
        return dest, durations
    list_file = work / "edit_concat.txt"
    list_file.write_text(
        "".join(f"file '{p.resolve().as_posix()}'\n" for p in normalized),
        encoding="utf-8",
    )
    cmd = [
        ffmpeg_bin(),
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c",
        "copy",
        str(dest),
    ]
    try:
        _run_ffmpeg(cmd)
    except EditError:
        cmd[-3:-1] = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"]
        _run_ffmpeg(cmd)
    return dest, durations


def _escape_ffmpeg_path(path: Path) -> str:
    return path.resolve().as_posix().replace("\\", "/").replace(":", "\\:").replace("'", r"\'")


def _subtitle_filter(ass: Path) -> str:
    return f"ass='{_escape_ffmpeg_path(ass)}'"


def burn_subtitles(src: Path, ass: Path, dest: Path) -> Path:
    cmd = [
        ffmpeg_bin(),
        "-y",
        "-i",
        str(src),
        "-vf",
        _subtitle_filter(ass),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "copy",
        str(dest),
    ]
    _run_ffmpeg(cmd, timeout=300)
    return dest


def burned_subtitle_mask(bgr) -> "object":
    import cv2
    import numpy as np

    h, w = bgr.shape[:2]
    mask = np.zeros((h, w), np.uint8)
    band_top = int(h * 0.48)
    band_bot = int(h * 0.92)
    roi = bgr[band_top:band_bot]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    white = cv2.inRange(hsv, (0, 0, 185), (180, 70, 255))
    yellow = cv2.inRange(hsv, (18, 50, 160), (42, 255, 255))
    bright = cv2.bitwise_or(white, yellow)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(bright, 8)
    filtered = np.zeros_like(bright)
    rh, rw = bright.shape[:2]
    for i in range(1, n):
        _x, _y, bw, bh, area = stats[i]
        if area < 18 or area > 0.12 * rh * rw:
            continue
        if bw > 0.55 * rw and bh > 0.28 * rh:
            continue
        if bh > 90:
            continue
        filtered[labels == i] = 255
    closed = cv2.morphologyEx(
        filtered,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (max(11, w // 40), 5)),
    )
    n, labels, stats, _ = cv2.connectedComponentsWithStats(closed, 8)
    line_boxes = []
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if bw < 0.18 * rw or bh < 14 or bh > 96 or area < 80:
            continue
        if bw / max(bh, 1) < 2.2:
            continue
        cx = x + bw / 2
        if cx < rw * 0.18 or cx > rw * 0.82:
            continue
        full_cy = band_top + y + bh / 2
        if full_cy > h * 0.84:
            continue
        line_boxes.append((x, y, bw, bh))
    if not line_boxes:
        return mask
    dark = cv2.inRange(gray, 0, 80)
    near = cv2.dilate(filtered, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    strokes = cv2.bitwise_or(filtered, cv2.bitwise_and(dark, near))
    glyphs = np.zeros_like(filtered)
    pad = 6
    for x, y, bw, bh in line_boxes:
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(rw, x + bw + pad), min(rh, y + bh + pad)
        glyphs[y0:y1, x0:x1] = strokes[y0:y1, x0:x1]
    glyphs = cv2.dilate(glyphs, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)))
    mask[band_top:band_bot] = glyphs
    return mask


def _sample_has_burned_subtitles(src: Path, samples: int = 10) -> bool:
    try:
        import cv2
    except ImportError:
        return False
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        return False
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total <= 0:
            ok, frame = cap.read()
            return bool(ok and burned_subtitle_mask(frame).any())
        hits = 0
        for i in range(samples):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * i / max(samples, 1)))
            ok, frame = cap.read()
            if ok and burned_subtitle_mask(frame).any():
                hits += 1
                if hits >= 2:
                    return True
        return False
    finally:
        cap.release()


def remove_burned_subtitles(src: Path, dest: Path) -> bool:
    """Detect H3 baked-in captions and inpaint them. Returns True if any frame was cleaned."""
    try:
        import cv2
    except ImportError:
        shutil.copy2(src, dest)
        return False

    if not _sample_has_burned_subtitles(src):
        shutil.copy2(src, dest)
        return False

    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        shutil.copy2(src, dest)
        return False
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0) or 30.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if width < 32 or height < 32:
        cap.release()
        shutil.copy2(src, dest)
        return False

    tmp = dest.parent / f"{dest.stem}_nosub_v.mp4"
    writer = cv2.VideoWriter(str(tmp), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    cleaned = 0
    frames = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frames += 1
            mask = burned_subtitle_mask(frame)
            if mask.any():
                frame = cv2.inpaint(frame, mask, 2, cv2.INPAINT_NS)
                cleaned += 1
            writer.write(frame)
    finally:
        cap.release()
        writer.release()

    if frames == 0 or cleaned == 0:
        if tmp.exists():
            tmp.unlink()
        shutil.copy2(src, dest)
        return False

    cmd = [
        ffmpeg_bin(),
        "-y",
        "-i",
        str(tmp),
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(dest),
    ]
    try:
        _run_ffmpeg(cmd, timeout=300)
    except EditError:
        cmd = [
            ffmpeg_bin(),
            "-y",
            "-i",
            str(tmp),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(dest),
        ]
        _run_ffmpeg(cmd, timeout=300)
    tmp.unlink(missing_ok=True)
    return True


def _has_audio_stream(path: Path) -> bool:
    probe = ffprobe_bin()
    cmd = [
        probe,
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "csv=p=0",
        str(path),
    ]
    try:
        out = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=20)
        return "audio" in (out.stdout or "").lower()
    except Exception:
        return False


def _attach_dub(video: Path, dub: Path, dest: Path) -> Path:
    cmd = [
        ffmpeg_bin(),
        "-y",
        "-i",
        str(video),
        "-i",
        str(dub),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-shortest",
        str(dest),
    ]
    _run_ffmpeg(cmd, timeout=240)
    return dest


def mix_dub(video: Path, dub: Path, dest: Path, original_volume: float = 0.22) -> Path:
    return mix_audio_layers(video, [(dub, 1.15)], dest, original_volume=original_volume)


def mix_audio_layers(
    video: Path,
    tracks: list[tuple[Path, float]],
    dest: Path,
    original_volume: float = 0.22,
) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    usable = [(path, vol) for path, vol in tracks if path and path.exists() and path.stat().st_size > 32]
    if not usable:
        shutil.copy2(video, dest)
        return dest
    if not _has_audio_stream(video) and len(usable) == 1:
        return _attach_dub(video, usable[0][0], dest)
    cmd = [ffmpeg_bin(), "-y", "-i", str(video)]
    parts: list[str] = []
    labels: list[str] = []
    next_idx = 1
    if _has_audio_stream(video) and original_volume > 0:
        parts.append(f"[0:a]volume={original_volume},aformat=sample_fmts=fltp[orig]")
        labels.append("[orig]")
    for path, vol in usable:
        cmd.extend(["-i", str(path)])
        parts.append(f"[{next_idx}:a]volume={vol},aformat=sample_fmts=fltp[t{next_idx}]")
        labels.append(f"[t{next_idx}]")
        next_idx += 1
    mix = "".join(labels) + f"amix=inputs={len(labels)}:duration=first:dropout_transition=2:normalize=0[a]"
    parts.append(mix)
    cmd.extend(
        [
            "-filter_complex",
            ";".join(parts),
            "-map",
            "0:v:0",
            "-map",
            "[a]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-shortest",
            str(dest),
        ]
    )
    try:
        _run_ffmpeg(cmd, timeout=240)
        return dest
    except EditError:
        return _attach_dub(video, usable[0][0], dest)


def _sfx_filter(kind: str, duration: float) -> str:
    d = max(0.15, min(float(duration), 36.0))
    if kind == "alarm":
        return (
            "sine=frequency=980:sample_rate=48000:duration=0.11,afade=t=out:st=0.07:d=0.04,"
            f"aloop=loop=16:size=5280,atrim=0:{d:.3f},volume=0.4,aformat=channel_layouts=stereo"
        )
    if kind == "footstep":
        return (
            "anoisesrc=color=brown:amplitude=0.55:sample_rate=48000:duration=0.07,"
            "afade=t=in:d=0.008,afade=t=out:st=0.04:d=0.03,apad=whole_dur=0.42,"
            f"aloop=loop=10:size=20160,atrim=0:{d:.3f},lowpass=f=380,volume=0.48,aformat=channel_layouts=stereo"
        )
    if kind == "ding":
        return (
            "sine=frequency=1319:sample_rate=48000:duration=0.45,afade=t=out:st=0.06:d=0.39,"
            "volume=0.36,aformat=channel_layouts=stereo"
        )
    if kind == "door":
        return (
            "anoisesrc=color=brown:amplitude=0.7:sample_rate=48000:duration=0.28,"
            "lowpass=f=280,afade=t=out:st=0.1:d=0.18,volume=0.5,aformat=channel_layouts=stereo"
        )
    if kind == "engine":
        return (
            f"anoisesrc=color=brown:amplitude=0.45:sample_rate=48000:duration={d:.3f},"
            "lowpass=f=160,volume=0.55,aformat=channel_layouts=stereo"
        )
    if kind == "tire":
        return (
            "anoisesrc=color=white:amplitude=0.35:sample_rate=48000:duration=0.7,"
            "highpass=f=700,lowpass=f=3200,afade=t=in:d=0.05,afade=t=out:st=0.4:d=0.3,"
            "volume=0.32,aformat=channel_layouts=stereo"
        )
    if kind == "sizzle":
        return (
            f"anoisesrc=color=white:amplitude=0.18:sample_rate=48000:duration={d:.3f},"
            "highpass=f=3500,volume=0.22,aformat=channel_layouts=stereo"
        )
    if kind == "traffic":
        return (
            f"anoisesrc=color=pink:amplitude=0.22:sample_rate=48000:duration={d:.3f},"
            "lowpass=f=450,volume=0.38,aformat=channel_layouts=stereo"
        )
    if kind == "wind":
        return (
            f"anoisesrc=color=brown:amplitude=0.28:sample_rate=48000:duration={d:.3f},"
            "highpass=f=180,lowpass=f=1600,volume=0.36,aformat=channel_layouts=stereo"
        )
    return (
        f"anoisesrc=color=pink:amplitude=0.07:sample_rate=48000:duration={d:.3f},"
        "lowpass=f=700,volume=0.3,aformat=channel_layouts=stereo"
    )


def _render_sfx_clip(kind: str, duration: float, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin(),
        "-y",
        "-f",
        "lavfi",
        "-i",
        _sfx_filter(kind, duration),
        "-t",
        f"{max(0.15, duration):.3f}",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        str(dest),
    ]
    _run_ffmpeg(cmd, timeout=60)
    return dest


def build_ambience_track(events: list[SoundEvent], dest: Path, duration: float, work: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    duration = max(1.0, float(duration))
    pieces: list[tuple[SoundEvent, Path]] = []
    room = work / "sfx_room.m4a"
    _render_sfx_clip("room", duration, room)
    pieces.append((SoundEvent(start=0.0, duration=duration, kind="room"), room))
    for i, event in enumerate(events, start=1):
        clip = work / f"sfx_{i:02d}_{event.kind}.m4a"
        try:
            _render_sfx_clip(event.kind, event.duration, clip)
        except Exception:
            continue
        pieces.append((event, clip))
    cmd = [ffmpeg_bin(), "-y"]
    filter_parts: list[str] = [f"anullsrc=r=48000:cl=stereo,atrim=0:{duration:.3f},aformat=sample_fmts=fltp[base]"]
    labels = ["[base]"]
    for i, (event, path) in enumerate(pieces):
        cmd.extend(["-i", str(path)])
        delay = max(0, int(round(event.start * 1000)))
        vol = 0.55 if event.kind == "room" else 1.0
        filter_parts.append(
            f"[{i}]volume={vol},adelay={delay}:all=1,aformat=sample_fmts=fltp[s{i}]"
        )
        labels.append(f"[s{i}]")
    n = len(labels)
    filter_parts.append(
        "".join(labels) + f"amix=inputs={n}:duration=first:dropout_transition=0:normalize=0[out]"
    )
    cmd.extend(
        ["-filter_complex", ";".join(filter_parts), "-map", "[out]", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-t", f"{duration:.3f}", str(dest)]
    )
    _run_ffmpeg(cmd, timeout=240)
    return dest


def build_dub_track(files: list[tuple[Cue, Path]], dest: Path, duration: float) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not files:
        raise EditError("没有配音文件")
    cmd = [ffmpeg_bin(), "-y"]
    filter_parts: list[str] = [f"anullsrc=r=24000:cl=mono,atrim=0:{duration:.3f},aformat=sample_fmts=fltp[base]"]
    labels = ["[base]"]
    for i, (cue, path) in enumerate(files):
        cmd.extend(["-i", str(path)])
        delay = max(0, int(round(cue.start * 1000)))
        filter_parts.append(
            f"[{i}]adelay={delay}:all=1,aformat=sample_fmts=fltp[d{i}]"
        )
        labels.append(f"[d{i}]")
    n = len(labels)
    filter_parts.append("".join(labels) + f"amix=inputs={n}:duration=first:dropout_transition=0[out]")
    cmd.extend(["-filter_complex", ";".join(filter_parts), "-map", "[out]", "-c:a", "aac", "-ar", "48000", str(dest)])
    _run_ffmpeg(cmd, timeout=240)
    return dest


def ffmpeg_available() -> bool:
    exe = ffmpeg_bin()
    return Path(exe).exists() or bool(shutil.which("ffmpeg"))


async def render_episode_edit(
    state: ProjectState,
    *,
    dub: bool,
    subtitles: bool,
    remove_burned_subs: bool = True,
    ambience: bool = True,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
) -> dict:
    from . import storage

    async def note(message: str) -> None:
        if on_progress:
            await on_progress(message)

    paths = clip_media_paths(state)
    if not paths:
        raise EditError("还没有成功的成片段，请先出片")
    vdir = storage.video_dir(state.id)
    work = vdir / "edit_work"
    work.mkdir(exist_ok=True)
    concat = work / "concat.mp4"
    await note("正在自动剪辑：拼接片段…")
    _concat, clip_durations = concat_reencode(paths, concat)
    cues = build_cues(state, clip_durations)
    srt = write_srt(cues, vdir / "episode.srt") if (subtitles and cues) else None
    ass = write_ass(cues, work / "episode.ass") if (subtitles and cues) else None

    current = concat
    removed_burned = False
    use_wipe = remove_burned_subs and (state.video_provider or "").strip().lower() != "seedance"
    if use_wipe:
        await note("正在自动剪辑：检查并去除模型字幕…")
        cleaned = work / "concat_clean.mp4"
        try:
            removed_burned = remove_burned_subtitles(concat, cleaned)
            if removed_burned and cleaned.exists():
                current = cleaned
        except Exception:
            removed_burned = False
    voice_path: Path | None = None
    dubbed = 0
    dub_error = ""
    amb_path: Path | None = None
    sfx_count = 0
    duration = probe_duration(concat) or 30.0
    if cues:
        duration = max(duration, cues[-1].end + 0.4)

    if ambience:
        await note("正在自动剪辑：铺环境音…")
        try:
            events = collect_sound_events(state, clip_durations)
            amb_path = vdir / "ambience.m4a"
            build_ambience_track(events, amb_path, duration, work)
            sfx_count = len(events)
        except Exception:
            amb_path = None

    if dub and not doubao_configured():
        dub_error = "未配置豆包配音密钥（workshop/.env 或 video.env 的 DOUBAO_API_KEY）"
    elif dub and doubao_configured() and not cues:
        dub_error = "分镜里没有对白，无法配音"
    elif dub and doubao_configured() and cues:
        spoken = [c for c in cues if not c.narration]
        files: list[tuple[Cue, Path]] = []
        errors: list[str] = []
        for i, cue in enumerate(spoken, start=1):
            style = voice_style_for(cue.speaker, state.analysis)
            if not style:
                continue
            await note(f"正在自动剪辑：配音 {i}/{len(spoken)} {cue.speaker or ''}…")
            out = work / f"line_{i:02d}.mp3"
            try:
                await synthesize_line_resilient(cue.text, style, out)
            except Exception as exc:
                errors.append(f"{cue.speaker or '角色'}：{exc}")
                continue
            files.append((cue, out))
        if files:
            await note("正在自动剪辑：叠配音轨…")
            voice_path = vdir / "dub.m4a"
            try:
                build_dub_track(files, voice_path, duration)
                dubbed = len(files)
            except Exception as exc:
                dub_error = f"配音合成了 {len(files)} 句，但叠到视频失败：{exc}"
                voice_path = voice_path if voice_path.exists() else None
        elif spoken:
            dub_error = "配音失败：" + (errors[0] if errors else "没有可用声线")
        if errors and dubbed:
            dub_error = f"配上 {dubbed} 句，另有 {len(errors)} 句失败"

    tracks: list[tuple[Path, float]] = []
    if voice_path and voice_path.exists():
        tracks.append((voice_path, 1.2))
    if amb_path and amb_path.exists():
        tracks.append((amb_path, 0.7))
    if tracks:
        await note("正在自动剪辑：混合音轨…")
        mixed = work / "mixed.mp4"
        try:
            mix_audio_layers(current, tracks, mixed)
            current = mixed
        except Exception as exc:
            if not dub_error:
                dub_error = f"音轨叠加失败：{exc}"

    final = vdir / "episode_edit.mp4"
    if ass and ass.exists() and ass.stat().st_size > 8:
        await note("正在自动剪辑：烧字幕…")
        try:
            burn_subtitles(current, ass, final)
        except Exception:
            shutil.copy2(current, final)
    else:
        shutil.copy2(current, final)

    return {
        "filename": final.name,
        "subtitle": srt.name if srt else "",
        "voiceover": voice_path.name if voice_path else "",
        "cues": len(cues),
        "dubbed": dubbed,
        "removed_burned": removed_burned,
        "dub_error": dub_error,
        "sfx": sfx_count,
    }


def episode_export_path(state: ProjectState) -> Path | None:
    from . import storage

    vdir = storage.video_dir(state.id)
    names = [state.edit_filename, state.episode_video_filename, "episode_edit.mp4", "episode.mp4"]
    seen: set[str] = set()
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        path = vdir / name
        if path.exists() and path.stat().st_size > 4096:
            return path
    return None


def collect_series_parts(series_id: str) -> list[tuple[str, Path]]:
    from . import storage

    series = storage.load_series(series_id)
    parts: list[tuple[str, Path]] = []
    for ep in sorted(series.episodes, key=lambda item: item.index or 0):
        try:
            state = storage.load_state(ep.project_id)
        except FileNotFoundError:
            continue
        path = episode_export_path(state)
        if not path:
            continue
        label = storage.episode_cn_label(ep.index or len(parts) + 1)
        extra = (state.episode_title or ep.title or "").strip()
        extra = re.sub(r"^第\s*[0-9一二三四五六七八九十百]+\s*集\s*[·\-—:：]?\s*", "", extra)
        title = f"{label} · {extra}" if extra and extra != label else label
        parts.append((title, path))
    return parts


def compile_series(series_id: str) -> dict:
    from . import storage

    parts = collect_series_parts(series_id)
    if not parts:
        raise EditError("还没有可拼接的剧集成片。请先把各集出片或自动剪辑。")
    vdir = storage.series_video_dir(series_id)
    work = vdir / "compile_work"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    dest = vdir / "series_full.mp4"
    concat_reencode([path for _title, path in parts], work / "series_full.mp4")
    shutil.copy2(work / "series_full.mp4", dest)
    return {
        "filename": dest.name,
        "episodes": [title for title, _path in parts],
        "count": len(parts),
    }


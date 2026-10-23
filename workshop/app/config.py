from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
PROJECTS_DIR = DATA_DIR / "projects"
SERIES_DIR = DATA_DIR / "series"
WEB_DIR = ROOT_DIR / "web"
VIDEO_ENV_CANDIDATES = (
    ROOT_DIR / ".env",
    ROOT_DIR / "video.env",
    Path(r"E:\VIDeO") / ".env",
    Path(r"E:\video") / ".env",
    ROOT_DIR.parent / "VIDeO" / ".env",
    ROOT_DIR.parent / "video" / ".env",
)


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _video_env() -> tuple[dict[str, str], Path | None]:
    for path in VIDEO_ENV_CANDIDATES:
        data = _read_env_file(path)
        if data.get("DOUBAO_API_KEY"):
            return data, path
    return {}, None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    deepseek_prompt_model: str = "deepseek-chat"

    image_api_base: str = "http://127.0.0.1:8765"
    image_api_key: str = "local-dev-key"

    minimax_api_key: str = ""
    minimax_base_url: str = "https://cp.compshare.cn"
    minimax_model: str = "MiniMax-H3"
    minimax_resolution: str = "768P"
    minimax_ratio: str = "9:16"
    minimax_use_context_ir: bool = False

    seedance_api_key: str = ""
    seedance_base_url: str = "https://toapis.cn"
    seedance_model: str = "seedance-2-fast"
    volcengine_access_key: str = ""
    volcengine_secret_key: str = ""
    volcengine_region: str = "cn-beijing"
    ark_project_name: str = "default"

    doubao_api_key: str = ""
    doubao_tts_speaker: str = "zh_female_vv_uranus_bigtts"
    doubao_tts_speaker_male: str = "zh_male_m191_uranus_bigtts"
    doubao_tts_resource_id: str = "seed-tts-2.0"
    doubao_tts_endpoint: str = "https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse"
    ffmpeg_path: str = ""

    host: str = "127.0.0.1"
    port: int = 8000


def _apply_video_keys(s: Settings, video: dict[str, str]) -> Settings:
    """豆包配音：优先本目录 .env / video.env，没有再读 E:\\VIDeO\\.env。"""
    if video.get("DOUBAO_API_KEY"):
        s.doubao_api_key = video["DOUBAO_API_KEY"]
    if video.get("DOUBAO_TTS_SPEAKER"):
        s.doubao_tts_speaker = video["DOUBAO_TTS_SPEAKER"]
    if video.get("DOUBAO_TTS_SPEAKER_MALE"):
        s.doubao_tts_speaker_male = video["DOUBAO_TTS_SPEAKER_MALE"]
    if video.get("DOUBAO_TTS_RESOURCE_ID"):
        s.doubao_tts_resource_id = video["DOUBAO_TTS_RESOURCE_ID"]
    if video.get("DOUBAO_TTS_ENDPOINT"):
        s.doubao_tts_endpoint = video["DOUBAO_TTS_ENDPOINT"]
    ak = (
        video.get("VOLCENGINE_ACCESS_KEY")
        or video.get("VOLC_ACCESS_KEY")
        or video.get("ACCESS_KEY_ID")
        or ""
    )
    sk = (
        video.get("VOLCENGINE_SECRET_KEY")
        or video.get("VOLC_SECRET_KEY")
        or video.get("SECRET_ACCESS_KEY")
        or ""
    )
    if ak and not s.volcengine_access_key:
        s.volcengine_access_key = ak
    if sk and not s.volcengine_secret_key:
        s.volcengine_secret_key = sk
    if video.get("ARK_PROJECT_NAME") and not s.ark_project_name:
        s.ark_project_name = video["ARK_PROJECT_NAME"]
    return s


_VIDEO_ENV, VIDEO_ENV_PATH = _video_env()
settings = _apply_video_keys(Settings(), _VIDEO_ENV)
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
SERIES_DIR.mkdir(parents=True, exist_ok=True)


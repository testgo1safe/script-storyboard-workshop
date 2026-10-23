from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import hmac
import io
import json
import logging
import re
from pathlib import Path
from urllib.parse import quote

import httpx
from PIL import Image

from .config import DATA_DIR, settings

logger = logging.getLogger(__name__)

ARK_HOST = "open.volcengineapi.com"
ARK_SERVICE = "ark"
ARK_VERSION = "2024-01-01"
CACHE_FILE = DATA_DIR / "seedance_assets.json"


class ArkAssetError(RuntimeError):
    pass


def _friendly_api_error(action: str, code: str, message: str) -> str:
    text = f"{action} {code} {message}".strip()
    code_l = (code or "").lower()
    msg_l = (message or "").lower()
    if "subscriptionrequired" in code_l or "active subscription" in msg_l:
        return (
            f"{text}。账号还没开通方舟素材库（虚拟人像）能力，"
            "请到火山方舟控制台开通高级版/旗舰版或素材库相关套餐后再入库。"
            "入口：方舟控制台 → 素材管理 / 虚拟人像，按页面提示订阅。"
        )
    if "missingparameter.filter" in code_l or "parameter filter is missing" in msg_l:
        return f"{text}。查询素材组必须带 Filter（例如 GroupType=AIGC）。"
    return text


def assets_configured() -> bool:
    return bool(settings.volcengine_access_key and settings.volcengine_secret_key)


def _project_name() -> str:
    return (settings.ark_project_name or "default").strip() or "default"


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def image_data_url(path: Path, max_side: int = 1280, quality: int = 85) -> str:
    img = Image.open(path)
    if img.mode not in {"RGB", "L"}:
        img = img.convert("RGB")
    elif img.mode == "L":
        img = img.convert("RGB")
    w, h = img.size
    scale = min(1.0, max_side / max(w, h, 1))
    if scale < 1:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {"groups": {}, "hashes": {}}
    try:
        raw = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"groups": {}, "hashes": {}}
    if not isinstance(raw, dict):
        return {"groups": {}, "hashes": {}}
    raw.setdefault("groups", {})
    raw.setdefault("hashes", {})
    return raw


def _save_cache(cache: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sha256_hex(data: str | bytes) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def _sign_headers(action: str, payload: str) -> dict[str, str]:
    ak = settings.volcengine_access_key
    sk = settings.volcengine_secret_key
    region = settings.volcengine_region or "cn-beijing"
    now = dt.datetime.now(dt.timezone.utc)
    x_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    content_type = "application/json"
    payload_hash = _sha256_hex(payload)
    signed_headers = "content-type;host;x-content-sha256;x-date"
    canonical_headers = (
        f"content-type:{content_type}\n"
        f"host:{ARK_HOST}\n"
        f"x-content-sha256:{payload_hash}\n"
        f"x-date:{x_date}\n"
    )
    query = f"Action={quote(action, safe='-_.~')}&Version={quote(ARK_VERSION, safe='-_.~')}"
    canonical_request = (
        f"POST\n/\n{query}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )
    credential_scope = f"{datestamp}/{region}/{ARK_SERVICE}/request"
    string_to_sign = (
        f"HMAC-SHA256\n{x_date}\n{credential_scope}\n{_sha256_hex(canonical_request)}"
    )
    k_date = _hmac(sk.encode("utf-8"), datestamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, ARK_SERVICE)
    k_signing = _hmac(k_service, "request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    auth = (
        f"HMAC-SHA256 Credential={ak}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "Content-Type": content_type,
        "Host": ARK_HOST,
        "X-Date": x_date,
        "X-Content-Sha256": payload_hash,
        "Authorization": auth,
    }


async def ark_open_api(action: str, body: dict) -> dict:
    if not assets_configured():
        raise ArkAssetError(
            "还没配置火山引擎 AK/SK。Seedance 素材库不能用 API Key。"
            "请在 .env 填写 VOLCENGINE_ACCESS_KEY 和 VOLCENGINE_SECRET_KEY"
            "（控制台「访问控制 → 密钥管理」创建），保存后重启工坊。"
        )
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    headers = _sign_headers(action, payload)
    url = f"https://{ARK_HOST}/?Action={action}&Version={ARK_VERSION}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, headers=headers, content=payload.encode("utf-8"))
    try:
        data = resp.json()
    except Exception as exc:
        raise ArkAssetError(f"素材库接口返回不是 JSON：{resp.text[:400]}") from exc
    meta = data.get("ResponseMetadata") if isinstance(data, dict) else None
    if isinstance(meta, dict) and meta.get("Error"):
        err = meta["Error"]
        message = err.get("Message") if isinstance(err, dict) else str(err)
        code = err.get("Code") if isinstance(err, dict) else ""
        raise ArkAssetError(_friendly_api_error(action, code, message))
    if resp.status_code >= 400:
        raise ArkAssetError(f"素材库 HTTP {resp.status_code}: {str(data)[:400]}")
    result = data.get("Result") if isinstance(data, dict) else None
    return result if isinstance(result, dict) else (data if isinstance(data, dict) else {})


def _safe_group_name(label: str) -> str:
    text = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", (label or "character").strip())[:48].strip("-")
    return (text or "character")[:64]


def _remember_group(cache: dict, key: str, group_id: str) -> str:
    cache.setdefault("groups", {})[key] = group_id
    _save_cache(cache)
    return group_id


async def ensure_character_group(cache: dict, owner: str, character: str) -> str:
    key = f"{owner}|{character}"
    cached = (cache.get("groups") or {}).get(key)
    if cached:
        return str(cached)
    name = _safe_group_name(f"chaifen-{owner}-{character}")
    try:
        existing = await _find_existing_group(name)
    except ArkAssetError as exc:
        logger.warning("查询素材组失败，改为直接创建：%s", exc)
        existing = ""
    if existing:
        return _remember_group(cache, key, existing)
    try:
        result = await ark_open_api(
            "CreateAssetGroup",
            {
                "Name": name,
                "Description": f"工坊虚拟角色 {character}",
                "GroupType": "AIGC",
                "ProjectName": _project_name(),
            },
        )
    except ArkAssetError as exc:
        try:
            existing = await _find_existing_group(name)
        except ArkAssetError:
            existing = ""
        if existing:
            return _remember_group(cache, key, existing)
        raise ArkAssetError(f"创建素材组失败：{exc}") from exc
    group_id = str(result.get("Id") or result.get("id") or "")
    if not group_id:
        raise ArkAssetError(f"创建素材组未返回 ID：{result}")
    return _remember_group(cache, key, group_id)


async def _find_existing_group(name: str) -> str:
    listed = await ark_open_api(
        "ListAssetGroups",
        {
            "Filter": {
                "Name": name,
                "GroupType": "AIGC",
            },
            "PageNumber": 1,
            "PageSize": 20,
            "SortBy": "CreateTime",
            "SortOrder": "Desc",
            "ProjectName": _project_name(),
        },
    )
    items = listed.get("Items") or listed.get("items") or []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        if str(item.get("Name") or "") == name:
            return str(item.get("Id") or "")
    return ""


async def wait_asset_active(asset_id: str, timeout: int = 90) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout
    last: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        last = await ark_open_api(
            "GetAsset",
            {"Id": asset_id, "ProjectName": _project_name()},
        )
        status = str(last.get("Status") or last.get("UpstreamStatus") or "")
        if status == "Active":
            return last
        if status == "Failed":
            err = last.get("Error") or {}
            message = err.get("Message") if isinstance(err, dict) else str(err or last)
            raise ArkAssetError(f"素材审核失败：{message}")
        await asyncio.sleep(3)
    raise ArkAssetError(f"素材入库超时（仍是 {last.get('Status') or '未知状态'}）")


async def ingest_character_image(path: Path, *, owner: str, character: str) -> str:
    if not path.exists():
        raise ArkAssetError(f"找不到角色图：{path}")
    digest = file_digest(path)
    cache = _load_cache()
    cached = (cache.get("hashes") or {}).get(digest)
    if cached:
        return str(cached)
    group_id = await ensure_character_group(cache, owner, character)
    data_url = image_data_url(path)
    try:
        result = await ark_open_api(
            "CreateAsset",
            {
                "GroupId": group_id,
                "URL": data_url,
                "AssetType": "Image",
                "Name": _safe_group_name(character)[:64],
                "ProjectName": _project_name(),
            },
        )
    except ArkAssetError as exc:
        raise ArkAssetError(
            f"角色「{character}」入库失败：{exc}。"
            "请确认 AK/SK 与 Seedance API Key 在同一个方舟项目，"
            "ARK_PROJECT_NAME 没填错（默认 default）。"
        ) from exc
    asset_id = str(result.get("Id") or result.get("id") or "")
    if not asset_id:
        raise ArkAssetError(f"创建素材未返回 ID：{result}")
    await wait_asset_active(asset_id)
    uri = f"asset://{asset_id}" if not asset_id.lower().startswith("asset://") else asset_id
    cache.setdefault("hashes", {})[digest] = uri
    _save_cache(cache)
    return uri


async def ingest_character_audio(path: Path, *, owner: str, character: str) -> str:
    if not path.exists():
        raise ArkAssetError(f"找不到参考音频：{path}")
    digest = file_digest(path)
    cache = _load_cache()
    cached = (cache.get("hashes") or {}).get(digest)
    if cached:
        return str(cached)
    group_id = await ensure_character_group(cache, owner, character)
    raw = path.read_bytes()
    mime = "audio/wav" if path.suffix.lower() == ".wav" else "audio/mpeg"
    data_url = f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
    try:
        result = await ark_open_api(
            "CreateAsset",
            {
                "GroupId": group_id,
                "URL": data_url,
                "AssetType": "Audio",
                "Name": _safe_group_name(f"{character}-voice")[:64],
                "ProjectName": _project_name(),
            },
        )
    except ArkAssetError as exc:
        raise ArkAssetError(f"角色「{character}」声线入库失败：{exc}。") from exc
    asset_id = str(result.get("Id") or result.get("id") or "")
    if not asset_id:
        raise ArkAssetError(f"创建音频素材未返回 ID：{result}")
    await wait_asset_active(asset_id)
    uri = f"asset://{asset_id}" if not asset_id.lower().startswith("asset://") else asset_id
    cache.setdefault("hashes", {})[digest] = uri
    _save_cache(cache)
    return uri


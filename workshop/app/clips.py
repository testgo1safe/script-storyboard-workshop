from __future__ import annotations

import re

from .prompts import H3_MAX_IMAGES, H3_MAX_SECONDS, H3_MAX_TEXT, H3_MIN_SECONDS, SEEDANCE_MAX_TEXT
from .schemas import Analysis, ClipPlan, PictureBinding, PositionMark, Scene, Shot, VideoClip

EPISODE_MIN_SECONDS = 8
EPISODE_MAX_SECONDS = 90
CLIP_PACK_MAX_SHOTS = 3
_CONTINUOUS_CAMERA = re.compile(
    r"push|pull|pan|tilt|track|dolly|arc|crane|zoom|follow|steadicam|一镜|推|拉|摇|移|跟",
    re.I,
)
_SOFT_CUT = re.compile(r"continuous|same|match|insert|延续|运镜|同机位", re.I)
_CN_STOP = {
    "镜头",
    "近景",
    "特写",
    "中景",
    "中近景",
    "远景",
    "黑场",
    "人物",
    "开始",
    "结束",
    "对白",
    "动作",
    "场景",
    "角色",
    "竖屏",
    "短剧",
    "风声",
    "脚步声",
}


def clamp_episode_duration(seconds: float) -> int:
    value = int(round(float(seconds)))
    return max(EPISODE_MIN_SECONDS, min(EPISODE_MAX_SECONDS, value))


def estimate_episode_duration(script: str) -> dict:
    """按剧本密度估整集秒数：一拍约一段 10–15 秒，避免灌水或挤爆。"""
    text = (script or "").strip()
    text = re.sub(r"不要[^。！？\n]{1,20}", "", text)
    compact = re.sub(r"\s+", "", text)
    chars = len(compact)
    if chars < 8:
        return {
            "seconds": 12,
            "beats": 1,
            "reason": "剧本过短，先按 12 秒一段拍完一个动作。",
        }

    quotes = re.findall(r"[「『“\"](.{1,60})[」』”\"]", text)
    dialogue_n = len([q for q in quotes if q.strip()])
    colon_lines = len(re.findall(r"[\u4e00-\u9fffA-Za-z]{1,12}[：:].{2,80}", text))
    dialogue_n = max(dialogue_n, min(colon_lines, 6))

    location_shift = len(
        re.findall(r"来到|走进|回到|另一边|与此同时|随后来到", text)
    )
    actions = len(
        re.findall(r"交给|递过|取出|追|跑|摔门|拥抱|打开门|按下", text)
    )
    combat = len(
        re.findall(r"枪|子弹|战斗|掐|杀|士兵|开火|爆炸|追击|格斗", text)
    )

    beats = 1
    if location_shift >= 2:
        beats += min(2, location_shift - 1)
    if actions >= 2:
        beats += 1
    if combat >= 2:
        beats += 1
    if combat >= 4:
        beats += 1
    if dialogue_n >= 3:
        beats += 1
    if chars > 400:
        beats += 1
    if chars > 700:
        beats += 1
    beats = max(1, min(6, beats))

    table = {1: 12, 2: 24, 3: 36, 4: 48, 5: 60, 6: 72}
    seconds = table[beats]
    if beats == 1 and (dialogue_n >= 2 or chars > 180):
        seconds = 15
    if beats == 2 and actions >= 3:
        seconds = 28
    seconds = clamp_episode_duration(seconds)
    reason = (
        f"约 {beats} 个剧情节拍（对白 {dialogue_n} 句，动作变化 {actions} 处"
        f"{'，有转场' if location_shift else ''}{'，含动作/冲突' if combat else ''}），"
        f"建议 {seconds} 秒、{beats} 段出片，避免拉太长站桩或压太短看不清。"
    )
    return {"seconds": seconds, "beats": beats, "reason": reason}


def snap_h3_duration(seconds: float) -> int:
    value = int(round(float(seconds)))
    if value < H3_MIN_SECONDS:
        return H3_MIN_SECONDS
    if value > H3_MAX_SECONDS:
        return H3_MAX_SECONDS
    return value


def fmt_h3_ts(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    minutes = int(seconds // 60)
    rest = seconds - minutes * 60
    return f"{minutes:02d}:{rest:06.3f}"


def shots_for_clip(analysis: Analysis, clip: ClipPlan | VideoClip) -> list[Shot]:
    matched = [s for s in analysis.shots if int(s.clip_index or 1) == int(clip.index)]
    if matched:
        return matched
    return [
        s
        for s in analysis.shots
        if s.start < clip.end and s.end > clip.start
    ]


def pack_clips(analysis: Analysis) -> list[ClipPlan]:
    if not analysis.shots:
        duration = snap_h3_duration(analysis.duration_seconds or H3_MAX_SECONDS)
        analysis.clips = [
            ClipPlan(index=1, title=analysis.title or "片段 1", hook=analysis.logline, start=0, end=duration, duration_seconds=duration)
        ]
        return analysis.clips

    shots = sorted(analysis.shots, key=lambda s: s.start)
    groups: list[list[Shot]] = []
    current: list[Shot] = []

    def group_span(items: list[Shot]) -> float:
        return items[-1].end - items[0].start

    for shot in shots:
        if not current:
            current = [shot]
            continue
        next_span = shot.end - current[0].start
        contact_break = _is_contact_shot(current[-1]) != _is_contact_shot(shot)
        if (
            len(current) >= CLIP_PACK_MAX_SHOTS
            or next_span > H3_MAX_SECONDS + 0.2
            or contact_break
        ):
            groups.append(current)
            current = [shot]
        else:
            current.append(shot)
    if current:
        groups.append(current)

    plans: list[ClipPlan] = []
    for i, group in enumerate(groups, start=1):
        start = group[0].start
        end = group[-1].end
        duration = snap_h3_duration(end - start)
        if any(_is_contact_shot(s) for s in group):
            heavy = any(_is_heavy_contact_shot(s) for s in group)
            duration = max(duration, 12 if heavy else 8)
            duration = min(H3_MAX_SECONDS, duration)
        elif any((s.dialogue_line or "").strip() for s in group):
            duration = max(duration, 8)
            duration = min(H3_MAX_SECONDS, duration)
        end = start + duration
        title = group[0].beat or f"片段 {i}"
        hook = (group[-1].result_state or group[-1].beat or group[0].beat).strip()
        plans.append(
            ClipPlan(
                index=i,
                title=title,
                hook=hook,
                start=start,
                end=end,
                duration_seconds=duration,
            )
        )
        for shot in group:
            shot.clip_index = i
        if group[-1].end != end:
            group[-1].end = end

    analysis.clips = plans
    if plans:
        analysis.duration_seconds = sum(p.duration_seconds for p in plans)
    return plans


def _mentions(text: str, name: str) -> bool:
    blob = text or ""
    name = (name or "").strip()
    if not name:
        return False
    if name in blob:
        return True
    if len(name) >= 2 and name[-2:] in blob:
        return True
    if len(name) >= 3 and name[1:] in blob:
        return True
    return False


_SCENE_HINTS: list[tuple[re.Pattern[str], tuple[str, ...]]] = [
    (re.compile(r"商场|购物车|货架|扫货"), ("商场",)),
    (re.compile(r"酒店|泳池|套房|总统|红酒|品酒"), ("酒店", "泳池", "套房")),
    (re.compile(r"深夜|凌晨|午夜|拨号|已读|警告"), ("深夜", "凌晨")),
    (re.compile(r"豪宅|别墅|客厅|沙发|电视"), ("豪宅", "别墅", "客厅")),
    (re.compile(r"街道|空城|马路"), ("街道", "空城")),
    (re.compile(r"出租屋|公寓|出租"), ("出租屋", "公寓")),
]
_NIGHT_SCENE_RE = re.compile(r"深夜|凌晨|午夜")
_SCALE_SPACE_RE = re.compile(r"商场|酒店|套房|泳池|豪宅|别墅|大厅|中庭|总统|广场|空城|五星")
_PLEASURE_RE = re.compile(r"扫货|全要|放飞|随便|泡|红酒|购物车|总统|挥手|品酒")
_INTIMATE_RE = re.compile(r"拨号|已读|警告|微信|短信")
_CLOSE_SIZE_RE = re.compile(r"近景|特写|中近景|close-?up|MCU|CU|ECU|medium close", re.I)
_MED_SIZE_RE = re.compile(r"中景|medium shot", re.I)


def _split_name_parts(name: str) -> list[str]:
    parts = re.split(r"[/／、,，;；|]", name or "")
    return [p.strip() for p in parts if len(p.strip()) >= 2]


def _unique_names(items: list[str], *, skip: set[str] | None = None) -> list[str]:
    skip = skip or set()
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = (item or "").strip()
        if len(text) < 2 or text in skip or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


_OCCUPATION_ALIASES = [
    (re.compile(r"外卖|骑手|送餐|快递"), ["外卖员", "骑手", "送餐员", "外卖小哥", "快递员"]),
    (re.compile(r"警察|公安"), ["警察", "警官"]),
    (re.compile(r"医生|大夫"), ["医生", "大夫"]),
    (re.compile(r"保安"), ["保安"]),
    (re.compile(r"司机"), ["司机"]),
    (re.compile(r"护士"), ["护士"]),
    (re.compile(r"老师|教师"), ["老师", "教师"]),
    (re.compile(r"律师"), ["律师"]),
    (re.compile(r"厨师|厨子"), ["厨师"]),
    (re.compile(r"经理"), ["经理"]),
    (re.compile(r"保姆|阿姨"), ["保姆"]),
]
_RELATION_ALIASES = [
    (re.compile(r"父亲|爸爸"), ["父亲", "爸爸"]),
    (re.compile(r"母亲|妈妈"), ["母亲", "妈妈"]),
    (re.compile(r"哥哥|兄"), ["哥哥"]),
    (re.compile(r"姐姐"), ["姐姐"]),
    (re.compile(r"叔叔|舅舅"), ["叔叔"]),
]
_SKIP_INTRO_ALIAS = {
    "成年",
    "主演",
    "配角",
    "独立要强",
    "都市白领",
}
_ABSENT_RE = re.compile(r"已离开|离开了|走了|不在场|已经走")
_MEMORY_FATHER_RE = re.compile(r"父亲的味道|父亲小时候|父亲外套|父亲失踪|父亲那天|照片中的?父亲|手机里的?父亲")
_REVEAL_FATHER_RE = re.compile(r"爸[？?]|“爸”|『爸』|喊出.{0,6}爸|是你爸|是父亲|认出父亲|把口罩摘")
_MEDIATED_RE = re.compile(
    r"照片|相片|头像|微信|短信|屏幕|手机里|手机中|聊天界面|聊天记录|放大|古装|城墙|城池"
)


def _intro_line_for_char(script: str, char) -> str:
    names = _unique_names([char.name, *_split_name_parts(char.name)])
    for line in (script or "").splitlines()[:60]:
        stripped = line.strip()
        if not stripped:
            continue
        for name in names:
            if re.match(
                rf"^(?:主要人物[：:]?\s*)?(?:[\u4e00-\u9fff]{{0,8}}[/／、]\s*)*{re.escape(name)}(?:\s*[/／、]\s*[\u4e00-\u9fff]{{2,8}})*\s*[：:]",
                stripped,
            ):
                return stripped
    return ""


def _aliases_from_intro(intro: str, char_name: str) -> list[str]:
    if not intro:
        return []
    rest = intro
    if "：" in intro or ":" in intro:
        rest = re.split(r"[：:]", intro, maxsplit=1)[-1]
    found: list[str] = []
    for part in re.split(r"[，,、；;。]+", rest):
        part = part.strip()
        if not part or part in _SKIP_INTRO_ALIAS or part == char_name:
            continue
        if re.fullmatch(r"\d+岁?", part):
            continue
        if 2 <= len(part) <= 6 and re.fullmatch(r"[\u4e00-\u9fff]+", part):
            found.append(part)
            for suffix in ("老师", "经理", "医生", "护士", "司机", "警察", "律师", "厨师"):
                if part.endswith(suffix) and part != suffix:
                    found.append(suffix)
    return found


def enrich_character_aliases(analysis: Analysis, script: str = "") -> None:
    script = script or ""
    for char in analysis.characters:
        keys = list(char.aliases or [])
        keys.extend(_split_name_parts(char.name))
        source = " ".join(
            p for p in [char.name, char.role, char.wardrobe, _intro_line_for_char(script, char)] if p
        )
        occ_source = source + " " + (char.appearance or "")
        for occ_re, aliases in _OCCUPATION_ALIASES:
            if occ_re.search(occ_source):
                keys.extend(aliases)
        for rel_re, aliases in _RELATION_ALIASES:
            if rel_re.search(source):
                keys.extend(aliases)
        intro = _intro_line_for_char(script, char)
        keys.extend(_aliases_from_intro(intro, char.name))
        if intro:
            for match in re.finditer(r"[\u4e00-\u9fff]{2,8}(?:\s*/\s*[\u4e00-\u9fff]{2,8})+", intro):
                parts = _split_name_parts(match.group(0))
                if char.name in parts or any(p in parts for p in _split_name_parts(char.name)):
                    keys.extend(parts)
        char.aliases = _unique_names(keys, skip={char.name})


def character_alias_keys(char) -> list[str]:
    keys = [char.name, *(char.aliases or [])]
    keys.extend(_split_name_parts(char.name))
    return _unique_names(keys)


def is_body_feature_prop(name: str) -> bool:
    return bool(_BODY_FEATURE_PROP_RE.search(name or ""))


def enrich_prop_aliases(analysis: Analysis) -> None:
    for prop in analysis.props:
        keys = list(prop.aliases or [])
        keys.extend(_split_name_parts(prop.name))
        rest = prop.name or ""
        for prefix in _PROP_PREFIXES:
            if rest.startswith(prefix) and len(rest) - len(prefix) >= 2:
                rest = rest[len(prefix) :]
                keys.append(rest)
        blob = prop.name or ""
        for cre, syns in _PROP_SYNONYMS:
            if cre.search(blob):
                keys.extend(syns)
        prop.aliases = _unique_names(keys, skip={prop.name})


def prop_alias_keys(prop) -> list[str]:
    keys = [prop.name, *(getattr(prop, "aliases", None) or [])]
    keys.extend(_split_name_parts(prop.name or ""))
    return _unique_names(keys)


def _prop_for_picture(analysis: Analysis, pic: PictureBinding):
    for prop in analysis.props:
        if pic.name == prop.name or pic.ref_id == (prop.id or ""):
            return prop
        if pic.name in prop_alias_keys(prop):
            return prop
    return None


def prop_in_blob(blob: str, prop) -> bool:
    text = blob or ""
    for key in prop_alias_keys(prop):
        if len(key) >= 2 and key in text:
            return True
    return False


def picture_prop_in_blob(blob: str, pic: PictureBinding, analysis: Analysis) -> bool:
    if is_body_feature_prop(pic.name):
        return False
    prop = _prop_for_picture(analysis, pic)
    if prop:
        return prop_in_blob(blob, prop)
    return _mentions(blob, pic.name)


def cast_roster_text(analysis: Analysis) -> str:
    lines = []
    for char in analysis.characters:
        aka = "、".join(char.aliases) if char.aliases else "无"
        mask = "戴口罩仍是此人。" if re.search(r"口罩", (char.wardrobe or "") + (char.appearance or "")) else ""
        lines.append(
            f"{char.name}（别称：{aka}）：{char.wardrobe or char.appearance}。{mask}"
            "以上称呼都是同一个人，禁止另找演员。"
        )
    return "\n".join(lines)


def _only_absent_mention(blob: str, name: str) -> bool:
    hits = list(re.finditer(re.escape(name), blob))
    if not hits:
        return True
    for match in hits:
        window = blob[max(0, match.start() - 2) : match.end() + 8]
        if not _ABSENT_RE.search(window):
            return False
    return True


def _only_mediated_mention(blob: str, name: str) -> bool:
    """名字只出现在照片/微信/屏幕里，现场并没有这个人。"""
    if not name or not blob:
        return False
    hits = list(re.finditer(re.escape(name), blob))
    if not hits:
        return False
    for match in hits:
        window = blob[max(0, match.start() - 16) : match.end() + 16]
        if not _MEDIATED_RE.search(window):
            return False
    return True


def _relation_is_present(blob: str, alias: str) -> bool:
    if alias not in {"父亲", "爸爸"}:
        return False
    if _REVEAL_FATHER_RE.search(blob):
        return True
    if alias not in blob:
        return False
    stripped = _MEMORY_FATHER_RE.sub("", blob)
    return alias in stripped and not _only_absent_mention(stripped, alias)


def character_in_blob(blob: str, char, pic_name: str = "") -> bool:
    text = blob or ""
    keys = character_alias_keys(char)
    if pic_name:
        keys = _unique_names(keys + [pic_name])
    for key in keys:
        if key in {"父亲", "爸爸"}:
            if _relation_is_present(text, key):
                return True
            continue
        if key and key in text and not _only_absent_mention(text, key):
            if _only_mediated_mention(text, key):
                continue
            return True
    if char.name and _mentions(text, char.name) and not _only_absent_mention(text, char.name):
        if _only_mediated_mention(text, char.name):
            return False
        return True
    return False


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_EXTRA_ROLE_RE = re.compile(r"群演|路人|群众|龙套|extras?|background", re.I)
_EXTRA_NAME_RE = re.compile(
    r"士兵|军人|敌人|枪手|保镖|侍卫|下属|路人|群众|宾客|客人|服务员|同事们"
)
_SHOT_SIZE_EN = {
    "大特写": "extreme close-up",
    "特写": "close-up",
    "近景": "close-up",
    "中近景": "medium close-up",
    "中景": "medium shot",
    "全景": "full shot",
    "远景": "wide shot",
    "过肩": "over-the-shoulder",
}


def has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def english_only(text: str) -> str:
    value = (text or "").strip()
    if not value or has_cjk(value):
        return ""
    return value


def is_extra_character(char) -> bool:
    role = getattr(char, "role", "") or ""
    if re.search(r"主演|男主|女主|男配|女配|反派|配角", role):
        return False
    if _EXTRA_ROLE_RE.search(role):
        return True
    name = getattr(char, "name", "") or ""
    if _EXTRA_ROLE_RE.search(name):
        return True
    return bool(re.search(r"^(敌军)?士兵|路人[甲乙丙丁ABC]?$|群众|保镖[ABC]?$", name))


def extra_in_blob(blob: str, char) -> bool:
    if character_in_blob(blob, char):
        return True
    if not is_extra_character(char):
        return False
    text = blob or ""
    keys = list(character_alias_keys(char)) + [char.name, getattr(char, "role", "")]
    for key in keys:
        key = (key or "").strip()
        if len(key) >= 2 and key in text and not _only_absent_mention(text, key):
            if _only_mediated_mention(text, key):
                continue
            return True
    name_role = f"{char.name}{getattr(char, 'role', '')}"
    if re.search(r"士兵|军人|枪手|敌人", name_role) and re.search(
        r"士兵|军人|敌人|枪手|开火", text
    ):
        return True
    if re.search(r"路人|群众|宾客|客人|服务员", name_role) and re.search(
        r"路人|群众|宾客|客人|服务员", text
    ):
        return True
    return False


def is_extra_picture(analysis: Analysis | None, pic: PictureBinding) -> bool:
    if pic.kind != "character":
        return False
    char = _char_for_picture(analysis, pic) if analysis else None
    if char:
        return is_extra_character(char)
    blob = f"{pic.name} {pic.lock_text}"
    return bool(_EXTRA_ROLE_RE.search(blob) or _EXTRA_NAME_RE.search(pic.name or ""))


def split_cast_pictures(
    analysis: Analysis | None, pictures: list[PictureBinding] | None
) -> tuple[list[PictureBinding], list[PictureBinding]]:
    leads: list[PictureBinding] = []
    extras: list[PictureBinding] = []
    for pic in pictures or []:
        if pic.kind != "character":
            continue
        if is_extra_picture(analysis, pic):
            extras.append(pic)
        else:
            leads.append(pic)
    return leads, extras


def _char_for_picture(analysis: Analysis, pic: PictureBinding):
    for char in analysis.characters:
        if pic.ref_id and pic.ref_id == char.id:
            return char
        if pic.name and pic.name == char.name:
            return char
        if pic.name and pic.name in character_alias_keys(char):
            return char
    return None


def shot_blob(shot: Shot) -> str:
    return " ".join(
        [
            shot.beat,
            shot.start_state,
            shot.process,
            shot.result_state,
            shot.performance,
            shot.camera,
            shot.continuity,
            shot.blocking,
            shot.dialogue_speaker,
            shot.dialogue_line,
            " ".join(shot.sound or []),
        ]
    )


CONTACT_RE = re.compile(
    r"掐|举起|抬起|抱起|摔倒|摔在|挡子弹|挡住子弹|"
    r"收紧|松手|松开|踢|殴打|拳打|打人|打向|打中|"
    r"推|按住|扑|撞|追|格斗|刺|砍|扭|拽|摔"
)
HEAVY_CONTACT_RE = re.compile(
    r"掐|举起|抬起|抱起|挡子弹|挡住子弹|踢|打|扑|撞|格斗|刺"
)
INSPECT_OBJ_RE = re.compile(r"照片|相片|手机|屏幕|纸条|证件|纽扣|扣子|名片|字条|报纸|病历|病例|存储卡")
INSPECT_ACT_RE = re.compile(r"对照|对比|翻到|放大|端详|看着|细看|查看|凑近|拿出|摊开|捡起|掉下|落下|展开")
INSPECT_PROMPT_MARK = "CAMERA GOES TO THE OBJECT"
_BODY_FEATURE_PROP_RE = re.compile(r"伤疤|疤痕|胎记|纹身|伤口|刀疤")
_PROP_PREFIXES = (
    "泛黄的",
    "一张",
    "一颗",
    "一枚",
    "一部",
    "一份",
    "一本",
    "一个",
    "一块",
    "一封",
    "旧的",
    "泛黄",
    "旧",
)
_PROP_SYNONYMS = [
    (re.compile(r"纽扣|扣子"), ["纽扣", "扣子"]),
    (re.compile(r"报纸|剪报"), ["报纸", "旧报纸", "剪报"]),
    (re.compile(r"病历|病例"), ["病历", "病例"]),
    (re.compile(r"照片|相片"), ["照片", "相片", "旧照片"]),
    (re.compile(r"手机"), ["手机"]),
    (re.compile(r"存储卡|记忆卡"), ["存储卡", "记忆卡"]),
    (re.compile(r"U盘|优盘"), ["U盘", "优盘"]),
]


def _action_src(shot: Shot) -> str:
    return " ".join(p for p in [shot.beat, shot.start_state, shot.process] if p)


def _is_contact_shot(shot: Shot) -> bool:
    if CONTACT_RE.search(_action_src(shot)):
        return True
    return bool(re.search(r"松手|松开|摔倒|摔在", shot.result_state or ""))


def _is_heavy_contact_shot(shot: Shot) -> bool:
    return bool(HEAVY_CONTACT_RE.search(_action_src(shot)))


def _is_inspect_shot(shot: Shot) -> bool:
    blob = shot_blob(shot)
    return bool(INSPECT_OBJ_RE.search(blob) and INSPECT_ACT_RE.search(blob))


def _inspect_en(prop_label: str = "") -> str:
    obj = prop_label or "the object"
    return (
        f"{INSPECT_PROMPT_MARK}: the camera moves to {obj}. "
        f"High-angle over-shoulder or a slow tilt-down, then a small Push In onto {obj}. "
        "She looks DOWN at the objects at chest or lap height, reading them for herself. "
        f"Do NOT lift {obj} toward the filming lens. "
        "Not a vlog, not a product demo, not showing evidence to the audience. "
        f"We see {obj} because the camera arrives there, not because she presents it to camera."
    )


def _lead_name_en(blob: str) -> str:
    if re.search(r"K-07|K07", blob, re.I):
        return "K-07"
    if "爸爸" in blob or "机器人" in blob:
        return "the robot father"
    return "the lead"


def _strip_instant(text: str) -> str:
    return re.sub(r"瞬间|立刻|马上|突然", "", text or "").strip()


def _physics_en(shot: Shot) -> str:
    blob = shot_blob(shot)
    lead = _lead_name_en(blob)
    facts = _strip_instant(
        " ".join(
            p
            for p in [shot.start_state, shot.process, shot.performance, shot.result_state]
            if p
        )
    )
    extras: list[str] = []
    if _is_contact_shot(shot):
        extras.append(
            "Play this as a readable sequence, not all at once: start, then visible contact, then result. "
            "Fast but not a teleport. Contacting surfaces must touch before any force. "
            "No clipping, no extra limbs, no identity morph, no rubber bodies."
        )
    if re.search(r"冲来|冲向|追|扑", blob):
        extras.append(
            "The attacker closes distance with real footsteps before any grab or hit."
        )
    if re.search(r"挡子弹|挡住子弹|火花", blob):
        extras.append(
            f"{lead} turns the BACK to incoming fire and holds the other person against the chest. "
            "Sparks hit the backplate. This is a body shield, not a face-to-face hug, not a neck grab."
        )
    if re.search(r"抱起", blob) and not re.search(r"掐|挡子弹|挡住子弹", blob):
        extras.append(
            f"{lead}'s arms stay wrapped around the other person; feet leave the ground only after the hold locks."
        )
    if re.search(r"掐", blob):
        if "脖子" in blob or "喉" in blob:
            extras.append(
                f"{lead} plants both feet. The grabbing hand wraps fully around the throat: "
                "full contact, skin compressed, no gap, no clipping. Boots stay on the ground until the grip locks."
            )
        else:
            extras.append(
                f"{lead} plants both feet. The grabbing hand wraps the target with full contact before tightening."
            )
    if re.search(r"举起|抬起", blob):
        extras.append(
            "Only after the grip is locked, one continuous lift. Feet scrape, then leave the ground about 30cm. "
            "The body hangs with visible weight; not levitation, not a morph, not a snap pop."
        )
    if re.search(r"踢|打|推|撞|刺|砍", blob):
        extras.append(
            "The striking limb travels through space and hits a visible surface; the target's body takes the weight of the hit and reacts. "
            "Do not pop the pose."
        )
    if re.search(r"收紧", blob):
        extras.append("After contact is locked, the grip tightens a little; faces do not swap.")
    if re.search(r"松手|松开|摔倒|摔在", blob):
        extras.append(
            "When released, the body drops with gravity, hits the ground, then moves. No freeze-float."
        )
    return " ".join(p for p in [facts, *extras] if p)


def _prop_label(pictures: list[PictureBinding]) -> str:
    names = [p.name for p in pictures if p.kind == "prop"]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return " and ".join(names)


def _shot_size_en(shot: Shot) -> str:
    raw = (shot.shot_size or "").strip()
    for zh, en in _SHOT_SIZE_EN.items():
        if zh in raw:
            return en
    if raw and not has_cjk(raw):
        return raw
    return "medium close-up"


def _visible_shot_body(
    shot: Shot, prop_label: str = "", marks: list[PositionMark] | None = None
) -> str:
    # H3 会把提示词里的中文当成台词念出来，画面描述必须是英文。
    # performance / 感情提示只给分镜导演看，禁止进 H3。
    size = _shot_size_en(shot)
    use_marks = _marks_from_items(getattr(shot, "marks", None)) or marks or []
    blocking = "Blocking: " + "; ".join(_mark_en(m) for m in use_marks) + "." if use_marks else ""
    if _is_contact_shot(shot):
        body = " ".join(p for p in [size, blocking, english_only(shot.blocking), _physics_en(shot)] if p)
    else:
        parts = [size, blocking, english_only(shot.blocking)]
        action = [
            english_only(shot.start_state),
            english_only(shot.process),
            english_only(shot.result_state),
        ]
        parts.extend(p for p in action if p)
        if not any(action):
            parts.append(
                "Silent live-action of this beat only. Faces and bodies act; "
                "nobody speaks director notes, emotion labels, or performance text."
            )
        body = " ".join(p for p in parts if p)
    if _is_inspect_shot(shot):
        body = " ".join(p for p in [body, _inspect_en(prop_label)] if p)
    return body


def select_clip_pictures(
    analysis: Analysis,
    clip: ClipPlan | VideoClip,
    pictures: list[PictureBinding],
    shots: list[Shot] | None = None,
) -> list[PictureBinding]:
    shots = shots if shots is not None else shots_for_clip(analysis, clip)
    blob = _clip_blob(analysis, clip)
    chosen: list[PictureBinding] = []

    def take(pic: PictureBinding) -> None:
        if pic.picture_index in {p.picture_index for p in chosen}:
            return
        chosen.append(pic)

    enrich_character_aliases(analysis)
    enrich_prop_aliases(analysis)
    chars = [p for p in pictures if p.kind == "character"]
    scenes = [p for p in pictures if p.kind == "scene"]
    props = [p for p in pictures if p.kind == "prop" and not is_body_feature_prop(p.name)]

    named: list[PictureBinding] = []
    seen: set[int] = set()
    for char in analysis.characters:
        if not extra_in_blob(blob, char):
            continue
        for pic in chars:
            if pic.picture_index in seen:
                continue
            linked = _char_for_picture(analysis, pic)
            if linked is char or pic.name == char.name or pic.ref_id == char.id:
                named.append(pic)
                seen.add(pic.picture_index)
                break
            if pic.name in character_alias_keys(char):
                named.append(pic)
                seen.add(pic.picture_index)
                break
    for pic in named:
        take(pic)
        if len(chosen) >= H3_MAX_IMAGES:
            return chosen[:H3_MAX_IMAGES]

    prop_hits = [p for p in props if picture_prop_in_blob(blob, p, analysis)]
    prop_hits.sort(key=lambda p: len(p.name or ""), reverse=True)
    for pic in prop_hits:
        take(pic)
        if len(chosen) >= H3_MAX_IMAGES:
            return chosen[:H3_MAX_IMAGES]

    wanted = best_scene(analysis, clip)
    if wanted:
        for pic in scenes:
            if pic.name == wanted.name or (wanted.id and pic.ref_id == wanted.id):
                take(pic)
                break
        else:
            for pic in scenes:
                if wanted.name and pic.name and (wanted.name in pic.name or pic.name in wanted.name):
                    take(pic)
                    break

    return chosen[:H3_MAX_IMAGES]


def picture_role_line(pic: PictureBinding, image_no: int, analysis: Analysis | None = None) -> str:
    char = _char_for_picture(analysis, pic) if analysis else None
    extra = is_extra_picture(analysis, pic)
    masked = bool(char and re.search(r"口罩", (char.wardrobe or "") + (char.appearance or "")))
    tag = f"Image {image_no} / <Picture {pic.picture_index}>"
    if pic.kind == "character":
        if extra:
            duty = (
                f"locks extra type {pic.name}: one unique background player, face, body and wardrobe; "
                "bind this extra as a cast member, not a random crowd face"
            )
            avoid = (
                "do not clone this extra into twins or a mirror, do not duplicate this face, "
                "do not promote this extra into a new lead; "
                "do not copy this image's pose, background, lighting or framing"
            )
        else:
            duty = f"locks {pic.name} identity, face, body and wardrobe only; one unique person"
            avoid = (
                "do not copy this image's pose, sitting, looking-into-camera, background, lighting or framing; "
                "do not spawn a second copy or mirrored twin of this person; "
                "the character must perform this clip's action"
            )
        if masked:
            avoid += (
                "; if masked, keep the same eyes, hair, age and body; "
                "unmasking reveals THIS face, never a different actor"
            )
    elif pic.kind == "scene":
        duty = f"locks the {pic.name} location, architecture, weather and light direction"
        avoid = "do not copy extra faces or extra lead characters from this image"
    else:
        duty = (
            f"IS the {pic.name}: lock this object's exact shape, color, material, wear and silhouette; "
            f"every on-screen {pic.name} must be THIS object, never a substitute"
        )
        avoid = "do not invent a different prop, do not copy people or tabletop background from this image"
    return f"{tag} {duty}; {avoid}."


def _is_continuous_camera(shot: Shot) -> bool:
    blob = " ".join(p for p in [shot.camera_move, shot.camera, shot.cut] if p)
    return bool(_CONTINUOUS_CAMERA.search(blob)) or bool(_SOFT_CUT.search(blob))


def _same_blocking(a: Shot, b: Shot) -> bool:
    if a.marks and b.marks:
        return [m.model_dump() for m in a.marks] == [m.model_dump() for m in b.marks]
    ba = (a.blocking or "").strip()
    bb = (b.blocking or "").strip()
    return bool(ba) and ba == bb


def _merge_shots(into: Shot, extra: Shot) -> None:
    into.end = max(float(into.end), float(extra.end))
    for field in ("process", "result_state", "performance", "continuity"):
        cur = (getattr(into, field) or "").strip()
        add = (getattr(extra, field) or "").strip()
        if add and add not in cur:
            setattr(into, field, f"{cur} {add}".strip() if cur else add)
    if (extra.dialogue_line or "").strip() and not (into.dialogue_line or "").strip():
        into.dialogue_speaker = extra.dialogue_speaker
        into.dialogue_line = extra.dialogue_line
    move = (extra.camera_move or extra.camera or "").strip()
    if move and (_is_continuous_camera(extra) or not (into.camera_move or into.camera)):
        into.camera_move = extra.camera_move or into.camera_move
        into.camera = extra.camera or into.camera
    into.cut = "continuous"


def _force_merge_shots(shots: list[Shot], limit: int) -> list[Shot]:
    if len(shots) <= limit:
        return shots
    merged = [shots[0].model_copy(deep=True)]
    for shot in shots[1:]:
        if len(merged) >= limit:
            _merge_shots(merged[-1], shot)
        elif _is_contact_shot(shot) or _is_contact_shot(merged[-1]):
            merged.append(shot.model_copy(deep=True))
        else:
            _merge_shots(merged[-1], shot)
    return merged


def coalesce_clip_shots(shots: list[Shot], duration: float) -> list[Shot]:
    if len(shots) <= 1:
        return shots
    if any(_is_contact_shot(s) for s in shots):
        max_shots = 3 if duration >= 12 else 2
        return _force_merge_shots([s.model_copy(deep=True) for s in shots], max_shots)

    out: list[Shot] = []
    for shot in shots:
        cur = shot.model_copy(deep=True)
        if not out:
            out.append(cur)
            continue
        prev = out[-1]
        hard_cut = (cur.cut or "hard_cut").lower() == "hard_cut" and not _is_continuous_camera(cur)
        if hard_cut and not _same_blocking(prev, cur) and not _is_continuous_camera(prev):
            out.append(cur)
            continue
        if _is_inspect_shot(prev) ^ _is_inspect_shot(cur):
            out.append(cur)
            continue
        _merge_shots(prev, cur)

    if duration <= 8:
        cap = 1
    elif duration <= 12:
        cap = 2
    else:
        cap = 3
    return _force_merge_shots(out, cap)


def _scene_match_blob(analysis: Analysis, clip: ClipPlan | VideoClip) -> str:
    blob = _clip_blob(analysis, clip)
    events = getattr(analysis, "complete_event", None) or []
    idx = int(getattr(clip, "index", 0) or 0)
    if 1 <= idx <= len(events):
        blob = f"{blob} {events[idx - 1]}"
    return blob


def score_scene(scene: Scene, blob: str) -> int:
    name = (scene.name or "").strip()
    if not name:
        return 0
    score = 0
    if name in blob:
        score += 20 + min(len(name), 12)
    elif _mentions(blob, name):
        score += 8 + min(len(name), 8)
    for hint_re, keys in _SCENE_HINTS:
        if hint_re.search(blob) and any(k in name for k in keys):
            score += 12
    if _NIGHT_SCENE_RE.search(name):
        if _NIGHT_SCENE_RE.search(blob):
            score += 10
        else:
            score -= 8
    return score


def best_scene(analysis: Analysis, clip: ClipPlan | VideoClip) -> Scene | None:
    if not analysis.scenes:
        return None
    blob = _scene_match_blob(analysis, clip)
    ranked = sorted(
        ((score_scene(scene, blob), i, scene) for i, scene in enumerate(analysis.scenes)),
        key=lambda item: (-item[0], item[1]),
    )
    top_score, _, top = ranked[0]
    if top_score > 0:
        return top
    prev = previous_clip_plan(analysis, clip)
    if prev is not None and int(getattr(prev, "index", 0) or 0) < int(getattr(clip, "index", 0) or 0):
        inherited = best_scene(analysis, prev)
        if inherited:
            return inherited
    if len(analysis.scenes) == 1:
        return analysis.scenes[0]
    return None


def _clip_scene(
    analysis: Analysis,
    clip: ClipPlan | VideoClip,
    pictures: list[PictureBinding],
) -> Scene | None:
    wanted = best_scene(analysis, clip)
    if wanted:
        return wanted
    scene_pics = [p for p in pictures if p.kind == "scene"]
    if scene_pics:
        name = scene_pics[0].name
        for scene in analysis.scenes:
            if scene.name == name:
                return scene
    return None


def _lighting_en(analysis: Analysis, scene: Scene | None) -> str:
    visual = analysis.visual
    blob = " ".join(
        p
        for p in [
            scene.lighting if scene else "",
            scene.time if scene else "",
            scene.atmosphere if scene else "",
            visual.lighting if visual else "",
            visual.color if visual else "",
        ]
        if p
    )
    parts: list[str] = []
    for token in [
        visual.lighting if visual else "",
        scene.lighting if scene else "",
    ]:
        en = english_only(token or "")
        if en:
            parts.append(en)
    if re.search(r"雨|夜|晚|凌晨|午夜", blob):
        parts.append("night interior, cold ambient, wet-window spill, low-key contrast")
    elif re.search(r"晨|早|午|白天|日|晴", blob):
        parts.append("daylight through windows, soft natural fill, no unmotivated night lamps")
    elif re.search(r"审讯|警局|讯问", blob):
        parts.append("single harsh overhead practical, deep shadows, cool institutional spill")
    elif re.search(r"压抑|昏暗|阴", blob):
        parts.append("dim motivated practicals only, no flat beauty fill")
    if re.search(r"暖|台灯光|落地灯|壁灯|烛", blob):
        parts.append("one warm practical accent against cooler ambient")
    if re.search(r"冷|蓝|霓虹", blob):
        parts.append("cooler color temperature on ambient and rim")
    deduped: list[str] = []
    seen: set[str] = set()
    for part in parts:
        key = part.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(part)
    return "; ".join(deduped) or "motivated practicals matching the scene reference time of day"


def _lighting_lock_sentence(
    analysis: Analysis,
    clip: ClipPlan | VideoClip,
    pictures: list[PictureBinding],
) -> str:
    scene = _clip_scene(analysis, clip, pictures)
    body = _lighting_en(analysis, scene)
    return (
        f"LIGHT LOCK: {body}. "
        "Keep only motivated sources that belong in this location and time "
        "(window, desk lamp, corridor spill, neon, interrogation lamp, candle, screen glow). "
        "Do not add unrelated studio lamps or remove established practicals from the scene reference."
    )


def _shot_head(
    i: int,
    rel: float,
    visible: str,
    prev: Shot | None,
    shot: Shot,
) -> str:
    if i == 1:
        return f"[Shot 1] {visible}".strip()
    cut = (shot.cut or "hard_cut").strip().lower()
    ts = fmt_h3_ts(rel)
    if cut in {"continuous", "same", "match"} or _SOFT_CUT.search(cut or ""):
        move = english_only(shot.camera_move) or english_only(shot.camera) or "the same move"
        return f"[Shot {i}] At {ts}, camera continues with {move} — {visible}".strip()
    if prev and (_is_continuous_camera(shot) or _is_continuous_camera(prev)) and _same_blocking(prev, shot):
        move = english_only(shot.camera_move) or english_only(prev.camera_move) or "slow push-in"
        return f"[Shot {i}] At {ts}, continuing {move} — {visible}".strip()
    if cut == "insert":
        return f"[Shot {i}] At {ts}, brief insert cut to {visible}".strip()
    if _is_inspect_shot(shot) and prev and _same_blocking(prev, shot):
        move = english_only(shot.camera_move) or "slow tilt-down and push-in"
        return f"[Shot {i}] At {ts}, same setup, {move} onto the detail — {visible}".strip()
    return f"[Shot {i}] At {ts}, hard cut to {visible}".strip()


def _camera_line(shot: Shot, prop_label: str = "") -> str:
    if _is_inspect_shot(shot):
        obj = prop_label or "the object in the hands"
        return (
            f"Camera: one continuous move — high-angle over-shoulder, slow tilt down, then Push In onto {obj} "
            "at slow speed. Do not hard-cut mid-inspection. "
            "Do not frame this as a frontal presentation to the filming lens."
        )
    if _is_contact_shot(shot):
        if _is_heavy_contact_shot(shot):
            return (
                "Camera: slightly low medium shot, hold both bodies in frame at medium speed. "
                "Do not cut during the contact."
            )
        return "Camera: medium shot holding both bodies in frame at medium speed."
    move = english_only(shot.camera_move) or english_only(shot.camera) or "Static Shot"
    speed = shot.camera_speed or "slow"
    if "Camera:" in move:
        return move
    return f"Camera: {move} at {speed} speed."


def _truncate(text: str, limit: int = H3_MAX_TEXT) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit("\n", 1)[0]
    return cut.strip()


def _distinct_cn(text: str) -> set[str]:
    return {w for w in re.findall(r"[\u4e00-\u9fff]{2,}", text or "") if w not in _CN_STOP}


def _clip_blob(analysis: Analysis, clip: ClipPlan | VideoClip) -> str:
    shots = shots_for_clip(analysis, clip)
    return " ".join(
        [
            getattr(clip, "hook", "") or "",
            getattr(clip, "title", "") or "",
            *(shot_blob(s) for s in shots),
        ]
    )


def _present_cast(
    analysis: Analysis, blob: str, pictures: list[PictureBinding] | None = None
) -> tuple[list[str], list[str]]:
    leads, extras = split_cast_pictures(analysis, pictures)

    def on_set(pic: PictureBinding) -> bool:
        char = _char_for_picture(analysis, pic)
        if char:
            return extra_in_blob(blob, char)
        name = (pic.name or "").strip()
        if not name or _only_absent_mention(blob, name) or _only_mediated_mention(blob, name):
            return False
        return name in blob

    lead_names = [p.name for p in leads if on_set(p)]
    extra_names = [p.name for p in extras if on_set(p)]
    if not lead_names:
        lead_names = [
            c.name
            for c in analysis.characters
            if (not is_extra_character(c)) and extra_in_blob(blob, c)
        ]
    if not extra_names:
        extra_names = [
            c.name for c in analysis.characters if is_extra_character(c) and extra_in_blob(blob, c)
        ]
    if not extra_names and re.search(r"士兵|军人|敌人|枪手|士兵们", blob):
        extra_names = ["soldiers"]
    return lead_names, extra_names


def _entity_lock_line(analysis: Analysis, blob: str, pictures: list[PictureBinding] | None = None) -> str:
    lead_names, extra_names = _present_cast(analysis, blob, pictures)
    n = len(lead_names)
    m = len(extra_names)
    if n == 0 and m == 0:
        base = "no extra crowd faces, do not invent a new lead"
    elif n == 0:
        base = "no named leads"
    elif n == 1:
        base = (
            f"exactly 1 named adult lead ({lead_names[0]}), "
            "same face and wardrobe as the reference image, do not recast"
        )
    else:
        listed = " and ".join(lead_names) if lead_names else "none"
        base = (
            f"exactly {n} named adult leads: {listed}. "
            "Same faces and wardrobe as their reference images. Do not recast."
        )
    if m:
        extras = " and ".join(extra_names)
        label = "extra" if m == 1 else "extras"
        base += f" Plus {m} locked {label} ({extras}) matching their extra reference images"
    elif re.search(r"士兵|军人|敌人|枪手|士兵们", blob):
        base += ", plus unnamed soldiers required by the action"
    if m:
        base += ", no faces beyond this locked cast"
    else:
        base += ", no extra crowd faces"
    return base.rstrip(" .")


def _headcount_lock_sentence(
    analysis: Analysis, blob: str, pictures: list[PictureBinding] | None = None
) -> str:
    lead_names, extra_names = _present_cast(analysis, blob, pictures)
    n, m = len(lead_names), len(extra_names)
    total = n + m
    who = " + ".join(
        p
        for p in [
            f"{n} named leads" if n else "",
            f"{m} extras" if m else "",
        ]
        if p
    ) or "0 people"
    return (
        f"HEADCOUNT LOCK: {who} = {total} unique bodies on screen. "
        "Never a second copy of the same person, never a mirrored twin, never a leftover extra body, "
        "never a clone in the background of a close-up. Count the heads before you generate."
    )


def _camera_lock_sentence(shots: list[Shot]) -> str:
    first = shots[0] if shots else None
    move = "Static Shot"
    if first:
        move = english_only(first.camera_move) or english_only(first.camera) or "Static Shot"
    prefer = (
        "prefer one continuous take or a single slow push/pull; "
        "hard cuts only for a deliberate new beat, not every few seconds; "
    )
    return (
        f"CAMERA LOCK: stay on one side of the 180-degree line; named move is {move}; "
        f"{prefer}"
        "hard cuts change shot size or angle only; never a new room setup; "
        "never a mirrored reverse that clones a person."
    )


def _scene_lock_sentence(
    analysis: Analysis, clip: ClipPlan | VideoClip, pictures: list[PictureBinding]
) -> str:
    scene_pics = [p for p in pictures if p.kind == "scene"]
    if scene_pics:
        loc = "the attached scene reference image locks architecture, weather and motivated light"
    else:
        loc = "stay in this one location"
    return (
        f"SCENE LOCK: {loc}. Do not change rooms, time of day, or weather. Do not invent a second space."
    )


def _speech_lock_sentence(shots: list[Shot]) -> str:
    has_line = any((s.dialogue_line or "").strip() for s in shots)
    if not has_line:
        return (
            "SPEECH LOCK: no spoken Chinese in this clip. Actors stay silent. "
            "Never speak performance notes, emotion labels, blocking, or director text."
        )
    return (
        'SPEECH LOCK: the only spoken Chinese is inside says: [Chinese] "..." quotes. '
        "Never speak performance, emotion, blocking, camera, or director notes. "
        "Silent acting only outside those quotes."
    )


_SIDE_NORM = {
    "left": "left",
    "l": "left",
    "screen-left": "left",
    "screen_left": "left",
    "左": "left",
    "左侧": "left",
    "左边": "left",
    "画面左": "left",
    "right": "right",
    "r": "right",
    "screen-right": "right",
    "screen_right": "right",
    "右": "right",
    "右侧": "right",
    "右边": "right",
    "画面右": "right",
    "center": "center",
    "middle": "center",
    "中": "center",
    "中间": "center",
    "画面中": "center",
    "bg": "bg",
    "background": "bg",
    "后景": "bg",
    "背景": "bg",
    "远处": "bg",
}
_FACE_NORM = {
    "left": "left",
    "right": "right",
    "each-other": "each-other",
    "each_other": "each-other",
    "toward": "each-other",
    "away": "away",
    "左": "left",
    "朝左": "left",
    "面向左": "left",
    "右": "right",
    "朝右": "right",
    "面向右": "right",
    "对视": "each-other",
    "面对面": "each-other",
    "背对": "away",
}
_ANCHOR_NORM = {
    "desk": "desk",
    "table": "desk",
    "桌": "desk",
    "桌子": "desk",
    "茶几": "desk",
    "door": "door",
    "门": "door",
    "window": "window",
    "窗": "window",
    "窗户": "window",
    "seat": "seat",
    "sofa": "seat",
    "椅": "seat",
    "沙发": "seat",
    "bed": "bed",
    "床": "bed",
    "car": "car",
    "车": "car",
    "wall": "wall",
    "墙": "wall",
}
_POSTURE_NORM = {
    "sitting": "sitting",
    "sit": "sitting",
    "坐": "sitting",
    "坐下": "sitting",
    "standing": "standing",
    "stand": "standing",
    "站": "standing",
    "站着": "standing",
}
_SIDE_EN = {
    "left": "viewer's screen-left",
    "right": "viewer's screen-right",
    "center": "screen-center",
    "bg": "background",
}
_FACE_EN = {
    "left": "facing screen-left",
    "right": "facing screen-right",
    "each-other": "facing the other person",
    "away": "facing away",
}
_ANCHOR_EN = {
    "desk": "by the desk",
    "door": "by the door",
    "window": "by the window",
    "seat": "at the seat",
    "bed": "by the bed",
    "car": "by the car",
    "wall": "by the wall",
}
_SIDE_ZH = {"left": "画面左", "right": "画面右", "center": "画面中", "bg": "后景"}
_FACE_ZH = {"left": "朝左", "right": "朝右", "each-other": "对视", "away": "背对"}
_ANCHOR_ZH = {
    "desk": "靠桌",
    "door": "靠门",
    "window": "靠窗",
    "seat": "在座位",
    "bed": "靠床",
    "car": "靠车",
    "wall": "靠墙",
}
_POSTURE_ZH = {"sitting": "坐", "standing": "站"}


def _norm_token(value: str, table: dict[str, str]) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    return table.get(raw, table.get(raw.lower(), raw.lower() if raw.lower() in table.values() else ""))


def _coerce_mark(item) -> PositionMark | None:
    if isinstance(item, PositionMark):
        mark = item
    elif isinstance(item, dict):
        try:
            mark = PositionMark.model_validate(item)
        except Exception:
            return None
    else:
        return None
    name = (mark.name or "").strip()
    if not name:
        return None
    return PositionMark(
        name=name,
        side=_norm_token(mark.side, _SIDE_NORM),
        facing=_norm_token(mark.facing, _FACE_NORM),
        anchor=_norm_token(mark.anchor, _ANCHOR_NORM),
        posture=_norm_token(mark.posture, _POSTURE_NORM),
    )


def _merge_mark(dst: PositionMark, src: PositionMark) -> PositionMark:
    return PositionMark(
        name=dst.name or src.name,
        side=dst.side or src.side,
        facing=dst.facing or src.facing,
        anchor=dst.anchor or src.anchor,
        posture=dst.posture or src.posture,
    )


def _marks_from_items(items) -> list[PositionMark]:
    out: list[PositionMark] = []
    seen: dict[str, int] = {}
    for item in items or []:
        mark = _coerce_mark(item)
        if not mark:
            continue
        if mark.name in seen:
            idx = seen[mark.name]
            out[idx] = _merge_mark(out[idx], mark)
        else:
            seen[mark.name] = len(out)
            out.append(mark)
    return out


def _window_mark(name: str, window: str) -> PositionMark:
    clause = re.sub(r"面向[左右]|朝[左右]|看向[左右]", " ", window)
    side = ""
    left = re.search(r"(?:画面|镜头|屏幕)?左(?:侧|边)?", clause)
    right = re.search(r"(?:画面|镜头|屏幕)?右(?:侧|边)?", clause)
    if left and right:
        side = "left" if left.start() < right.start() else "right"
    elif left:
        side = "left"
    elif right:
        side = "right"
    elif re.search(r"中间|画面中|中央", clause):
        side = "center"
    elif re.search(r"后景|背景|远处", clause):
        side = "bg"
    facing = ""
    if re.search(r"面向右|朝右|看向右", window):
        facing = "right"
    elif re.search(r"面向左|朝左|看向左", window):
        facing = "left"
    elif re.search(r"对视|面对面", window):
        facing = "each-other"
    elif re.search(r"背对|背向", window):
        facing = "away"
    anchor = ""
    for zh, en in (
        ("茶几", "desk"),
        ("桌子", "desk"),
        ("桌", "desk"),
        ("门", "door"),
        ("窗", "window"),
        ("沙发", "seat"),
        ("椅", "seat"),
        ("床", "bed"),
        ("车", "car"),
        ("墙", "wall"),
    ):
        if zh in window:
            anchor = en
            break
    posture = ""
    if re.search(r"坐", window):
        posture = "sitting"
    elif re.search(r"站|立", window):
        posture = "standing"
    return PositionMark(name=name, side=side, facing=facing, anchor=anchor, posture=posture)


def _parse_marks_from_text(text: str, names: list[str]) -> list[PositionMark]:
    blob = text or ""
    if not blob:
        return []
    found: list[PositionMark] = []
    for name in names:
        if not name:
            continue
        hits = list(re.finditer(re.escape(name), blob))
        if not hits:
            continue
        merged = PositionMark(name=name)
        for match in hits:
            clause = re.split(r"[，。；;]", blob[match.start() :], 1)[0]
            after = blob[match.end() : match.end() + 14]
            merged = _merge_mark(merged, _window_mark(name, clause or after))
        if merged.side or merged.facing or merged.anchor or merged.posture:
            found.append(merged)
    for side, token in (("left", r"左(?:侧|边)?(?:是|为)"), ("right", r"右(?:侧|边)?(?:是|为)")):
        for match in re.finditer(token, blob):
            tail = blob[match.end() : match.end() + 8]
            for name in names:
                if name and tail.startswith(name):
                    found = _marks_from_items([*found, PositionMark(name=name, side=side)])
                    break
    return _marks_from_items(found)


def _default_marks(lead_names: list[str], extra_names: list[str]) -> list[PositionMark]:
    marks: list[PositionMark] = []
    if len(lead_names) == 1:
        marks.append(PositionMark(name=lead_names[0], side="center", facing="right", posture="standing"))
    elif len(lead_names) >= 2:
        marks.append(PositionMark(name=lead_names[0], side="left", facing="right", posture="standing"))
        marks.append(PositionMark(name=lead_names[-1], side="right", facing="left", posture="standing"))
        for name in lead_names[1:-1]:
            marks.append(PositionMark(name=name, side="center", facing="each-other", posture="standing"))
    for name in extra_names:
        marks.append(PositionMark(name=name, side="bg", facing="away", posture="standing"))
    return marks


def collect_position_marks(
    analysis: Analysis,
    clip: ClipPlan | VideoClip,
    shots: list[Shot],
    pictures: list[PictureBinding],
) -> list[PositionMark]:
    blob = _clip_blob(analysis, clip)
    lead_names, extra_names = _present_cast(analysis, blob, pictures)
    names = [*lead_names, *extra_names]
    plan = None
    idx = int(getattr(clip, "index", 0) or 0)
    for other in analysis.clips or []:
        if int(other.index) == idx:
            plan = other
            break
    layers = [
        _marks_from_items(getattr(shots[0], "marks", None) if shots else None),
        _marks_from_items(getattr(plan, "marks", None) if plan else None),
        _marks_from_items(getattr(analysis.visual, "marks", None) if analysis.visual else None),
        _parse_marks_from_text(
            " ".join(
                p
                for p in [
                    getattr(analysis.visual, "spatial_lock", "") if analysis.visual else "",
                    *(s.blocking or "" for s in shots),
                    *(s.start_state or "" for s in shots),
                    *(s.continuity or "" for s in shots),
                ]
                if p
            ),
            names,
        ),
    ]
    filled: list[PositionMark] = []
    for layer in layers:
        filled = _marks_from_items([*filled, *layer])
    have = {m.name for m in filled}
    for fallback in _default_marks(lead_names, extra_names):
        if fallback.name not in have:
            filled.append(fallback)
        else:
            for i, mark in enumerate(filled):
                if mark.name == fallback.name:
                    filled[i] = _merge_mark(mark, fallback)
                    break
    return filled


def _mark_en(mark: PositionMark) -> str:
    bits = [mark.name]
    if mark.side:
        bits.append(_SIDE_EN.get(mark.side, mark.side))
    if mark.posture:
        bits.append(mark.posture)
    if mark.anchor:
        bits.append(_ANCHOR_EN.get(mark.anchor, f"by the {mark.anchor}"))
    if mark.facing:
        bits.append(_FACE_EN.get(mark.facing, mark.facing))
    return " ".join(p for p in bits if p)


def _mark_zh(mark: PositionMark) -> str:
    bits = [mark.name]
    if mark.side:
        bits.append(_SIDE_ZH.get(mark.side, mark.side))
    if mark.facing:
        bits.append(_FACE_ZH.get(mark.facing, mark.facing))
    if mark.anchor:
        bits.append(_ANCHOR_ZH.get(mark.anchor, mark.anchor))
    if mark.posture:
        bits.append(_POSTURE_ZH.get(mark.posture, mark.posture))
    return "".join(bits) if len(bits) == 1 else "·".join(bits)


def _position_lock_sentence(marks: list[PositionMark]) -> str:
    if not marks:
        return (
            "POSITION LOCK: freeze this room's geography. Each person keeps one screen side "
            "and one floor mark. Do not swap left/right, do not mirror the set, do not teleport."
        )
    body = ". ".join(_mark_en(m) for m in marks)
    return (
        f"POSITION LOCK: {body}. These marks stay frozen for every shot in this clip. "
        "Hard cuts do not restage. Do not swap left/right. Do not mirror the room. "
        "A person only changes mark if they walk a visible path in this clip. "
        "Extras stay behind or to the side, never between the locked leads."
    )


def _position_lock_zh(marks: list[PositionMark]) -> str:
    return "；".join(_mark_zh(m) for m in marks) if marks else "本场站位不变"


def prompt_matches_clip(
    prompt: str,
    clip: ClipPlan | VideoClip,
    analysis: Analysis,
    pictures: list[PictureBinding] | None = None,
) -> bool:
    text = prompt or ""
    if "integrated_multimodal_description:" not in text:
        return False
    if "overall_soundscape:" not in text or "non_diegetic_music:" not in text:
        return False
    if "SPACE LOCK:" not in text:
        return False
    if "HEADCOUNT LOCK:" not in text:
        return False
    if "CAMERA LOCK:" not in text:
        return False
    if "SCENE LOCK:" not in text:
        return False
    if "LIGHT LOCK:" not in text:
        return False
    if "SPEECH LOCK:" not in text:
        return False
    if "POSITION LOCK:" not in text:
        return False
    dur = int(getattr(clip, "duration_seconds", 0) or 0)
    if dur:
        found = re.search(r"(\d+)\s*-?\s*second", text, re.I)
        if found and int(found.group(1)) != dur:
            return False
    shots = shots_for_clip(analysis, clip)
    this_blob = _clip_blob(analysis, clip)
    this_tok = _distinct_cn(this_blob)
    allowed = set(this_tok)
    allowed |= _distinct_cn(analysis.title)
    allowed |= _distinct_cn(analysis.style)
    allowed |= _distinct_cn(analysis.genre_anchor)
    if analysis.visual:
        allowed |= _distinct_cn(analysis.visual.look)
        allowed |= _distinct_cn(analysis.visual.lighting)
        allowed |= _distinct_cn(analysis.visual.color)
    for shot in shots:
        allowed |= _distinct_cn(shot.dialogue_speaker)
        allowed |= _distinct_cn(shot.dialogue_line)
    prev = previous_clip_plan(analysis, clip)
    for scene in analysis.scenes:
        allowed |= _distinct_cn(scene.name)
    for char in analysis.characters:
        allowed |= _distinct_cn(char.name)
        allowed |= _distinct_cn(" ".join(char.aliases or []))
        name = (char.name or "").strip()
        if len(name) >= 2:
            allowed.add(name[-2:])
    for scene in analysis.scenes:
        if _mentions(this_blob, scene.name):
            allowed |= _distinct_cn(scene.name)
    enrich_prop_aliases(analysis)
    for prop in analysis.props:
        allowed |= _distinct_cn(prop.name)
        allowed |= _distinct_cn(" ".join(prop.aliases or []))
    pic_map = {p.picture_index: p for p in pictures or []}
    uploaded: list[PictureBinding] = []
    for idx in getattr(clip, "picture_indices", []) or []:
        pic = pic_map.get(idx)
        if pic:
            uploaded.append(pic)
            allowed |= _distinct_cn(pic.name)
            linked = _char_for_picture(analysis, pic)
            if linked:
                allowed |= _distinct_cn(" ".join(character_alias_keys(linked)))
    for pic in pictures or []:
        if pic.kind == "character" and pic.picture_index in (getattr(clip, "picture_indices", None) or []):
            if pic.name and pic.name not in text:
                return False
    for i, pic in enumerate(uploaded, start=1):
        tag = f"<Picture {pic.picture_index}>"
        if tag not in text and pic.name not in text:
            return False
        if not re.search(
            rf"Image {i}\s*/\s*<Picture {pic.picture_index}>",
            text,
        ):
            return False
    for shot in shots:
        line = (shot.dialogue_line or "").replace("\\", "").strip()
        if line and line not in text:
            return False
        for field in (shot.performance, shot.start_state, shot.process, shot.result_state):
            token = (field or "").strip()
            if len(token) >= 6 and token in text:
                return False
    extra = _distinct_cn(text) - allowed
    if extra:
        return False
    if any(_is_contact_shot(s) for s in shots):
        if re.search(r"\binstant\b|suddenly|瞬间", text, re.I):
            return False
        if re.search(r"掐|举起|抬起", this_blob) and not re.search(
            r"contact|grip|boots|weight|hangs", text, re.I
        ):
            return False
    if any(_is_inspect_shot(s) for s in shots):
        if re.search(
            r"hold(?:s|ing)? .{0,24}(?:up |to |toward ).{0,16}(?:camera|lens)|举到镜头|对着镜头展示|凑到镜头",
            text,
            re.I,
        ):
            return False
        if INSPECT_PROMPT_MARK not in text and not re.search(
            r"over-shoulder|tilt down|high-angle", text, re.I
        ):
            return False
    for other in analysis.clips or []:
        if int(other.index) == int(clip.index):
            continue
        if prev and int(other.index) == int(prev.index):
            continue
        other_blob = _clip_blob(analysis, other)
        for token in _distinct_cn(other_blob):
            if len(token) < 4 or token in this_blob:
                continue
            if token in text:
                return False
    for prop in analysis.props:
        if is_body_feature_prop(prop.name):
            continue
        if prop_in_blob(this_blob, prop):
            continue
        if prop.name and prop.name in text:
            return False
    return True


def _canonicalize_cast(text: str, analysis: Analysis, pictures: list[PictureBinding]) -> str:
    if not text:
        return text
    attached = {p.name for p in pictures if p.kind == "character"}
    out = text
    for char in analysis.characters:
        if char.name not in attached:
            continue
        for alias in sorted(character_alias_keys(char), key=len, reverse=True):
            if alias == char.name:
                continue
            if alias in {"父亲", "爸爸"} and _MEMORY_FATHER_RE.search(out):
                continue
            if alias in out:
                out = out.replace(alias, char.name)
    return out


def _cast_lock_sentence(analysis: Analysis, pictures: list[PictureBinding]) -> str:
    parts: list[str] = []
    for i, pic in enumerate(pictures, start=1):
        if pic.kind != "character":
            continue
        extra = is_extra_picture(analysis, pic)
        if extra:
            parts.append(
                f"Image {i} binds extra {pic.name} as one unique background player; "
                "do not clone, mirror, or recast this extra."
            )
        else:
            parts.append(
                f"Image {i} binds {pic.name} as one unique person; "
                "all aliases of this name are the same actor; do not recast or duplicate."
            )
    return "CAST LOCK: " + " ".join(parts) if parts else ""


def _prop_lock_sentence(pictures: list[PictureBinding]) -> str:
    parts: list[str] = []
    for i, pic in enumerate(pictures, start=1):
        if pic.kind != "prop":
            continue
        parts.append(
            f"Image {i} / <Picture {pic.picture_index}> IS the {pic.name}; "
            f"the on-screen {pic.name} must match this image exactly; do not substitute."
        )
    return "PROP LOCK: " + " ".join(parts) if parts else ""


def clip_scene_names(analysis: Analysis, clip: ClipPlan | VideoClip) -> set[str]:
    scene = best_scene(analysis, clip)
    return {scene.name} if scene and scene.name else set()


def clips_share_scene(analysis: Analysis, a: ClipPlan | VideoClip, b: ClipPlan | VideoClip) -> bool:
    left = clip_scene_names(analysis, a)
    right = clip_scene_names(analysis, b)
    if left and right:
        return bool(left & right)
    return not left and not right


def previous_clip_plan(analysis: Analysis, clip: ClipPlan | VideoClip) -> ClipPlan | VideoClip | None:
    idx = int(getattr(clip, "index", 0) or 0)
    for other in analysis.clips or []:
        if int(other.index) == idx - 1:
            return other
    return None


def _space_lock_sentence(
    analysis: Analysis,
    clip: ClipPlan | VideoClip,
    shots: list[Shot],
    pictures: list[PictureBinding],
) -> str:
    visual = analysis.visual
    raw_lock = (visual.spatial_lock if visual else "") or ""
    lock = english_only(_canonicalize_cast(raw_lock, analysis, pictures))
    blocks: list[str] = []
    seen: set[str] = set()
    for shot in shots:
        text = english_only(
            _canonicalize_cast((shot.blocking or shot.continuity or "").strip(), analysis, pictures)
        )
        if text and text not in seen:
            seen.add(text)
            blocks.append(text)
    parts = [
        "SPACE LOCK:",
        lock + "." if lock else "",
        ("This clip blocking: " + " / ".join(blocks) + ".") if blocks else "",
        "Keep every named character on the same screen side (viewer's left vs right), "
        "the same seat or standing mark, the same facing, and the same side of the 180-degree line. "
        "Hard cuts only change shot size or angle; they do not restage the room, swap left/right, or teleport.",
    ]
    prev = previous_clip_plan(analysis, clip)
    if prev and clips_share_scene(analysis, prev, clip):
        prev_shots = shots_for_clip(analysis, prev)
        last = prev_shots[-1] if prev_shots else None
        landing = ""
        if last:
            landing = english_only(
                _canonicalize_cast(
                    (last.result_state or last.blocking or last.beat or "").strip(),
                    analysis,
                    pictures,
                )
            )
        parts.append(
            "MATCH CUT from the previous clip of this same scene: Shot 1 must continue "
            f"{landing or 'that last frame'}. Same geography, same character positions."
        )
    return " ".join(p for p in parts if p)


def with_prev_video_note(prompt: str) -> str:
    mark = "Reference Video 1 is the previous clip of this same scene"
    if mark in (prompt or ""):
        return prompt
    note = (
        f"{mark}. MATCH CUT from its LAST FRAME: copy that blocking exactly "
        "(screen-left/right, seats, eyelines, camera side of the line). This is a continuation, not a new setup."
    )
    text = prompt or ""
    token = "SPACE LOCK:"
    if token in text:
        return text.replace(token, note + " " + token, 1)
    return text.replace(
        "integrated_multimodal_description:",
        "integrated_multimodal_description: " + note,
        1,
    )


def assemble_h3_prompt(
    analysis: Analysis,
    clip: ClipPlan | VideoClip,
    pictures: list[PictureBinding],
) -> str:
    shots = shots_for_clip(analysis, clip)
    if not shots:
        shots = analysis.shots[:CLIP_PACK_MAX_SHOTS]
    duration = snap_h3_duration(getattr(clip, "duration_seconds", None) or (clip.end - clip.start))
    shots = coalesce_clip_shots(shots, duration)[:CLIP_PACK_MAX_SHOTS]
    ratio = analysis.aspect_ratio or "9:16"
    genre = analysis.genre_anchor or analysis.style or "live-action cinematic short drama"
    enrich_character_aliases(analysis)
    enrich_prop_aliases(analysis)
    hook = english_only(
        _canonicalize_cast(clip.hook or clip.title or analysis.logline, analysis, pictures)
    )
    blob = _clip_blob(analysis, clip)
    entity = _entity_lock_line(analysis, blob, pictures)
    prop_label = _prop_label(pictures)

    ref_lines = [picture_role_line(pic, i, analysis) for i, pic in enumerate(pictures, start=1)]
    cast_lock = _cast_lock_sentence(analysis, pictures)
    prop_lock = _prop_lock_sentence(pictures)
    headcount_lock = _headcount_lock_sentence(analysis, blob, pictures)
    camera_lock = _camera_lock_sentence(shots)
    scene_lock = _scene_lock_sentence(analysis, clip, pictures)
    lighting_lock = _lighting_lock_sentence(analysis, clip, pictures)
    speech_lock = _speech_lock_sentence(shots)
    marks = collect_position_marks(analysis, clip, shots, pictures)
    position_lock = _position_lock_sentence(marks)
    shot_lines: list[str] = []
    clip_start = shots[0].start if shots else clip.start
    prev_shot: Shot | None = None
    for i, shot in enumerate(shots, start=1):
        rel = max(0.0, shot.start - clip_start)
        visible = _canonicalize_cast(_visible_shot_body(shot, prop_label, marks), analysis, pictures)
        dialogue = ""
        line = (shot.dialogue_line or "").replace("\\", "").strip()
        if line:
            speaker = _canonicalize_cast((shot.dialogue_speaker or "").strip(), analysis, pictures)
            if re.search(r"系统|旁白|内心|^OS$|^VO$", speaker, re.I):
                dialogue = (
                    f'Off-screen system voice: [Chinese] "{line}". '
                    "No mouth movement, not spoken by any on-screen person."
                )
            else:
                dialogue = f'{speaker or "the character"} says: [Chinese] "{line}", natural lip sync.'
        else:
            dialogue = "No spoken line in this shot."
        head = _shot_head(i, rel, visible, prev_shot, shot)
        shot_lines.append(" ".join(p for p in [head, dialogue, _camera_line(shot, prop_label)] if p))
        prev_shot = shot

    negatives = (
        "No on-screen text, subtitles, logos, watermarks, extra leads, duplicated people, "
        "mirrored twins, cloned faces, leftover extra bodies, storyboard grids, "
        "looking into camera, identity swap, recasting the same named person, "
        "sudden hugging or kneeling unless scripted."
    )
    if re.search(r"掐|脖子|喉", blob):
        negatives += (
            " No teleport grabs, no rubber limbs, no extra arms, no identity morph, "
            "no levitation before a locked grip, no feet leaving the ground before the hand is on the neck."
        )
    elif any(_is_contact_shot(s) for s in shots):
        negatives += " No teleport hits, no rubber limbs, no extra arms, no identity morph."
    if any(_is_inspect_shot(s) for s in shots):
        negatives += (
            " No vlog, no presenting a phone or photo to the filming lens, "
            "no evidence-to-audience framing."
        )
    negatives += " No screen-direction flips, no restaging, no teleporting people between shots."
    negatives += " No unmotivated rapid cutting; no random angle changes every few seconds."
    space_lock = _space_lock_sentence(analysis, clip, shots, pictures)

    description = " ".join(
        [
            f"{duration}-second, {ratio} live-action short-drama clip.",
            f"Genre: {genre}." if not has_cjk(genre) else "Genre: live-action cinematic short drama.",
            entity + ".",
            headcount_lock,
            camera_lock,
            scene_lock,
            lighting_lock,
            position_lock,
            cast_lock,
            prop_lock,
            space_lock,
            speech_lock,
            f"This clip's single turn: {hook}." if hook else "",
            " ".join(shot_lines),
            space_lock,
            cast_lock,
            prop_lock,
            " ".join(ref_lines),
            negatives,
        ]
    )

    sounds: list[str] = []
    for shot in shots:
        rel = max(0.0, shot.start - clip_start)
        fx = [s for s in (shot.sound or []) if s and not has_cjk(s)]
        if fx:
            sounds.append(f"At {fmt_h3_ts(rel)}, " + ", ".join(fx))
    if not sounds:
        sounds = ["Natural room tone and physically accurate foley for this clip only"]
    if any(_is_contact_shot(s) for s in shots):
        sounds.append(
            "Physically accurate contact foley: boots, metal or cloth impact, body weight, no cartoon whoosh"
        )
    soundscape = ". ".join(sounds) + "."
    if re.search(r"战斗|枪声|掐|杀|追|士兵|枪|爆炸", blob):
        music = "A sparse low tension drone enters at 0s and cuts off when the clip's turning action resolves."
    else:
        music = "N/A"

    prompt = (
        f"integrated_multimodal_description: {description.strip()}\n\n"
        f"overall_soundscape: {soundscape}\n\n"
        f"non_diegetic_music: {music}"
    )
    return _truncate(prompt)


def seedance_ref_line(pic: PictureBinding, image_no: int) -> str:
    name = (pic.name or "参考").strip()
    if pic.kind == "character":
        return f"图片{image_no}锁定【{name}】的脸和衣服，只当演员，不要抄姿势和背景"
    if pic.kind == "scene":
        return f"图片{image_no}锁定【{name}】的空间和现场环境光，不要另外打灯"
    return f"图片{image_no}锁定【{name}】为手里正在用的东西，不要举到镜头前"


def pictures_for_clip(
    pictures: list[PictureBinding], clip: ClipPlan | VideoClip
) -> list[PictureBinding]:
    by_index = {p.picture_index: p for p in pictures}
    out: list[PictureBinding] = []
    for idx in getattr(clip, "picture_indices", None) or []:
        pic = by_index.get(idx)
        if pic:
            out.append(pic)
    return out


_SEEDANCE_REF_PREFIX = re.compile(
    r"^(?:【参考图绑定】)?(?:(?:参考图|图片)\d+锁定【[^】]+】[^；。]*[；。]\s*)+"
)


def strip_seedance_ref_prefix(text: str) -> str:
    out = (text or "").strip()
    while True:
        m = _SEEDANCE_REF_PREFIX.match(out)
        if not m:
            break
        out = out[m.end() :].lstrip()
    out = re.sub(r"上传顺序：图片\d+为第\d+张[^。]*。\s*", "", out)
    return out.strip()


def seedance_ref_header(
    pictures: list[PictureBinding], audio_pictures: list[PictureBinding] | None = None
) -> str:
    if not pictures:
        return ""
    lines = [seedance_ref_line(pic, i) for i, pic in enumerate(pictures, start=1)]
    audio_lines = []
    audio_i = 1
    for pic in audio_pictures if audio_pictures is not None else pictures:
        if pic.kind != "character":
            continue
        if not (getattr(pic, "seedance_audio_asset_id", "") or getattr(pic, "voice_filename", "")):
            continue
        name = (pic.name or "").strip() or "角色"
        audio_lines.append(f"参考音频{audio_i}锁定【{name}】的声线、口音与说话方式，该角色开口时口型跟随对白")
        audio_i += 1
        if audio_i > 3:
            break
    extra = ("。" + "；".join(audio_lines) + "。") if audio_lines else "。"
    return (
        "【参考图绑定】"
        + "；".join(lines)
        + extra
        + "上传顺序：图片1为第1张上传图，图片2为第2张，以此类推。"
    )


def finalize_seedance_prompt(
    prompt: str,
    pictures: list[PictureBinding],
    audio_pictures: list[PictureBinding] | None = None,
) -> str:
    body = strip_seedance_ref_prefix(prompt or "")
    header = seedance_ref_header(pictures, audio_pictures=audio_pictures)
    if not header:
        return _truncate(body, SEEDANCE_MAX_TEXT)
    combined = header + body
    return _truncate(combined, SEEDANCE_MAX_TEXT)


def seedance_prompt_matches_clip(
    prompt: str,
    clip: ClipPlan | VideoClip,
    analysis: Analysis,
    pictures: list[PictureBinding] | None = None,
) -> bool:
    text = prompt or ""
    if not text.strip():
        return False
    uploaded = pictures_for_clip(pictures or [], clip)
    if not uploaded:
        return True
    for i, pic in enumerate(uploaded, start=1):
        name = (pic.name or "").strip()
        if not name:
            continue
        if name not in text:
            return False
        if not re.search(rf"(?:图片|参考图){i}\s*锁定", text):
            return False
    return True


_WALK_RE = re.compile(
    r"走|迈步|跑步|跑向|冲出|冲向|下床|起床|出门|进入|离开|穿过|上楼|下楼|走向|走到|跑开|跟上|转身走"
)
_EXAGGERATED_FACE_RE = re.compile(
    r"慌张|震惊|惊恐|崩溃|瞪大|眼睛睁大|眼睁大|嘴大张|嘴微张|张大嘴|歇斯底里|"
    r"痛哭|泪崩|表情夸张|夸张表情|夸张哭|夸张皱眉|狂喜|绝望|大喊|激动得|怒吼|瞳孔放大"
)


def _is_walk_shot(shot: Shot) -> bool:
    return bool(_WALK_RE.search(_action_src(shot) + " " + (shot.result_state or "")))


def _clip_isolation(clip: ClipPlan | VideoClip) -> bool:
    blob = f"{clip.hook or ''} {clip.title or ''}"
    return bool(re.search(r"空|无人|安静|愣|只剩|空荡", blob))


def _naturalize_zh(text: str) -> str:
    out = text or ""
    out = re.sub(r"举起(手机|照片|相片|屏幕|证件|纸条)", r"拿起\1", out)
    out = re.sub(r"把(手机|照片|相片|屏幕)举到镜头", r"低头看自己手里的\1", out)
    out = re.sub(r"对着镜头展示", "自己看", out)
    out = re.sub(r"逐渐受光|突然受光|打光上脸|补光打脸", "光比保持原样", out)
    return out


def _seedance_move_zh(move: str) -> str:
    text = (move or "").strip()
    lowered = text.lower()
    if "arc" in lowered or "环绕" in text or "转圈" in text:
        return "固定机位，不要围着人转"
    if "push" in lowered or "推近" in text or "推进" in text:
        return "机位略前移，不要围着转"
    if "pull" in lowered or "拉远" in text or "拉出" in text:
        return "机位略后退，露出环境"
    if "track" in lowered or "跟" in text or "follow" in lowered:
        return "跟着人走，速度对齐脚步，不要绕圈"
    if "tilt" in lowered or "下摇" in text or "上摇" in text:
        return "视线跟着人看的方向，一次到位"
    if "pan" in lowered or "横摇" in text:
        return "横摇跟着视线，一次到位"
    return "固定机位，人自己动"


def _clip_scene_mode(
    analysis: Analysis,
    clip: ClipPlan | VideoClip,
    pictures: list[PictureBinding],
) -> str:
    scene = _clip_scene(analysis, clip, pictures)
    text = " ".join(
        [
            _scene_match_blob(analysis, clip),
            scene.name if scene else "",
            getattr(clip, "title", "") or "",
            getattr(clip, "hook", "") or "",
        ]
    )
    if _INTIMATE_RE.search(text) and not _PLEASURE_RE.search(text):
        return "intimate"
    if _PLEASURE_RE.search(text):
        return "pleasure"
    if _SCALE_SPACE_RE.search(text):
        return "void_scale"
    return "normal"


def _scene_emphasis_zh(
    analysis: Analysis,
    clip: ClipPlan | VideoClip,
    pictures: list[PictureBinding],
) -> str:
    mode = _clip_scene_mode(analysis, clip, pictures)
    scene = _clip_scene(analysis, clip, pictures)
    name = scene.name if scene else ""
    if mode == "pleasure":
        if re.search(r"酒店|泳池", name):
            return "先看见总统套房泳池有多长、落地窗外空城有多远，大理石和水面反光。一个人占整层楼。"
        if re.search(r"商场", name):
            return "先看见空荡的多层中庭和奢侈品店，人只占一角，购物车排开。像这地方的主人一样挥手全要，人小场子大。"
        return "先看见这地方有多大、多空、多贵，人再进画。身体放得开，不要贴脸。"
    if mode == "void_scale":
        return "先看见几百平客厅的层高和全开的灯，人躺在沙发上显得很小。空、贵、只剩他一个。"
    return ""


def _effective_shot_size(shot: Shot, mode: str) -> str:
    size = (shot.shot_size or "中近景").strip()
    if mode in {"pleasure", "void_scale"}:
        if _CLOSE_SIZE_RE.search(size):
            return "全景"
        if _MED_SIZE_RE.search(size):
            return "中全景"
    return size or "中近景"


def _seedance_camera_note(shot: Shot, mode: str = "normal") -> str:
    move = (shot.camera_move or shot.camera or "").strip()
    lowered = move.lower()
    static = (not move) or "static" in lowered or "固定" in move
    if _is_inspect_shot(shot) and mode == "intimate":
        if static:
            return "人低头看自己手里的东西，机位不动，不要围着转，不要把东西举到镜头前"
        return "人低头看自己手里的东西，机位最多略近一点，不要围着转，不要把东西举到镜头前"
    if mode in {"pleasure", "void_scale"}:
        if static:
            return "全景看空间，人小场子大，机位略远，不要推近脸"
        if "pull" in lowered or "拉远" in move or "拉出" in move:
            return "略拉远，露出层高和空荡，人要小"
        if "track" in lowered or "跟" in move or "follow" in lowered:
            return "跟人走，始终把空荡的大厅留在画里，不要推近脸"
        return "人小空间大，不要推近脸"
    if _is_inspect_shot(shot):
        if static:
            return "人低头看自己手里的东西，机位不动，不要围着转，不要把东西举到镜头前"
        return "人低头看自己手里的东西，机位最多略近一点，不要围着转，不要把东西举到镜头前"
    if static:
        return "固定机位，不要转圈、不要手持乱晃"
    return _seedance_move_zh(move)


def _seedance_performance_zh(shot: Shot, mode: str = "normal") -> str:
    raw = shot.performance or ""
    if mode != "pleasure":
        raw = _EXAGGERATED_FACE_RE.sub("", raw)
    raw = re.sub(r"[，、\s]{2,}", "，", raw).strip("，、。 ")
    if mode == "pleasure":
        return raw or "身体松、占地方，动作放开"
    return raw


def _seedance_motion_zh(shot: Shot) -> str:
    process = _naturalize_zh(_strip_instant(shot.process or ""))
    process = re.sub(r"冲出房间|冲出|冲向", "走向", process)
    process = re.sub(r"跳下床", "先坐起，双脚先后落地再站起来", process)
    if _is_walk_shot(shot) or _WALK_RE.search(process):
        gait = "走路左右脚交替、有重量，禁止滑步"
        return f"{process}，{gait}" if process else gait
    return process


def _lighting_lock_zh(
    analysis: Analysis,
    clip: ClipPlan | VideoClip,
    pictures: list[PictureBinding],
) -> str:
    scene = _clip_scene(analysis, clip, pictures)
    core = ""
    if scene and str(scene.lighting or "").strip():
        core = str(scene.lighting).strip().rstrip("。；")
    elif analysis.visual and str(analysis.visual.lighting or "").strip():
        core = str(analysis.visual.lighting).strip().rstrip("。；")
    else:
        core = "跟随场景参考图的环境光"
    core = re.sub(r"一侧脸亮[^；。]*", "", core)
    core = re.sub(r"走近灯下[^；。]*", "", core)
    core = re.sub(r"[；。]{2,}", "；", core).strip("；。 ")
    return f"{core}。整段光比不变，没有突然补光或追光打脸"


def seedance_craft_suffix(
    analysis: Analysis,
    clip: ClipPlan | VideoClip,
    pictures: list[PictureBinding],
) -> str:
    return ""


def _seedance_shot_line(
    i: int,
    shot: Shot,
    rel: float,
    prev: Shot | None,
    clip: ClipPlan | VideoClip,
    mode: str = "normal",
) -> str:
    if i == 1:
        head = f"【镜头{i}】"
    elif prev and _is_continuous_camera(shot):
        head = f"【镜头{i}】{fmt_h3_ts(rel)}续"
    else:
        head = f"【镜头{i}】"
    size = _effective_shot_size(shot, mode)
    chunks = [f"{head}{size}"]
    start = _naturalize_zh((shot.start_state or "").strip())
    if start:
        chunks.append(start)
    process = _seedance_motion_zh(shot)
    if process:
        chunks.append(process)
    result = _naturalize_zh((shot.result_state or "").strip())
    if result:
        chunks.append(result)
    face = _seedance_performance_zh(shot, mode)
    if face:
        chunks.append(face)
    if re.search(r"白光", " ".join([start, process, result])):
        chunks.append("白光只闪一下，灭了还是原来的现场光")
    line = (shot.dialogue_line or "").replace("\\", "").strip()
    speaker = (shot.dialogue_speaker or "").strip()
    speak_at = 0.6 if i == 1 else max(0.6, rel)
    ts = fmt_h3_ts(speak_at)
    if line:
        if re.search(r"系统|旁白|内心|^OS$|^VO$", speaker, re.I):
            chunks.append(f"{ts}画外：「{line}」")
        else:
            chunks.append(f"{ts}{speaker or '角色'}说：「{line}」，先入画再张嘴")
    chunks.append(_seedance_camera_note(shot, mode))
    return "。".join(c for c in chunks if c) + "。"


def assemble_seedance_prompt(
    analysis: Analysis,
    clip: ClipPlan | VideoClip,
    pictures: list[PictureBinding],
) -> str:
    shots = shots_for_clip(analysis, clip)
    if not shots:
        shots = analysis.shots[:CLIP_PACK_MAX_SHOTS]
    duration = snap_h3_duration(getattr(clip, "duration_seconds", None) or (clip.end - clip.start))
    shots = coalesce_clip_shots(shots, duration)[:CLIP_PACK_MAX_SHOTS]
    ratio = analysis.aspect_ratio or "9:16"

    hook = _naturalize_zh((clip.hook or clip.title or "").strip())
    lighting_lock = _lighting_lock_zh(analysis, clip, pictures)
    clip.lighting_lock = lighting_lock
    position_lock = getattr(clip, "position_lock", "") or ""
    headcount_lock = getattr(clip, "headcount_lock", "") or ""
    scene_lock = getattr(clip, "scene_lock", "") or ""
    mode = _clip_scene_mode(analysis, clip, pictures)
    emphasis = _scene_emphasis_zh(analysis, clip, pictures)

    setup = [
        f"{duration}秒，{ratio}。",
        f"{headcount_lock}。" if headcount_lock else "",
        f"{scene_lock}。" if scene_lock else "",
        f"{lighting_lock}。",
        f"{position_lock}。" if position_lock else "",
    ]

    clip_start = shots[0].start if shots else clip.start
    shot_lines: list[str] = []
    prev_shot: Shot | None = None
    for i, shot in enumerate(shots, start=1):
        rel = max(0.0, shot.start - clip_start)
        shot_lines.append(_seedance_shot_line(i, shot, rel, prev_shot, clip, mode))
        prev_shot = shot

    parts = [
        "".join(setup),
        f"{hook}。" if hook else "",
        f"{emphasis}" if emphasis else "",
        "".join(shot_lines),
        "禁止：举道具给镜头、围着人转圈、突然打光、直视镜头、字幕水印、滑步。",
    ]
    prompt = "".join(p for p in parts if p)
    return finalize_seedance_prompt(prompt, pictures)


def fill_clip_locks(
    clip: VideoClip,
    analysis: Analysis,
    shots: list[Shot],
    pictures: list[PictureBinding],
) -> VideoClip:
    first = shots[0] if shots else None
    move = ""
    size = "中近景"
    if first:
        move = (first.camera_move or first.camera or "Static Shot").strip()
        size = (first.shot_size or "中近景").strip()
    mode = _clip_scene_mode(analysis, clip, pictures)
    if mode in {"pleasure", "void_scale"}:
        size = "全景"
        lowered = (move or "").lower()
        if "push" in lowered or "推近" in move or "推进" in move:
            move = "Pull Out" if mode == "void_scale" else (move or "Tracking Shot")
    clip.camera_lock = f"{size} / {move or 'Static Shot'} / 不越轴"
    blob = _clip_blob(analysis, clip)
    lead_names, extra_names = _present_cast(analysis, blob, pictures)
    parts = []
    if lead_names:
        parts.append(f"{len(lead_names)}主演")
    if extra_names:
        parts.append(f"{len(extra_names)}群演")
    who = "、".join([*lead_names, *extra_names])
    clip.headcount_lock = ("+".join(parts) or "0人") + (f"：{who}" if who else "")
    scene = best_scene(analysis, clip)
    if scene and scene.name:
        clip.scene_lock = scene.name
    else:
        fallback = next((p.name for p in pictures if p.kind == "scene" and p.name), "")
        clip.scene_lock = fallback or "本场"
    clip.lighting_lock = _lighting_lock_zh(analysis, clip, pictures)
    clip.position_lock = _position_lock_zh(collect_position_marks(analysis, clip, shots, pictures))
    return clip


def clip_director_notes(analysis: Analysis, clip: ClipPlan | VideoClip, shots: list[Shot], pictures: list[PictureBinding]) -> str:
    pic_txt = "、".join(f"<Picture {p.picture_index}> {p.name}" for p in pictures) or "无参考图"
    shot_txt = "；".join(f"{s.beat or '镜头'} {s.start:.0f}–{s.end:.0f}s" for s in shots) or "按提示词"
    lock = ""
    if analysis.visual and analysis.visual.spatial_lock:
        lock = f"\n站位：{analysis.visual.spatial_lock}"
    elif any(s.blocking for s in shots):
        lock = "\n站位：" + "；".join(s.blocking for s in shots if s.blocking)
    camera = getattr(clip, "camera_lock", "") or ""
    headcount = getattr(clip, "headcount_lock", "") or ""
    scene = getattr(clip, "scene_lock", "") or ""
    lighting = getattr(clip, "lighting_lock", "") or ""
    position = getattr(clip, "position_lock", "") or ""
    setup = ""
    if camera or headcount or scene or lighting or position:
        setup = f"\n机位：{camera} · 人数：{headcount} · 场景：{scene} · 灯光：{lighting} · 位置：{position}"
    return (
        f"{clip.title or f'片段 {clip.index}'} · {clip.duration_seconds}s · {clip.hook or analysis.logline}\n"
        f"参考：{pic_txt}\n镜头：{shot_txt}{lock}{setup}"
    )


def build_video_clips(analysis: Analysis, pictures: list[PictureBinding]) -> list[VideoClip]:
    enrich_character_aliases(analysis)
    enrich_prop_aliases(analysis)
    pack_clips(analysis)
    clips: list[VideoClip] = []
    ready = [p for p in pictures if p.status == "ready"]
    pool = ready if ready else []
    for plan in analysis.clips:
        raw_shots = shots_for_clip(analysis, plan)
        shots = coalesce_clip_shots(raw_shots, plan.duration_seconds)
        selected = select_clip_pictures(analysis, plan, pool, shots) if pool else []
        clip = VideoClip(
            index=plan.index,
            title=plan.title,
            hook=plan.hook,
            start=plan.start,
            end=plan.end,
            duration_seconds=plan.duration_seconds,
            picture_indices=[p.picture_index for p in selected],
        )
        fill_clip_locks(clip, analysis, shots, selected)
        clip.director_notes = clip_director_notes(analysis, clip, shots, selected)
        clip.h3_prompt = assemble_h3_prompt(analysis, clip, selected)
        clip.seedance_prompt = assemble_seedance_prompt(analysis, clip, selected)
        clips.append(clip)
    return clips


def apply_llm_prompts(
    clips: list[VideoClip],
    payload: dict,
    analysis: Analysis | None = None,
    pictures: list[PictureBinding] | None = None,
) -> list[VideoClip]:
    rows = payload.get("clips") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return clips
    by_index = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            idx = int(row.get("index") or 0)
        except (TypeError, ValueError):
            continue
        prompt = str(row.get("h3_prompt") or "").strip()
        if idx and prompt:
            by_index[idx] = _truncate(prompt)
    for clip in clips:
        cand = by_index.get(clip.index)
        if not cand:
            continue
        if analysis is not None and not prompt_matches_clip(cand, clip, analysis, pictures):
            continue
        clip.h3_prompt = cand
    return clips


def apply_seedance_llm_prompts(
    clips: list[VideoClip],
    payload: dict,
    analysis: Analysis | None = None,
    pictures: list[PictureBinding] | None = None,
) -> list[VideoClip]:
    rows = payload.get("clips") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return clips
    by_index: dict[int, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            idx = int(row.get("index") or 0)
        except (TypeError, ValueError):
            continue
        prompt = str(row.get("seedance_prompt") or "").strip()
        if idx and prompt:
            by_index[idx] = _truncate(prompt, SEEDANCE_MAX_TEXT)
    for clip in clips:
        cand = by_index.get(clip.index)
        if not cand:
            continue
        if analysis is not None and not seedance_prompt_matches_clip(cand, clip, analysis, pictures):
            continue
        uploaded = pictures_for_clip(pictures or [], clip)
        clip.seedance_prompt = finalize_seedance_prompt(cand, uploaded)
    return clips


def join_episode_prompt(clips: list[VideoClip]) -> str:
    blocks = []
    for clip in clips:
        head = f"========== CLIP {clip.index} · {clip.title or '片段'} · {clip.duration_seconds}s =========="
        blocks.append(head + "\n" + (clip.h3_prompt or "").strip())
    return "\n\n".join(blocks).strip()


def join_episode_seedance_prompt(clips: list[VideoClip]) -> str:
    blocks = []
    for clip in clips:
        head = f"========== CLIP {clip.index} · {clip.title or '片段'} · {clip.duration_seconds}s · Seedance =========="
        blocks.append(head + "\n" + (clip.seedance_prompt or clip.h3_prompt or "").strip())
    return "\n\n".join(blocks).strip()


def clip_briefs_text(
    clips: list[VideoClip],
    pictures: list[PictureBinding],
    analysis: Analysis | None = None,
) -> str:
    pic_map = {p.picture_index: p for p in pictures}
    lines = []
    for clip in clips:
        uploaded = []
        for i, idx in enumerate(clip.picture_indices, start=1):
            pic = pic_map.get(idx)
            if pic:
                aka = ""
                if analysis is not None:
                    linked = _char_for_picture(analysis, pic)
                    if linked and linked.aliases:
                        aka = f"，别称{'/'.join(linked.aliases[:4])}"
                uploaded.append(f"图片{i}=本集参考图{idx} {pic.name}{aka}（{pic.kind}）")
        shot_txt = ""
        if analysis is not None:
            shots = shots_for_clip(analysis, clip)
            shot_txt = " | 本段镜头：" + "；".join(
                f"{s.beat or '镜头'}: {(s.blocking or s.process or s.start_state or '').strip()}"
                for s in shots
            )
            if analysis.visual and analysis.visual.spatial_lock:
                shot_txt += f" | 站位锁：{analysis.visual.spatial_lock}"
            pos = getattr(clip, "position_lock", "") or ""
            if pos:
                shot_txt += f" | 位置锁：{pos}"
        lines.append(
            f"Clip {clip.index}: {clip.title} | {clip.duration_seconds}s | {clip.start:g}–{clip.end:g}s | "
            f"hook={clip.hook}{shot_txt} | 上传顺序：{'; '.join(uploaded) or '无参考图'} | "
            "只许写本段 hook 和本段镜头，禁止写其他 clip 或其他集的情节"
        )
    return "\n".join(lines)


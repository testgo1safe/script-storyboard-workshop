"""整理系列 bd67b0b0db66：修复第二集、删除重复集、标记未生成的集。"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from app.config import PROJECTS_DIR
from app.storage import (
    delete_episode,
    drop_state_cache,
    load_series,
    load_state,
    save_series,
    save_state,
)

SERIES_ID = "bd67b0b0db66"
EP2_FAILED = "439ca0cd6f03"
EP2_GOOD = "12a474de95d7"
DUP_IDS = ["12a474de95d7", "4cb7ae8b0628"]


def merge_ep2_from_success() -> None:
    src = PROJECTS_DIR / EP2_GOOD
    dest = PROJECTS_DIR / EP2_FAILED
    if not src.exists():
        raise SystemExit(f"缺少成功副本 {EP2_GOOD}")
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    state = load_state(EP2_FAILED)
    state.id = EP2_FAILED
    state.episode_index = 2
    state.episode_title = "第二集 · 第一条短信指向丈夫"
    state.series_id = SERIES_ID
    state.series_name = load_series(SERIES_ID).name
    save_state(state)
    drop_state_cache(EP2_FAILED)
    print(f"已将 {EP2_GOOD} 的成功数据迁回第二集 {EP2_FAILED}")


def remove_duplicate_episodes() -> None:
    series = load_series(SERIES_ID)
    keep = [ep for ep in series.episodes if ep.project_id not in DUP_IDS]
    if len(keep) == len(series.episodes):
        print("series.json 中无重复条目需移除")
    else:
        series.episodes = keep
        for i, ep in enumerate(series.episodes, start=1):
            ep.index = i
        save_series(series)
        print(f"series.json 已整理为 {len(keep)} 集")

    for pid in DUP_IDS:
        path = PROJECTS_DIR / pid
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            drop_state_cache(pid)
            print(f"已删除重复项目目录 {pid}")


def reindex_episode_states() -> None:
    series = load_series(SERIES_ID)
    for i, ep in enumerate(series.episodes, start=1):
        try:
            state = load_state(ep.project_id)
        except FileNotFoundError:
            continue
        state.episode_index = i
        label = f"第{i}集"
        title = (ep.title or "").strip()
        if title and title not in {label, f"第 {i} 集"}:
            state.episode_title = f"{label} · {title}"
        else:
            state.episode_title = title or label
        save_state(state)
    print("各集 episode_index / episode_title 已对齐")


def mark_pending_episodes() -> None:
    series = load_series(SERIES_ID)
    msg = "拆集后尚未生成。请选中本集，点「重新生成本集分镜」。"
    for ep in series.episodes:
        try:
            state = load_state(ep.project_id)
        except FileNotFoundError:
            continue
        if state.status == "queued" and not state.analysis:
            state.status = "error"
            state.message = msg
            state.error = msg
            state.progress = 0
            save_state(state)
            print(f"  标记待生成：第{ep.index}集 {ep.title} ({ep.project_id})")


def main() -> None:
    merge_ep2_from_success()
    remove_duplicate_episodes()
    reindex_episode_states()
    mark_pending_episodes()
    series = load_series(SERIES_ID)
    print(f"\n完成。当前共 {len(series.episodes)} 集。")


if __name__ == "__main__":
    main()


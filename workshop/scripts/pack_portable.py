"""把工坊和出图服务打成另一台电脑能用的便携目录 + zip。"""
from __future__ import annotations

import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE_SRC = Path(r"C:\path\to\chatgpt-web-api")
VIDEO_ENV = Path(r"C:\path\to\video") / ".env"
BUNDLE = Path(r"C:\path\to\portable_bundle")
ZIP_PATH = Path(r"C:\path\to\portable_bundle.zip")
STARTERS = ROOT / "scripts" / "portable"

WORKSHOP_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".cursor",
    "tmp_frames",
    "tmp_new_frames",
    "_compare_frames",
}
IMAGE_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "output",
    ".chrome-profile",
}


def copy_tree(src: Path, dst: Path, skip_dirs: set[str]) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)

    def ignore(directory: str, names: list[str]) -> set[str]:
        dropped: set[str] = set()
        for name in names:
            if name in skip_dirs or name.endswith(".pyc"):
                dropped.add(name)
            if name == ".runtime-pids.txt":
                dropped.add(name)
        return dropped

    shutil.copytree(src, dst, dirs_exist_ok=True, ignore=ignore)


def read_env(path: Path) -> dict[str, str]:
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


def merge_video_keys(dest_env: Path) -> None:
    extra = read_env(VIDEO_ENV)
    if not extra:
        return
    current = read_env(dest_env)
    lines = dest_env.read_text(encoding="utf-8").splitlines() if dest_env.exists() else []
    added = False
    for key, value in extra.items():
        if not key.startswith("DOUBAO_") and key not in {
            "VOLCENGINE_ACCESS_KEY",
            "VOLCENGINE_SECRET_KEY",
            "VOLC_ACCESS_KEY",
            "VOLC_SECRET_KEY",
            "ARK_PROJECT_NAME",
        }:
            continue
        if current.get(key):
            continue
        if not added:
            lines.append("")
            lines.append("# 便携包合并的配音密钥")
            added = True
        lines.append(f"{key}={value}")
    dest_env.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def zip_dir(src: Path, dest: Path) -> None:
    if dest.exists():
        dest.unlink()
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for path in src.rglob("*"):
            if path.is_dir():
                continue
            if ".venv" in path.parts or "__pycache__" in path.parts:
                continue
            zf.write(path, path.relative_to(src))


def main() -> int:
    if not IMAGE_SRC.exists():
        print("找不到出图项目：", IMAGE_SRC, file=sys.stderr)
        return 1
    if BUNDLE.exists():
        shutil.rmtree(BUNDLE)
    BUNDLE.mkdir(parents=True)

    print("复制工坊…")
    copy_tree(ROOT, BUNDLE / "workshop", WORKSHOP_SKIP_DIRS)
    merge_video_keys(BUNDLE / "workshop" / ".env")

    print("复制出图服务…")
    copy_tree(IMAGE_SRC, BUNDLE / "image-api", IMAGE_SKIP_DIRS)

    for name in ("启动.bat", "启动.ps1", "停止.bat", "停止.ps1", "使用说明.txt"):
        shutil.copy2(STARTERS / name, BUNDLE / name)

    print("压缩…", ZIP_PATH)
    zip_dir(BUNDLE, ZIP_PATH)
    workshop_mb = sum(p.stat().st_size for p in (BUNDLE / "workshop").rglob("*") if p.is_file()) / 1_000_000
    image_mb = sum(p.stat().st_size for p in (BUNDLE / "image-api").rglob("*") if p.is_file()) / 1_000_000
    zip_mb = ZIP_PATH.stat().st_size / 1_000_000
    print(f"完成：{BUNDLE}")
    print(f"工坊约 {workshop_mb:.0f} MB，出图约 {image_mb:.0f} MB，压缩包 {zip_mb:.0f} MB")
    print(f"压缩包：{ZIP_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


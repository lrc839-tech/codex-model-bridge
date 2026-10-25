#!/usr/bin/env python3
"""Create a reproducible, source-only macOS ZIP from an explicit allowlist."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import sys
import zipfile

VERSION = "0.1.0"
PACKAGE_NAME = "codex-model-bridge"
ROOT = Path(__file__).resolve().parent.parent
ROOT_FILES = {
    "README.md", "LICENSE", "CHANGELOG.md", "SECURITY.md", "CONTRIBUTING.md",
    "install.command", "configure.command", "uninstall.command", "plugin.json", ".gitignore",
}
TREE_EXTENSIONS = {
    "skills": {".py", ".md", ".yaml", ".yml", ".json", ".html", ".css", ".js", ".svg", ".txt"},
    "scripts": {".py", ".sh"},
    "tests": {".py", ".json", ".txt"},
    "docs": {".md", ".png", ".svg", ".jpg", ".jpeg", ".json"},
    "examples": {".json", ".py", ".md", ".txt"},
    ".github": {".yml", ".yaml", ".md"},
    ".agents": {".json"},
}
EXCLUDED_PARTS = {"__pycache__", ".git", ".venv", "runs", "logs", "dist", "build", ".pytest_cache"}
EXCLUDED_NAMES = {".DS_Store", ".bridge-install.json", ".bridge-python", "auth.json", "credentials.json", "profile.json"}
PRIVATE_PATH = re.compile(r"/Users/[A-Za-z0-9_.-]+/")
LIKELY_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{24,}\b")
TEXT_EXTENSIONS = {".py", ".md", ".json", ".yaml", ".yml", ".html", ".css", ".js", ".sh", ".command", ".txt", ".svg", ""}


class ReleaseError(Exception):
    pass


def select_files(root: Path) -> list[Path]:
    selected = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if set(relative.parts) & EXCLUDED_PARTS or path.name in EXCLUDED_NAMES or path.name.startswith(".env"):
            continue
        allowed = (
            relative.as_posix() in ROOT_FILES
            or (len(relative.parts) > 1 and relative.parts[0] in TREE_EXTENSIONS and path.suffix.lower() in TREE_EXTENSIONS[relative.parts[0]])
        )
        if not allowed or path.is_dir():
            continue
        if path.is_symlink():
            raise ReleaseError("不能打包符号链接：" + relative.as_posix())
        selected.append(path)
    for required in ("README.md", "LICENSE", "install.command", "configure.command", "uninstall.command", "scripts/install.py", "scripts/mac_launcher.sh", "skills/codex-model-bridge/SKILL.md", "skills/codex-model-bridge/scripts/bridge.py", "skills/codex-model-bridge/runtime/config.py", "skills/codex-model-bridge/ui/index.html"):
        if root / required not in selected:
            raise ReleaseError("缺少发布文件：" + required)
    return selected


def validate_contents(path: Path, data: bytes, root: Path) -> None:
    relative = path.relative_to(root).as_posix()
    if len(data) > 5 * 1024 * 1024:
        raise ReleaseError("单个文件超过 5 MiB：" + relative)
    if path.suffix.lower() in TEXT_EXTENSIONS or path.name in {"LICENSE", ".gitignore"}:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReleaseError("文本文件不是 UTF-8：" + relative) from exc
        if PRIVATE_PATH.search(text):
            raise ReleaseError("检测到个人绝对路径，请使用可移植路径：" + relative)
        if LIKELY_KEY.search(text):
            raise ReleaseError("检测到疑似真实 API Key，拒绝发布：" + relative)


def zip_entry(archive: zipfile.ZipFile, archive_name: str, data: bytes, executable: bool = False) -> None:
    info = zipfile.ZipInfo(archive_name, date_time=(2026, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    mode = stat.S_IFREG | (0o755 if executable else 0o644)
    info.external_attr = mode << 16
    archive.writestr(info, data)


def build_release(root: Path = ROOT, output_dir: Path | None = None) -> dict:
    root = root.resolve()
    output_dir = (output_dir or root / "dist").resolve()
    files = select_files(root)
    contents = []
    for path in files:
        data = path.read_bytes()
        validate_contents(path, data, root)
        contents.append((path.relative_to(root).as_posix(), data, path.suffix in {".command", ".sh"} or bool(path.stat().st_mode & stat.S_IXUSR)))
    total = sum(len(data) for _, data, _ in contents)
    if total > 32 * 1024 * 1024:
        raise ReleaseError("源文件总量超过 32 MiB，请检查打包范围。")
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / (PACKAGE_NAME + "-v" + VERSION + "-mac.zip")
    prefix = PACKAGE_NAME + "-v" + VERSION
    manifest = {
        "name": PACKAGE_NAME,
        "version": VERSION,
        "platform": "macOS",
        "files": [{"path": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)} for name, data, _ in contents],
    }
    temporary = archive_path.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, data, executable in contents:
                zip_entry(archive, prefix + "/" + name, data, executable)
            zip_entry(archive, prefix + "/RELEASE-MANIFEST.json", (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        temporary.replace(archive_path)
    finally:
        temporary.unlink(missing_ok=True)
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    checksum_path = output_dir / "SHA256SUMS"
    checksum_path.write_text(digest + "  " + archive_path.name + "\n", encoding="utf-8")
    return {"zip": str(archive_path), "sha256": digest, "checksum": str(checksum_path), "files": len(contents), "source_bytes": total}


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 Mac 分享 ZIP（不包含凭据、运行记录或 Python 依赖）")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(build_release(output_dir=args.output_dir), ensure_ascii=False, indent=2))
        return 0
    except (ReleaseError, OSError) as exc:
        print("打包失败：" + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

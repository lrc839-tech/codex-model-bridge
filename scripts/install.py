#!/usr/bin/env python3
"""Install the portable skill without changing Codex model or authentication settings."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

VERSION = "0.1.0"
SKILL_NAME = "codex-model-bridge"
MARKER = ".bridge-install.json"
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
SOURCE_SKILL = PACKAGE_ROOT / "skills" / SKILL_NAME


class InstallError(Exception):
    pass


def default_destination() -> Path:
    return Path.home() / ".agents" / "skills" / SKILL_NAME


def find_codex(explicit: str | None = None) -> Path:
    candidates = [explicit, os.environ.get("CODEX_MODEL_BRIDGE_CODEX"), shutil.which("codex")]
    for app_root in (Path("/Applications"), Path.home() / "Applications"):
        candidates.extend([
            str(app_root / "ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"),
            str(app_root / "Codex.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"),
            str(app_root / "Codex.app/Contents/Resources/codex"),
        ])
    for candidate in candidates:
        if not candidate:
            continue
        executable = Path(candidate).expanduser()
        if executable.is_file() and os.access(executable, os.X_OK):
            return executable.resolve()
    raise InstallError("未找到 Codex CLI。请先安装并打开 Codex 桌面端，或将 codex 加入 PATH 后重试。")


def validate_source(source: Path) -> None:
    for required in ("SKILL.md", "scripts/bridge.py", "runtime/config.py", "ui/index.html"):
        if not (source / required).is_file():
            raise InstallError("安装包不完整，缺少 " + required + "。请重新下载完整 ZIP 并解压。")
    for path in source.rglob("*"):
        if path.is_symlink():
            raise InstallError("安装包中包含符号链接，已停止安装。")


def validate_destination(destination: Path) -> Path:
    destination = destination.expanduser().absolute()
    if destination.name != SKILL_NAME:
        raise InstallError("安装目录最后一级必须为 " + SKILL_NAME + "。")
    if destination.is_symlink():
        raise InstallError("目标技能目录是符号链接，请先自行检查并移除该链接。")
    if destination.exists() and not destination.is_dir():
        raise InstallError("目标路径不是文件夹。")
    if destination.resolve() == SOURCE_SKILL.resolve():
        raise InstallError("不能将安装目标设为安装包中的源技能目录。")
    return destination


def install(destination: Path, codex_path: Path, source: Path = SOURCE_SKILL) -> dict:
    validate_source(source)
    destination = validate_destination(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    staging = Path(tempfile.mkdtemp(prefix=".bridge-install-", dir=str(destination.parent)))
    backup = None
    try:
        payload = staging / SKILL_NAME
        shutil.copytree(source, payload, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", MARKER, ".bridge-python", ".DS_Store"))
        metadata = {
            "package": SKILL_NAME,
            "version": VERSION,
            "python": str(Path(sys.executable).resolve()),
            "codex": str(codex_path.resolve()),
            "installed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        (payload / MARKER).write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        (payload / ".bridge-python").write_text(metadata["python"] + "\n", encoding="utf-8")
        if destination.exists():
            backup_root = destination.parent.parent / "skill-backups"
            backup_root.mkdir(parents=True, exist_ok=True)
            backup = backup_root / (SKILL_NAME + "-" + stamp)
            destination.rename(backup)
        try:
            payload.rename(destination)
        except Exception:
            if backup is not None and not destination.exists():
                backup.rename(destination)
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {"destination": str(destination), "backup": str(backup) if backup else None, **metadata}


def installed_metadata(destination: Path) -> dict:
    destination = validate_destination(destination)
    marker = destination / MARKER
    if not marker.is_file() or marker.is_symlink():
        raise InstallError("未找到本安装器安装的技能。请先双击 install.command。")
    try:
        metadata = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InstallError("安装记录损坏，请重新运行 install.command。") from exc
    if metadata.get("package") != SKILL_NAME:
        raise InstallError("安装记录与本工具不匹配，已停止操作。")
    return metadata


def configure(destination: Path, codex_path: Path | None = None) -> int:
    metadata = installed_metadata(destination)
    entry = destination / "scripts" / "bridge.py"
    if not entry.is_file():
        raise InstallError("已安装的技能文件不完整，请重新运行 install.command。")
    executable = Path(metadata.get("python", ""))
    if not executable.is_file() or not os.access(executable, os.X_OK):
        executable = Path(sys.executable).resolve()
    env = os.environ.copy()
    configured_codex = str(codex_path or metadata.get("codex", ""))
    if configured_codex:
        env["CODEX_MODEL_BRIDGE_CODEX"] = configured_codex
    return subprocess.call([str(executable), str(entry), "configure"], env=env)


def uninstall(destination: Path) -> None:
    installed_metadata(destination)
    shutil.rmtree(destination)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Codex Model Bridge macOS 安装与配置")
    parser.add_argument("action", choices=("install", "configure", "uninstall"), nargs="?", default="install")
    parser.add_argument("--destination", type=Path, default=default_destination(), help=argparse.SUPPRESS)
    parser.add_argument("--no-configure", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--codex-path", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if platform.system() != "Darwin":
            raise InstallError("此安装包目前仅支持 macOS；Windows 安装器尚未提供。")
        if sys.version_info < (3, 9):
            raise InstallError("需要 Python 3.9 或更新版本：https://www.python.org/downloads/macos/")
        destination = validate_destination(args.destination)
        if args.action == "install":
            codex_path = find_codex(args.codex_path)
            result = install(destination, codex_path)
            print("已安装技能：" + result["destination"])
            if result["backup"]:
                print("旧版本已备份：" + result["backup"])
            print("Codex 默认模型与登录配置保持原样。重启 Codex 或新建对话后可使用 $codex-model-bridge。")
            if not args.no_configure:
                return configure(destination, codex_path)
        elif args.action == "configure":
            return configure(destination)
        else:
            uninstall(destination)
            print("已卸载 codex-model-bridge 技能。")
            print("本机 API 配置与运行记录保留在 ~/Library/Application Support/CodexModelBridge。")
        return 0
    except (InstallError, OSError) as exc:
        print("操作未完成：" + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/bin/bash
# Finder .command entry points share one dependency check. No dependency is downloaded.
set -u
package_root="$(cd "$(dirname "$0")/.." && pwd -P)"
bridge_action="${1:-install}"
bridge_python=""

try_python() {
  local candidate="$1"
  case "$candidate" in
    /usr/bin/python3|/Library/Developer/CommandLineTools/*|/Applications/Xcode.app/*) return 1 ;;
  esac
  [ -n "$candidate" ] && [ -x "$candidate" ] || return 1
  if "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
    bridge_python="$candidate"
    return 0
  fi
  return 1
}

if [ -n "${CODEX_MODEL_BRIDGE_PYTHON:-}" ]; then
  try_python "$CODEX_MODEL_BRIDGE_PYTHON" || true
fi
if [ -z "$bridge_python" ] && [ -f "$HOME/.agents/skills/codex-model-bridge/.bridge-python" ]; then
  IFS= read -r bridge_saved_python < "$HOME/.agents/skills/codex-model-bridge/.bridge-python" || true
  try_python "${bridge_saved_python:-}" || true
fi
if [ -z "$bridge_python" ]; then
  bridge_path_python="$(command -v python3 2>/dev/null || true)"
  for candidate in "$bridge_path_python" "$HOME/.local/bin/python3" /opt/homebrew/bin/python3 /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /Library/Frameworks/Python.framework/Versions/*/bin/python3 "$HOME"/.local/share/uv/python/*/bin/python3; do
    if try_python "$candidate"; then break; fi
  done
fi
if [ -z "$bridge_python" ]; then
  printf '%s\n' '需要 Python 3.9 或更新版本。' '请从 Python 官网下载安装 macOS 版本，安装完成后重新双击本文件：' 'https://www.python.org/downloads/macos/' '本安装器不会启动 Xcode，也不会自动下载或执行第三方安装脚本。'
  if [ -t 0 ]; then read -r -p '按回车关闭窗口。' ignored; fi
  exit 1
fi
"$bridge_python" "$package_root/scripts/install.py" "$bridge_action"
bridge_status=$?
if [ "$bridge_status" -ne 0 ] && [ -t 0 ]; then read -r -p '按回车关闭窗口。' ignored; fi
exit "$bridge_status"

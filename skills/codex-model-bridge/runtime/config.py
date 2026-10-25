"""Local-only three-field profile; never return the saved key to a browser."""
from __future__ import annotations
import json
import os
import stat
from pathlib import Path
import tempfile
from .providers import ProviderClient, ProviderError, normalize_base_url

STATE = Path(os.environ.get('CODEX_MODEL_BRIDGE_STATE', str(Path.home() / 'Library/Application Support/CodexModelBridge'))).expanduser()
PROFILE = STATE / 'profile.json'


def ensure_state():
    if STATE.is_symlink():
        raise ValueError('配置目录不能是符号链接。')
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    if STATE.stat().st_uid != os.getuid():
        raise ValueError('配置目录必须属于当前用户。')
    STATE.chmod(0o700)


def load_profile(required=True):
    if STATE.is_symlink() or PROFILE.is_symlink():
        raise ValueError('配置目录和配置文件不能是符号链接。')
    if STATE.exists():
        ensure_state()
    if not PROFILE.exists():
        if required:
            raise ValueError('尚未配置模型；运行 configure 打开本机设置页。')
        return {}
    fd = os.open(PROFILE, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'r', encoding='utf-8') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('配置文件须属于当前用户且权限为 600。')
        if info.st_size > 65536:
            raise ValueError('配置文件格式不正确。')
        value = json.loads(handle.read(65537))
    if not isinstance(value, dict):
        raise ValueError('配置文件格式不正确。')
    ProviderClient(value)
    return value


def resolve_profile(value):
    if not isinstance(value, dict):
        raise ValueError('请提交 API 链接、API Key 和模型名称。')
    url = normalize_base_url(value.get('base_url', ''))
    saved = load_profile(required=False)
    key = value.get('api_key', '')
    if not isinstance(key, str):
        raise ValueError('API Key 格式不正确。')
    key = key.strip()
    if not key:
        if url != saved.get('base_url') or not saved.get('api_key'):
            raise ValueError('更换 API 地址后必须重新填写对应的 API Key。')
        key = saved['api_key']
    result = {'base_url': url, 'api_key': key, 'model': value.get('model', '').strip() if isinstance(value.get('model'), str) else ''}
    ProviderClient(result)
    return result


def save_profile(value):
    result = resolve_profile(value)
    ensure_state()
    fd, name = tempfile.mkstemp(prefix='.profile-', dir=STATE)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(result, handle, ensure_ascii=False)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, PROFILE)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return public_profile(result)


def public_profile(value=None):
    value = load_profile(required=False) if value is None else value
    return {'base_url': value.get('base_url', ''), 'model': value.get('model', ''),
            'key_configured': bool(value.get('api_key')), 'default': 'pure_codex'}

"""Install separate state and interactive capabilities into the fixed copy."""
import json
import os
from pathlib import Path
import stat
import tempfile


def checked_config(folder):
    folder = Path(folder).absolute()
    target = folder / 'ai-pair.local.js'
    for path in (target, folder, *folder.parents):
        info = path.lstat()
        if getattr(info, 'st_file_attributes', 0) & 0x400 or stat.S_ISLNK(info.st_mode):
            raise OSError('reparse_target')
    if not stat.S_ISREG(target.stat().st_mode):
        raise OSError('not_regular_file')
    # Never traverse or modify the wallpaper media junctions.
    if not (folder / 'we-bridge.js').is_file():
        raise OSError('bridge_missing')
    return target


def atomic_write(target, payload):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.ai-pair-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def install_pair(folder, backup_folder, token=None, chat_token=None):
    target = checked_config(folder)
    original = target.read_bytes()
    backup_folder = Path(backup_folder)
    backup_folder.mkdir(parents=True, exist_ok=True)
    # Private local backup, never served by the web application.
    backup = backup_folder / 'ai-pair.previous.js'
    atomic_write(backup, original)
    config = {'enabled': bool(token)}
    if token:
        config['token'] = token
    if chat_token:
        config['chatToken'] = chat_token
    payload = ('window.PURPLE_AI_CONFIG = Object.freeze(' + json.dumps(config) + ');\n').encode()
    atomic_write(target, payload)
    return target, original

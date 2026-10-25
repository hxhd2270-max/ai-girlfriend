"""Portable layout, integrity checks and explicit local configuration."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sys

from tools.start_plan import StartupError


def plain_path(root, relative):
    relative = PurePosixPath(relative)
    if relative.is_absolute() or not relative.parts or any(p in ('.', '..') or ':' in p or chr(92) in p for p in relative.parts):
        raise StartupError('Invalid portable manifest path.')
    path = root.joinpath(*relative.parts)
    if path.resolve() != path.absolute():
        raise StartupError('Portable files cannot be symlinks or junctions.')
    return path


def verify(root, prefix=None):
    root = Path(root).absolute()
    manifest = json.loads((root / 'portable.json').read_text(encoding='utf-8'))
    if manifest.get('format') != 1 or not manifest.get('files'):
        raise StartupError('Portable manifest is missing or invalid.')
    checked = 0
    if prefix is None:
        print('Checking packaged files: ' + str(len(manifest['files'])) + ' ...', flush=True)
    for relative, expected in manifest['files'].items():
        if prefix is not None and not relative.startswith(prefix):
            continue
        path = plain_path(root, relative)
        if not path.is_file() or path.stat().st_size != expected['bytes']:
            raise StartupError('Portable file missing or changed: ' + relative)
        with path.open('rb') as source:
            actual = hashlib.file_digest(source, 'sha256').hexdigest()
        if actual != expected['sha256']:
            raise StartupError('Portable file hash mismatch: ' + relative)
        checked += 1
        if prefix is None and checked % 3000 == 0:
            print('Checked ' + str(checked) + '/' + str(len(manifest['files'])), flush=True)
    if checked == 0:
        raise StartupError('Portable manifest has no matching files.')
    return checked


def configure(root):
    root = Path(root).absolute()
    for directory in ('data', 'config', 'data/neko', 'desktop-host/runtime-profile', 'desktop-host/evidence'):
        path = plain_path(root, directory)
        path.mkdir(parents=True, exist_ok=True)
    os.environ.update({
        'COMPANION_DATA_DIR': str(root / 'data'),
        'COMPANION_USAGE_LIMITS_PATH': str(root / 'config/usage-limits.json'),
        'COMPANION_KEY_FILE': str(root / 'config/provider-key.txt'),
        'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
    })
    # Configuration paths belong to this copy, never an inherited developer tree.
    for name in ('PYTHONPATH', 'PYTHONHOME'):
        os.environ.pop(name, None)


def system_dependencies(root):
    if sys.platform != 'win32' or sys.maxsize <= 2**32:
        raise StartupError('This package requires 64-bit Windows.')
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                chr(92).join(('SOFTWARE', 'Microsoft', 'NET Framework Setup', 'NDP', 'v4', 'Full')),
                0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            if winreg.QueryValueEx(key, 'Release')[0] < 394802:
                raise OSError()
    except OSError:
        raise StartupError('Install .NET Framework 4.6.2 or later; .NET Framework 4.8 is recommended. See PORTABLE-README.md.') from None
    try:
        loader = ctypes.WinDLL(str(root / 'desktop-host/bin/WebView2Loader.dll'))
        get_version = loader.GetAvailableCoreWebView2BrowserVersionString
        get_version.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)]
        get_version.restype = ctypes.c_long
        value = ctypes.c_void_p()
        code = get_version(None, ctypes.byref(value))
        if code != 0 or not value.value:
            raise OSError()
        try:
            version = ctypes.wstring_at(value)
        finally:
            ctypes.windll.ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
            ctypes.windll.ole32.CoTaskMemFree(value)
    except OSError:
        raise StartupError('Install Microsoft Edge WebView2 Runtime (x64). See PORTABLE-README.md.') from None
    return version


def preflight(root, cloud=False):
    root = Path(root).absolute()
    configure(root)
    count = verify(root)
    version = system_dependencies(root)
    if cloud:
        from server import usage
        from tools.start_plan import read_credential
        try:
            from server.provider_routes import RuntimeRoutes
            if RuntimeRoutes.load(root) is None:
                encrypted = plain_path(root, 'config/provider-key.dpapi')
                read_credential(encrypted if encrypted.exists() else plain_path(root, 'config/provider-key.txt'))
        except (ValueError, OSError):
            raise StartupError('Cannot read API connections. Open offline mode and 04 API settings to check the configuration.') from None
        except StartupError:
            raise StartupError('Use 配置知夏 in the Windows Start menu to save your account. See PORTABLE-README.md.') from None
        limits = usage._limits()
        if any(value == 0 for value in limits.values()):
            raise StartupError('Cloud requests are disabled. Use 配置知夏 in the Windows Start menu to choose daily limits. See PORTABLE-README.md.')
    return {'verifiedFiles': count, 'webview2': version}


if __name__ == '__main__':
    try:
        print(json.dumps(preflight(Path(__file__).resolve().parents[1]), ensure_ascii=False))
    except (StartupError, OSError, ValueError):
        print('Portable check failed. Run start-standalone-simulation.cmd for a startup message; see PORTABLE-README.md.')
        raise SystemExit(1)

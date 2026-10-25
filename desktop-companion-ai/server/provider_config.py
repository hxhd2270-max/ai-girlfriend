"""Local account settings for the fixed Token Plan adapter; never return secrets."""
import ctypes
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets

from fastapi import HTTPException

PREFIX = b'COMPANION-DPAPI-V1\n'
KEY_NAME = 'provider-key.dpapi'
SERVICES = [
    {'name': '文字与记忆', 'model': 'qwen3.8-flash',
     'endpoint': 'https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions'},
    {'name': '语音合成', 'model': 'qwen-audio-3.0-tts-plus',
     'endpoint': 'https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer'},
    {'name': '语音识别', 'model': 'qwen-audio-3.0-asr-flash',
     'endpoint': 'https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation'},
]


def validate_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'sk-[A-Za-z0-9_.-]{8,512}', value):
        raise ValueError('invalid credential')
    return value


def crypt(data, *, decrypt=False):
    if os.name != 'nt':
        raise OSError('Windows account encryption required')

    class Blob(ctypes.Structure):
        _fields_ = [('size', ctypes.c_ulong), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    api = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        call = api.CryptUnprotectData
        call.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Blob)]
        args = (ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        call = api.CryptProtectData
        call.argtypes = [ctypes.POINTER(Blob), ctypes.c_wchar_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Blob)]
        args = (ctypes.byref(source), 'Companion provider account', None, None, None, 1, ctypes.byref(output))
    call.restype = ctypes.c_int
    try:
        if not call(*args):
            raise OSError('Account encryption failed')
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.memset(buffer, 0, len(buffer))
        if output.data:
            ctypes.memset(output.data, 0, output.size)
            kernel.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))


def read_encrypted(path):
    path = Path(path)
    if path.stat().st_size > 8192:
        raise ValueError('invalid credential store')
    raw = path.read_bytes()
    if not raw.startswith(PREFIX):
        raise ValueError('invalid credential store')
    return validate_key(crypt(raw[len(PREFIX):], decrypt=True).decode('ascii'))


class ProviderSettings:
    def __init__(self, root, cloud):
        self.root = Path(root).absolute()
        self.path = self.root / 'config' / KEY_NAME
        self.cloud = cloud

    def check_path(self):
        if self.path.resolve() != self.path.absolute():
            raise OSError('linked config path')

    def revision(self):
        self.check_path()
        if not self.path.exists():
            return 'empty'
        if self.path.stat().st_size > 8192:
            raise ValueError('invalid store')
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def public(self):
        try:
            revision = self.revision()
        except (OSError, ValueError, UnicodeError):
            raise HTTPException(503, '本机加密配置无法读取，请检查 Windows 用户和配置文件。') from None
        unreadable = False
        try:
            saved = read_encrypted(self.path) if self.path.exists() else None
        except (OSError, ValueError, UnicodeError):
            # Allow replacing an unreadable store from a running/simulation UI.
            # The launcher still fails closed and never falls back to an old key.
            saved, unreadable = None, True
        current = os.environ.get('DASHSCOPE_API_KEY', '')
        configured = bool(current) and self.cloud is not None
        override = os.environ.get('COMPANION_KEY_FILE')
        uses_managed = not override or Path(override).absolute() in (
            self.path.absolute(), self.root / 'config/provider-key.txt')
        return dict(revision=revision, provider='百炼 Token Plan',
                    currentConfigured=configured, saved=bool(saved),
                    restartRequired=bool(saved and (not configured or not hmac.compare_digest(saved, current))),
                    externalOverride=not uses_managed, savedUnreadable=unreadable, canSave=os.name == 'nt',
                    services=SERVICES, validation='not_tested')

    def save(self, body):
        if not isinstance(body, dict) or set(body) != {'revision', 'key'}:
            raise HTTPException(422, '请填写 API Key 并重新保存。')
        try:
            candidate = validate_key(body['key'])
        except ValueError:
            raise HTTPException(422, 'API Key 格式不正确，应为 sk- 开头且不含空格。') from None
        try:
            if body['revision'] != self.revision():
                raise HTTPException(409, 'API 配置已在其他页面修改，请重新读取后保存。')
            encrypted = PREFIX + crypt(candidate.encode('ascii'))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.check_path()
            temporary = self.path.with_name('.provider-' + secrets.token_hex(8) + '.tmp')
            try:
                with temporary.open('xb') as output:
                    output.write(encrypted)
                    output.flush()
                    os.fsync(output.fileno())
                if not hmac.compare_digest(read_encrypted(temporary), candidate):
                    raise ValueError('credential verification failed')
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
        except (OSError, ValueError):
            raise HTTPException(503, 'API 配置保存失败，原有配置保持不变。') from None
        return self.public()

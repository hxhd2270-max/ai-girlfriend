"""Versioned connections and task assignments; one encrypted, atomic Windows store.

Public views never include keys. Runtime consumers retain an immutable startup
snapshot: saving settings cannot redirect an in-flight conversation.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException
from server import provider_config as vault

NAME = 'provider-routes.dpapi'
PREFIX = b'COMPANION-ROUTES-V1\n'
TASKS = {'conversation': '人物对话', 'summary': '记忆摘要',
         'correction': '记忆修正', 'emotion': '情绪判断',
         'tts': '语音合成', 'asr': '语音识别'}
PROTOCOLS = {'chat-completions': ('conversation', 'summary', 'correction', 'emotion'),
             'anthropic-messages': ('conversation', 'summary', 'correction', 'emotion'),
             'responses': ('conversation', 'summary', 'correction', 'emotion'),
             'gemini-content': ('conversation', 'summary', 'correction', 'emotion'),
             'openai-audio': ('tts', 'asr'), 'token-plan': tuple(TASKS)}
AUTH_OPTIONS = {
    'chat-completions': ('bearer', 'none'), 'responses': ('bearer', 'none'),
    'anthropic-messages': ('x-api-key', 'none'),
    'gemini-content': ('x-goog-api-key', 'none'),
    'openai-audio': ('bearer', 'none'), 'token-plan': ('bearer', 'none'),
}
MAX_STORE = 262144


def defaults():
    return {'version': 1, 'profiles': {'qwen': {
        'label': '现有百炼 Token Plan', 'protocol': 'token-plan',
        'baseUrl': 'https://token-plan.cn-beijing.maas.aliyuncs.com',
        'auth': 'bearer', 'key': '', 'tokenField': 'max_tokens',
        'thinking': 'off', 'streamUsage': True}},
        'assignments': {task: {'profile': 'qwen', 'model': (
            'qwen-audio-3.0-tts-plus' if task == 'tts' else
            'qwen-audio-3.0-asr-flash' if task == 'asr' else 'qwen3.8-flash'),
            'voice': 'longanhuan_v3.6' if task == 'tts' else ''} for task in TASKS}}


def base_url(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 1000 or any(
            c.isspace() or ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError('invalid URL')
    parts = urlsplit(value)
    if (not parts.hostname or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or parts.port == 0
            or parts.scheme not in ('http', 'https')
            or (parts.scheme == 'http' and parts.hostname not in ('127.0.0.1', 'localhost', '::1'))
            or any(p in ('.', '..') for p in parts.path.split('/'))
            or '%' in value or '\\' in value):
        raise ValueError('invalid URL')
    return value.rstrip('/')


def credential(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or any(
            ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError('invalid credential')
    return value


def validate(value, previous=None):
    if not isinstance(value, dict) or set(value) != {'version', 'profiles', 'assignments'} or value['version'] != 1:
        raise ValueError('invalid configuration')
    profiles, assignments = value['profiles'], value['assignments']
    if not isinstance(profiles, dict) or not 1 <= len(profiles) <= 16 or not isinstance(assignments, dict) or set(assignments) != set(TASKS):
        raise ValueError('invalid tasks')
    result = copy.deepcopy(value)
    for identity, profile in profiles.items():
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,48}', identity) or not isinstance(profile, dict):
            raise ValueError('invalid profile')
        if set(profile) != {'label', 'protocol', 'baseUrl', 'auth', 'key', 'tokenField', 'thinking', 'streamUsage'}:
            raise ValueError('invalid profile fields')
        if not isinstance(profile['label'], str) or not 1 <= len(profile['label'].strip()) <= 80:
            raise ValueError('invalid label')
        if (not isinstance(profile['protocol'], str) or profile['protocol'] not in PROTOCOLS
                or profile['auth'] not in AUTH_OPTIONS[profile['protocol']]):
            raise ValueError('unsupported protocol')
        endpoint = base_url(profile['baseUrl'])
        if (profile['tokenField'] not in ('max_tokens', 'max_completion_tokens')
                or profile['thinking'] not in ('omit', 'off') or type(profile['streamUsage']) is not bool):
            raise ValueError('invalid options')
        key = profile['key']
        if profile['auth'] == 'none':
            key = ''
        elif not key and previous:
            old = previous['profiles'].get(identity, {})
            if old.get('baseUrl') == endpoint and old.get('auth') == profile['auth'] and old.get('protocol') == profile['protocol']:
                key = old.get('key', '')
        if profile['auth'] != 'none':
            key = credential(key)
        result['profiles'][identity].update(baseUrl=endpoint, key=key)
    for task, assignment in assignments.items():
        if not isinstance(assignment, dict) or set(assignment) != {'profile', 'model', 'voice'}:
            raise ValueError('invalid assignment')
        if not isinstance(assignment['profile'], str):
            raise ValueError('invalid task connection')
        profile = profiles.get(assignment['profile'])
        if not profile or task not in PROTOCOLS[profile['protocol']]:
            raise ValueError('incompatible task')
        for field, limit in (('model', 200), ('voice', 100)):
            item = assignment[field]
            if not isinstance(item, str) or len(item) > limit or any(ord(c) < 33 or ord(c) > 126 for c in item):
                raise ValueError('invalid model or voice')
        if not assignment['model'] or (task == 'tts' and not assignment['voice']):
            raise ValueError('model and TTS voice required')
    return result


class RouteStore:
    def __init__(self, root):
        self.path = Path(root).absolute() / 'config' / NAME

    def check(self):
        if self.path.resolve() != self.path.absolute():
            raise ValueError('linked configuration')

    def revision(self):
        self.check()
        if not self.path.exists():
            return 'legacy'
        if self.path.stat().st_size > MAX_STORE:
            raise ValueError('oversized configuration')
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def load(self):
        self.revision()
        raw = self.path.read_bytes()
        if not raw.startswith(PREFIX):
            raise ValueError('invalid encrypted store')
        return validate(json.loads(vault.crypt(raw[len(PREFIX):], decrypt=True)))

    def save(self, revision, config, legacy_key=None):
        try:
            if revision != self.revision():
                raise HTTPException(409, '配置已被其他页面修改，请重新读取后保存。')
            old = self.load() if self.path.exists() else defaults()
            if not self.path.exists() and legacy_key:
                old['profiles']['qwen']['key'] = legacy_key
            try:
                value = validate(config, old)
            except (ValueError, TypeError, KeyError):
                raise HTTPException(422, '请检查协议、地址、模型、音色和密钥；更换连接地址或协议时需要重新填写密钥。') from None
            encrypted = PREFIX + vault.crypt(json.dumps(value, ensure_ascii=False).encode('utf-8'))
            if len(encrypted) > MAX_STORE:
                raise HTTPException(422, '配置过大，请减少连接数量。')
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.check()
            temporary = self.path.with_name('.routes-' + secrets.token_hex(8) + '.tmp')
            try:
                with temporary.open('xb') as output:
                    output.write(encrypted)
                    output.flush()
                    os.fsync(output.fileno())
                if json.loads(vault.crypt(temporary.read_bytes()[len(PREFIX):], decrypt=True)) != value:
                    raise ValueError('encryption verification failed')
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
        except (ValueError, OSError, UnicodeError):
            raise HTTPException(503, '无法读取或加密 API 配置，请检查 Windows 用户和目录权限。') from None

    def public(self, running_revision=None, legacy_configured=False):
        try:
            revision = self.revision()
            value = self.load() if self.path.exists() else defaults()
        except (ValueError, TypeError, KeyError, OSError, UnicodeError):
            raise HTTPException(503, '本机 API 配置无法解密或格式损坏，请使用原 Windows 用户恢复配置。') from None
        for profile in value['profiles'].values():
            profile['hasKey'] = bool(profile.pop('key')) or (revision == 'legacy' and legacy_configured)
        return dict(revision=revision, config=value, tasks=TASKS, protocols={k: list(v) for k, v in PROTOCOLS.items()},
                    canSave=os.name == 'nt', restartRequired=(revision != 'legacy' and revision != running_revision),
                    runningRevision=running_revision, validation='not_tested')


class RuntimeRoutes:
    def __init__(self, config, revision):
        self._config = validate(config)
        self.revision = revision

    @classmethod
    def load(cls, root):
        store = RouteStore(root)
        if not store.path.exists():
            return None
        revision = store.revision()
        value = store.load()
        if store.revision() != revision:
            raise ValueError('configuration changed during startup')
        expected = os.environ.get('COMPANION_ROUTES_REVISION')
        if expected and expected != revision:
            raise ValueError('configuration changed during startup; restart all services')
        return cls(value, revision)

    def route(self, task):
        assignment = self._config['assignments'][task]
        profile = self._config['profiles'][assignment['profile']]
        return dict(profile, **{k: assignment[k] for k in ('model', 'voice')})

    def headers(self, task):
        route = self.route(task)
        if route['auth'] == 'bearer':
            return {'Authorization': 'Bearer ' + route['key']}
        if route['auth'] == 'x-api-key':
            return {'x-api-key': route['key'], 'anthropic-version': '2023-06-01'}
        if route['auth'] == 'x-goog-api-key':
            return {'x-goog-api-key': route['key']}
        return {}

    def endpoint(self, task):
        route = self.route(task)
        if route['protocol'] == 'token-plan':
            suffix = ('/api/v1/services/audio/tts/SpeechSynthesizer' if task == 'tts' else
                      '/api/v1/services/aigc/multimodal-generation/generation' if task == 'asr' else
                      '/compatible-mode/v1/chat/completions')
        elif route['protocol'] in ('chat-completions', 'openai-audio'):
            suffix = '/audio/speech' if task == 'tts' else '/audio/transcriptions' if task == 'asr' else '/chat/completions'
        elif route['protocol'] == 'anthropic-messages':
            suffix = '/messages'
        elif route['protocol'] == 'responses':
            suffix = '/responses'
        elif route['protocol'] == 'gemini-content':
            from urllib.parse import quote
            model = route['model'][7:] if route['model'].startswith('models/') else route['model']
            suffix = '/models/' + quote(model, safe='') + ':streamGenerateContent?alt=sse'
        else:
            raise ValueError('unsupported protocol')
        return route['baseUrl'] + suffix

    def chat_payload(self, task, payload):
        route = self.route(task)
        result = copy.deepcopy(payload)
        limit = result.pop('max_tokens', result.pop('max_completion_tokens', 3000))
        protocol = route['protocol']
        if protocol in ('chat-completions', 'token-plan'):
            result.update(model=route['model'])
            result[route['tokenField']] = limit
            result.pop('enable_thinking', None)
            if route['thinking'] == 'off':
                result['enable_thinking'] = False
            if not route['streamUsage']:
                result.pop('stream_options', None)
            return result
        messages = result.pop('messages', [])
        system = [m['content'] for m in messages if m.get('role') == 'system']
        conversation = [m for m in messages if m.get('role') != 'system']
        if protocol == 'anthropic-messages':
            result = dict(model=route['model'], messages=conversation,
                          max_tokens=limit, stream=True)
            if system:
                result['system'] = '\n\n'.join(system)
            return result
        if protocol == 'responses':
            result = dict(model=route['model'], input=conversation, stream=True,
                          max_output_tokens=limit)
            if system:
                result['instructions'] = '\n\n'.join(system)
            return result
        if protocol == 'gemini-content':
            contents = []
            for message in conversation:
                contents.append({'role': 'model' if message['role'] == 'assistant' else 'user',
                                 'parts': [{'text': message['content']}]})
            result = dict(contents=contents,
                          generationConfig={'maxOutputTokens': limit})
            if system:
                result['systemInstruction'] = {'parts': [{'text': '\n\n'.join(system)}]}
            return result
        raise ValueError('unsupported protocol')

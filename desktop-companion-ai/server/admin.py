"""Local settings bridge, including write-only encrypted account configuration."""
import asyncio
import copy
import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

FIELDS = {'昵称': 40, '性别': 20, '年龄': 20, '种族': 40, '自称': 20,
          '核心特质': 2000, '背景故事': 4000, '价值观与原则': 2400,
          '喜好与习惯': 1800, '自我认知': 2400, '边界与禁区': 1800,
          '互动方式': 1800, '补充设定': 4000, '关系定位': 1000, '表达习惯': 1500}
RESERVED = set('''_reserved live2d voice_id system_prompt model_type live3d_sub_type vrm
vrm_animation lighting vrm_rotation live2d_item_id live2d_idle_animation item_id
idleAnimation idleAnimations mmd mmd_animation mmd_idle_animation mmd_idle_animations
touch_set _field_order 档案名 原始数据 文件路径 创意工坊物品ID description tags name 描述 标签 关键词'''.split())
DEFAULT_VOICE = {'id': 'longanhuan_v3.6', 'label': '当前女声', 'gain': 1.0}


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)


def validate(value):
    if not isinstance(value, dict) or set(value) != {'persona', 'voice'}:
        raise HTTPException(422, '设置格式不正确')
    persona, voice = value['persona'], value['voice']
    if not isinstance(persona, dict) or set(persona) != set(FIELDS):
        raise HTTPException(422, '人物字段不完整，请刷新页面')
    for key, limit in FIELDS.items():
        if not isinstance(persona[key], str) or len(persona[key]) > limit:
            raise HTTPException(422, f'{key}内容过长或格式不正确')
    if not persona['昵称'].strip() or not persona['核心特质'].strip():
        raise HTTPException(422, '请填写昵称和核心性格')
    if not isinstance(voice, dict) or set(voice) != {'id', 'label', 'gain'}:
        raise HTTPException(422, '声音设置格式不正确')
    if not isinstance(voice['id'], str) or not re.fullmatch(r'[!-~]{1,100}', voice['id']):
        raise HTTPException(422, '音色 ID 格式不正确')
    if not isinstance(voice['label'], str) or not 1 <= len(voice['label'].strip()) <= 40:
        raise HTTPException(422, '请填写音色名称（最多40字）')
    if type(voice['gain']) not in (float, int) or not 0 <= voice['gain'] <= 1:
        raise HTTPException(422, '音量须在0至100%之间')
    return copy.deepcopy(value)


class Settings:
    def __init__(self, root, data, cloud, runtimes, voice):
        self.root, self.path = root, data / 'companion-settings.json'
        self.cloud, self.runtimes, self.voice = cloud, runtimes, voice
        self.lock = asyncio.Lock()
        self.applying = False
        self.previewing = False
        self.task = None
        self.state = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else None
        if self.state:
            changed = False
            for bucket in ('active', 'previous', 'pending', 'before'):
                item = self.state.get(bucket)
                if isinstance(item, dict) and isinstance(item.get('persona'), dict):
                    normalized = self.persona(item['persona'])
                    if normalized != item['persona']:
                        item['persona'] = normalized
                        changed = True
            if changed:
                self.persist()
        if self.state and self.state.get('pending'):
            # An interrupted HTTP update may have reached NEKO. Do not retry it silently.
            self.state['status'] = 'error'
            self.state['error'] = '上次应用被服务重启打断，请检查当前设定后重新保存。'
            self.state['pending'] = None
            self.persist()

    def persist(self):
        atomic(self.path, self.state)

    async def native(self, method='GET', payload=None):
        path = '/api/characters?language=zh-CN' if method == 'GET' else '/api/characters/catgirl/' + quote('知夏')
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=35, follow_redirects=False) as client:
                response = await client.request(method, 'http://127.0.0.1:49011' + path, json=payload,
                                                headers={'Origin': 'http://127.0.0.1:49011'})
                response.raise_for_status()
                result = response.json()
            if method == 'GET':
                return result['猫娘']['知夏']
            if result.get('success') is False or result.get('partial_success') or not result.get('success'):
                raise ValueError('partial update')
            return result
        except Exception:
            raise HTTPException(502, 'N.E.K.O. 设置读取或应用未完成，请检查服务；未确认成功。') from None

    @staticmethod
    def persona(raw):
        return {k: str(raw.get(k, '')) for k in FIELDS}

    async def ensure(self):
        if self.state is None:
            raw = await self.native()
            self.state = {'revision': uuid.uuid4().hex, 'active': {'persona': self.persona(raw),
                'voice': DEFAULT_VOICE.copy()}, 'previous': None, 'pending': None,
                'status': 'applied', 'error': None, 'updated': None}
            self.persist()

    def voice_settings(self):
        provider = getattr(self.cloud, 'voice', self.cloud)
        binding = getattr(provider, 'voice_binding', None)
        if binding and (not self.state or self.state.get('voiceBinding') != binding):
            return copy.deepcopy(provider.voice_default)
        return copy.deepcopy(self.state['active']['voice'] if self.state else DEFAULT_VOICE)

    def voice_available(self):
        provider = getattr(self.cloud, 'voice', None)
        return bool(getattr(provider, 'supports_voice', False) or getattr(provider, 'plan', False))

    def busy(self):
        return self.previewing or self.voice.task is not None or any(
            rt.active is not None or (rt.task is not None and not rt.task.done())
            for rt in self.runtimes.values())

    async def queue(self, payload, revision, restore=False):
        async with self.lock:
            await self.ensure()
            if revision != self.state['revision']:
                raise HTTPException(409, '设置已在其他页面改变，请刷新后再编辑')
            if self.state['pending'] or self.applying:
                raise HTTPException(409, '已有设置等待应用，请稍候')
            if restore:
                payload = copy.deepcopy(self.state['previous'])
                if payload is None:
                    raise HTTPException(409, '还没有可恢复的上一版')
                binding = getattr(getattr(self.cloud, 'voice', None), 'voice_binding', None)
                if self.state.get('previousVoiceBinding') != binding:
                    payload['voice'] = self.voice_settings()
            candidate = validate(payload)
            if not self.voice_available():
                raise HTTPException(409, '请先启动 N.E.K.O. 融合服务，当前模式不支持应用')
            raw = await self.native()
            # Preserve a true before-image, including changes made in the native manager.
            before = {'persona': self.persona(raw), 'voice': self.voice_settings()}
            self.state.update(pending=candidate, before=before, base=before['persona'],
                              revision=uuid.uuid4().hex, status='pending', error=None)
            self.persist()
            self.task = asyncio.create_task(self.apply_when_idle())
            return self.public()

    def public(self):
        result = {k: copy.deepcopy(self.state.get(k)) for k in
                  ('revision', 'active', 'previous', 'pending', 'status', 'error', 'updated')}
        result['busy'] = self.busy()
        result['canApply'] = self.voice_available()
        result['active']['voice'] = self.voice_settings()
        provider = getattr(self.cloud, 'voice', None)
        result['voiceProtocol'] = provider.routes.route('tts')['protocol'] if getattr(provider, 'routes', None) else 'token-plan'
        return result

    async def apply_when_idle(self):
        try:
            while self.busy():
                await asyncio.sleep(.25)
            # No await between busy check and the admission flag. New turns are rejected.
            self.applying = True
            self.state['status'] = 'applying'
            self.persist()
            candidate = self.state['pending']
            raw = await self.native()
            if self.persona(raw) != self.state['base']:
                raise HTTPException(409, '人物已被其他页面修改，请刷新并重新保存')
            if candidate['persona'] != self.persona(raw):
                await self.cloud.close()
                if self.cloud.unsafe:
                    raise HTTPException(502, '对话会话未安全结束，请重启融合服务后重试')
                merged = {k: v for k, v in raw.items() if k not in RESERVED and not k.startswith('_')}
                merged.update(candidate['persona'])
                await self.native('PUT', merged)
                verified = await self.native()
                if self.persona(verified) != candidate['persona']:
                    raise HTTPException(502, '设定回读不一致，未确认应用成功')
            self.state.update(active=candidate, previous=self.state['before'], pending=None,
                              status='applied', error=None, updated=time.time())
            self.state['previousVoiceBinding'] = getattr(getattr(self.cloud, 'voice', None), 'voice_binding', None)
            self.state['voiceBinding'] = getattr(getattr(self.cloud, 'voice', None), 'voice_binding', None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.state.update(status='error', pending=None,
                error=exc.detail if isinstance(exc, HTTPException) else '设置应用失败，请检查服务后重试')
        finally:
            self.applying = False
            self.persist()


def create_router(settings, valid_session, cookie):
    router = APIRouter()
    from server.provider_config import ProviderSettings
    provider = ProviderSettings(settings.root, settings.cloud)
    provider_lock = asyncio.Lock()
    from server.provider_routes import RouteStore
    routes = RouteStore(settings.root)

    def routes_public():
        voice = getattr(settings.cloud, 'voice', settings.cloud)
        runtime = getattr(voice, 'routes', None)
        return routes.public(getattr(runtime, 'revision', None), bool(os.environ.get('DASHSCOPE_API_KEY')))

    def auth(request):
        if not valid_session(request.cookies.get(cookie)):
            raise HTTPException(401, '请刷新页面建立本机连接')

    @router.get('/admin')
    async def page():
        return FileResponse(settings.root / 'web/admin.html', headers={'Cache-Control': 'no-store'})

    @router.get('/api/admin/settings')
    async def get_settings(request: Request):
        auth(request)
        async with settings.lock:
            await settings.ensure()
            result = settings.public()
        # Network readback must not hold the save/apply lock. The active state
        # already contains the last verified application.
        try:
            raw = await asyncio.wait_for(settings.native(), timeout=3)
            result['livePersona'] = settings.persona(raw)
        except (HTTPException, asyncio.TimeoutError):
            result['livePersona'] = None
        return result

    @router.get('/api/admin/provider')
    async def get_provider(request: Request):
        auth(request)
        async with provider_lock:
            return provider.public()

    @router.post('/api/admin/provider')
    async def save_provider(request: Request):
        auth(request)
        if request.headers.get('origin') != 'http://127.0.0.1:8765':
            raise HTTPException(403, '来源不允许')
        if len(await request.body()) > 4096:
            raise HTTPException(413, 'API 配置内容过大')
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(422, 'API 配置格式不正确') from None
        async with provider_lock:
            return provider.save(body)

    @router.get('/api/admin/providers')
    async def get_routes(request: Request):
        auth(request)
        async with provider_lock:
            return routes_public()

    @router.post('/api/admin/providers')
    async def save_routes(request: Request):
        auth(request)
        if request.headers.get('origin') != 'http://127.0.0.1:8765':
            raise HTTPException(403, '来源不允许')
        if len(await request.body()) > 65536:
            raise HTTPException(413, 'API 配置内容过大')
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {'revision', 'config'}:
                raise ValueError()
        except ValueError:
            raise HTTPException(422, 'API 配置格式不正确') from None
        async with provider_lock:
            legacy = os.environ.get('DASHSCOPE_API_KEY')
            # Migration may be performed from offline settings on a fresh machine.
            if not routes.path.exists() and not legacy:
                try:
                    from server.provider_config import read_encrypted
                    legacy = read_encrypted(settings.root / 'config/provider-key.dpapi')
                except (OSError, ValueError):
                    pass
            routes.save(body['revision'], body['config'], legacy_key=legacy)
            return routes_public()

    @router.post('/api/admin/settings')
    async def save_settings(request: Request):
        auth(request)
        if len(await request.body()) > 50000:
            raise HTTPException(413, '设置内容过大')
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError()
        except ValueError:
            raise HTTPException(422, '请求格式不正确')
        return await settings.queue(body.get('settings'), body.get('revision'), body.get('restore') is True)

    @router.get('/api/admin/status')
    async def status(request: Request):
        auth(request)
        async def probe(port):
            try:
                async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                    response = await client.get(f'http://127.0.0.1:{port}/health')
                    return response.is_success and response.json().get('status') == 'ok'
            except Exception:
                return False
        main, memory, gateway = await asyncio.gather(*(probe(p) for p in (49011, 49012, 49019)))
        return {'dialogue': main, 'memory': memory, 'gateway': gateway,
                'voice': settings.voice_available(),
                'busy': settings.busy(), 'applying': settings.applying}

    @router.post('/api/admin/preview')
    async def preview(request: Request):
        auth(request)
        if len(await request.body()) > 4000:
            raise HTTPException(413, '试听文字过长')
        try:
            body = await request.json()
            voice = body['voice']
            text = body['text']
        except (ValueError, TypeError, KeyError):
            raise HTTPException(422, '试听格式不正确')
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 100:
            raise HTTPException(422, '试听文字为1至100字')
        async with settings.lock:
            await settings.ensure()
            validate({'persona': settings.state['active']['persona'], 'voice': voice})
            if settings.busy() or settings.applying or settings.state['pending']:
                raise HTTPException(409, '请先结束当前对话或等待设置应用，再试听')
            provider = getattr(settings.cloud, 'voice', None)
            if not (getattr(provider, 'supports_voice', False) or getattr(provider, 'plan', False)):
                raise HTTPException(409, '当前模式不支持此音色试听')
            settings.previewing = True
        try:
            return await provider.synthesize(text, voice_settings=voice)
        except Exception:
            raise HTTPException(502, '试听合成失败，请确认音色 ID、供应商权限及服务状态') from None
        finally:
            settings.previewing = False

    return router

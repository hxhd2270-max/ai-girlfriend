"""Pinned native runtime for the existing companion; separate from S1."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid

import lab
import s1
from gateway import Denied, Ledger, LOCAL_TOKEN, MODEL, create_app

ROOT = Path(__file__).resolve().parent
COMPANION = ROOT.parent / 'desktop-companion-ai'


class DailyLedger(Ledger):
    """All native tasks share the companion's persistent daily chat limit."""
    def __init__(self, path):
        super().__init__(path)

    def reserve(self, request_id, category, task, digest):
        from server import usage
        try:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute('SELECT 1 FROM attempts WHERE request_id=?', (request_id,)).fetchone():
                    raise Denied('DUPLICATE_ATTEMPT', 409)
                # Reserve before network I/O; failures/cancellation are not refunded.
                usage.reserve('chat')
                db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,NULL,?,?,NULL)',
                    (request_id, category, task, digest, time.time(), 'reserved', MODEL))
        except usage.UsageError as exc:
            raise Denied(str(exc), 429 if str(exc) == 'chat_call_limit' else 503) from None
        except sqlite3.Error:
            raise Denied('LEDGER_UNAVAILABLE', 503) from None

    def snapshot(self):
        from server import usage
        result = super().snapshot()
        result['policy'] = {'sharedDailyBudget': usage.usage_status(),
            'allNativeTasksCountAsChat': True, 'retries': 0}
        return result


def configure():
    lab.DATA = ROOT / 'integration-runtime'
    lab.ANCHOR = ROOT / 'integration-local-appdata' / 'N.E.K.O'
    lab.setup()
    lab.offline_guard()
    lab.prepare()
    from utils.config_manager import get_config_manager
    manager = get_config_manager()
    raw = {'coreApi': 'qwen', 'assistApi': 'qwen', 'coreApiKey': LOCAL_TOKEN,
           'enableCustomApi': True, 'disableTts': True, 'textGuardMaxLength': 600,
           'powerful_memory_enabled': True}
    for task in ('conversation', 'summary', 'correction', 'emotion', 'vision', 'agent',
                 'gameMain', 'gameSummary', 'omni', 'tts'):
        raw.update({task + 'ModelProvider': 'custom', task + 'ModelId': MODEL,
                    task + 'ModelUrl': f'http://127.0.0.1:49019/{task}/v1',
                    task + 'ModelApiKey': LOCAL_TOKEN})
    manager.save_json_config('core_config.json', raw)
    # This integration is user-initiated text chat. Upstream defaults enable
    # activity inference even without the native frontend syncing preferences.
    from utils.preferences import save_global_conversation_settings
    if not save_global_conversation_settings({
            'proactiveChatEnabled': False, 'proactiveVisionEnabled': False}):
        raise RuntimeError('Unable to disable autonomous activity in integration')
    character_path = manager.get_config_path('characters.json')
    marker = lab.DATA / '.companion-character-initialized'
    if not marker.exists():
        manager.save_json_config('characters.json', {
            '主人': {'档案名': '用户', '性别': '未知', '昵称': '你'},
            '猫娘': {'知夏': {'昵称': '知夏', '性别': '女', '年龄': 26, '种族': '人类',
                '自称': '我', '核心特质': '自然、温和、诚实，尊重事实，不机械迎合。不假装看见用户，不编造共同经历。',
                '补充设定': '你是桌面陪伴角色。用自然中文交流，每次一两句，尽量60字以内，最多100字。用户打断不代表听完你的回答。不要声称已删除记忆或保证记忆准确。',
                '_reserved': {'avatar': {'type': 'live2d', 'model_path': '/static/yui-lolita/yui-lolita.model3.json'}}}},
            '当前猫娘': '知夏'})
        marker.write_text('知夏', encoding='utf-8')
    return manager


def serve(role, shutdown_token):
    import uvicorn
    from fastapi import Request, HTTPException
    # Keep provider credentials only in the gateway and companion processes.
    sys.path.insert(0, str(COMPANION))
    from server.provider_routes import RuntimeRoutes
    routes = RuntimeRoutes.load(COMPANION) if role in ('gateway', 'companion') else None
    if role == 'gateway':
        from tools.start_plan import read_credential, DEFAULT_KEY_FILE, KEY_FILE_ENV
        key = '' if routes else read_credential(Path(os.environ.get(KEY_FILE_ENV) or DEFAULT_KEY_FILE))
        app = create_app(DailyLedger(ROOT / 'integration-usage.sqlite3'), key,
            request_ids=lambda: str(uuid.uuid4()), app_id='NEKO-COMPANION-GATEWAY', queue_timeout=15, routes=routes)
        port = 49019
    elif role == 'companion':
        from tools.start_plan import runtime_environment, read_credential, DEFAULT_KEY_FILE, KEY_FILE_ENV
        key = '' if routes else read_credential(Path(os.environ.get(KEY_FILE_ENV) or DEFAULT_KEY_FILE))
        os.environ['COMPANION_DIALOGUE_BACKEND'] = 'neko'
        # Deliberately keep all production display history and daily counters
        # in their original database; bootstrap creates a fresh shared session.
        with runtime_environment(False, key):
            from server.app import app
        os.environ['DASHSCOPE_API_KEY'] = key
        os.environ['COMPANION_PROVIDER'] = 'token-plan'
        os.environ['COMPANION_PAID_TEST_AUTHORIZED'] = '1'
        os.environ['COMPANION_CHAT_MODEL'] = MODEL
        port = 8765
    else:
        configure()
        s1.install_bounds()
        if role == 'main':
            from app.main_server import app, set_start_config
            port = 49011
        else:
            from app.memory_server import runtime
            app, port = runtime.app, 49012
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
        access_log=False, log_level='warning', timeout_graceful_shutdown=20))

    async def shutdown(request):
        if request.headers.get('x-integration-control') != shutdown_token:
            raise HTTPException(403)
        server.should_exit = True
        return {'status': 'stopping'}
    # FastAPI resolves Request from function globals, even with future annotations.
    shutdown.__annotations__['request'] = Request
    app.add_api_route('/integration/shutdown', shutdown, methods=['POST'])
    if role == 'main':
        set_start_config({'server': server, 'browser_mode_enabled': False,
                          'shutdown_memory_server_on_exit': False})
    server.run()

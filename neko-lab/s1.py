"""Isolated bounded text/memory experiment; does not change the production launcher."""
from __future__ import annotations

import argparse
import ast
import importlib.abc
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import lab
from gateway import Ledger, LOCAL_TOKEN, MODEL, create_app

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 's1-processes.json'
ROLE = '知夏实验'
PORT = 49019
TARGETS = {
    'main_logic.omni_offline_client._streaming': 'main_logic/omni_offline_client/_streaming.py',
    'main_logic.omni_offline_client._lifecycle': 'main_logic/omni_offline_client/_lifecycle.py',
    'memory.recent': 'memory/recent.py',
    'memory.facts': 'memory/facts.py',
}
EXPECTED_RETRY_PATCHES = {
    'main_logic.omni_offline_client._streaming': 1,
    'main_logic.omni_offline_client._lifecycle': 1,
    'memory.recent': 3, 'memory.facts': 1,
}


class OneAttempt(ast.NodeTransformer):
    def __init__(self):
        self.changes = 0

    def visit_Assign(self, node):
        self.generic_visit(node)
        if any(isinstance(t, ast.Name) and t.id == 'max_retries' for t in node.targets):
            if isinstance(node.value, ast.Constant) and node.value.value == 3:
                node.value = ast.Constant(1)
                self.changes += 1
        return node

    def visit_AsyncFunctionDef(self, node):
        self.generic_visit(node)
        args = node.args.args[-len(node.args.defaults):] if node.args.defaults else []
        for i, arg in enumerate(args):
            if arg.arg == 'max_retries':
                node.args.defaults[i] = ast.Constant(1)
                self.changes += 1
        return node


class BoundedSource(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in TARGETS:
            return importlib.util.spec_from_loader(fullname, self, origin=str(lab.UPSTREAM / TARGETS[fullname]))

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        path = lab.UPSTREAM / TARGETS[module.__name__]
        tree = ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
        transformer = OneAttempt()
        tree = transformer.visit(tree)
        if transformer.changes != EXPECTED_RETRY_PATCHES[module.__name__]:
            raise RuntimeError('Retry patch differs from pinned source: ' + module.__name__)
        module.__file__ = str(path)
        exec(compile(ast.fix_missing_locations(tree), str(path), 'exec'), module.__dict__)


def configure():
    lab.DATA = ROOT / 's1-runtime'
    lab.ANCHOR = ROOT / 's1-local-appdata' / 'N.E.K.O'
    lab.setup()
    lab.offline_guard()
    lab.prepare()
    from utils.config_manager import get_config_manager
    manager = get_config_manager()
    raw = {
        'coreApi': 'qwen', 'assistApi': 'qwen', 'coreApiKey': LOCAL_TOKEN,
        'enableCustomApi': True, 'disableTts': True, 'textGuardMaxLength': 600,
        'powerful_memory_enabled': True,
    }
    for prefix in ('conversation', 'summary', 'correction', 'emotion', 'vision', 'agent',
                   'gameMain', 'gameSummary', 'omni', 'tts'):
        raw.update({prefix + 'ModelProvider': 'custom', prefix + 'ModelId': MODEL,
                    prefix + 'ModelUrl': f'http://127.0.0.1:{PORT}/{prefix}/v1',
                    prefix + 'ModelApiKey': LOCAL_TOKEN})
    manager.save_json_config('core_config.json', raw)
    characters = {
        '主人': {'档案名': '测试者', '性别': '未知', '昵称': '测试者'},
        '猫娘': {ROLE: {'昵称': '知夏', '性别': '女', '年龄': 26, '种族': '人类',
            '自称': '我', '核心特质': '自然、温和、诚实。不编造共同经历，不知道就说不知道。',
            '补充设定': '本次为虚构资料测试。请用简短自然的中文回答。用户临时情绪不代表永久特征。',
            '_reserved': {'avatar': {'type': 'live2d', 'model_path': '/static/yui-lolita/yui-lolita.model3.json'}}}},
        '当前猫娘': ROLE,
    }
    manager.save_json_config('characters.json', characters)
    cfg = manager.get_core_config()
    if not cfg.get('DISABLE_TTS'):
        raise RuntimeError('TTS must remain disabled')
    for task in ('conversation', 'summary', 'correction', 'emotion', 'vision', 'agent'):
        actual = manager.get_model_api_config(task)
        if actual.get('base_url', '').rstrip('/') != f'http://127.0.0.1:{PORT}/{task}/v1' or actual.get('model') != MODEL:
            raise RuntimeError('Unexpected model route: ' + task)
    return manager


def install_bounds():
    sys.meta_path.insert(0, BoundedSource())
    from utils.llm_client.openai_client import ChatOpenAI
    original = ChatOpenAI.__init__
    def init(self, *args, **kwargs):
        kwargs['max_retries'] = 0
        return original(self, *args, **kwargs)
    ChatOpenAI.__init__ = init
    from main_logic.omni_offline_client import OmniOfflineClient
    original_omni = OmniOfflineClient.__init__
    def omni(self, *args, **kwargs):
        original_omni(self, *args, **kwargs)
        self.max_response_rerolls = 0
    OmniOfflineClient.__init__ = omni


def serve(role):
    import uvicorn
    if role == 'gateway':
        # Import only the existing credential reader, never the production server.
        import importlib.util
        path = ROOT.parent / 'desktop-companion-ai' / 'tools' / 'start_plan.py'
        spec = importlib.util.spec_from_file_location('companion_key_reader', path)
        loader = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loader)
        selected = Path(os.environ.get(loader.KEY_FILE_ENV) or loader.DEFAULT_KEY_FILE)
        key = loader.read_credential(selected)
        app = create_app(Ledger(ROOT / 's1-usage.sqlite3'), key)
        port = PORT
    else:
        configure()
        install_bounds()
        if role == 'main':
            from app.main_server import app, set_start_config
            port = 49011
        else:
            from app.memory_server import runtime
            app, port = runtime.app, 49012
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                         access_log=False, log_level='warning', timeout_graceful_shutdown=15))
    @app.post('/lab/shutdown')
    async def shutdown():
        server.should_exit = True
        return {'status': 'stopping'}
    if role == 'main':
        set_start_config({'server': server, 'browser_mode_enabled': False,
                          'shutdown_memory_server_on_exit': False})
    server.run()


def live_records():
    import psutil
    if not STATE.exists():
        return []
    records = []
    for r in json.loads(STATE.read_text(encoding='utf-8')):
        try:
            proc = psutil.Process(r['pid'])
            if abs(proc.create_time() - r['created']) < .1 and str(Path(__file__).resolve()) in proc.cmdline():
                records.append((r, proc))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return records


def stop():
    import httpx
    results = []
    for r, proc in reversed(live_records()):
        graceful = False
        try:
            with httpx.Client(trust_env=False, timeout=4) as client:
                response = client.post(f'http://127.0.0.1:{r["port"]}/lab/shutdown')
                response.raise_for_status()
            proc.wait(timeout=25)
            graceful = True
        except Exception:
            proc.terminate()
            proc.wait(timeout=8)
        results.append({'role': r['role'], 'graceful': graceful})
    if STATE.exists():
        STATE.unlink()
    evidence = ROOT / 'evidence'
    evidence.mkdir(exist_ok=True)
    (evidence / 's1-shutdown.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    lab.emit(results)


def start():
    import psutil
    import httpx
    if live_records() or lab.tracked():
        raise RuntimeError('An experiment is already running')
    records = []
    (ROOT / 'logs').mkdir(exist_ok=True)
    try:
        for role, port in [('gateway', PORT), ('memory', 49012), ('main', 49011)]:
            import socket
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', port))
            log = ROOT / 'logs' / (time.strftime('%Y%m%d-%H%M%S') + '-s1-' + role + '.log')
            with log.open('wb') as output:
                proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), role], cwd=ROOT,
                    stdout=output, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            records.append({'pid': proc.pid, 'created': psutil.Process(proc.pid).create_time(),
                            'role': role, 'port': port, 'log': str(log)})
            STATE.write_text(json.dumps(records, indent=2), encoding='utf-8')
            deadline = time.monotonic() + 55
            with httpx.Client(trust_env=False, timeout=3) as client:
                while True:
                    if proc.poll() is not None:
                        raise RuntimeError(role + ' exited; inspect sanitized log')
                    try:
                        response = client.get(f'http://127.0.0.1:{port}/health')
                        value = response.json()
                        valid = value.get('app') == 'NEKO-S1-GATEWAY' if role == 'gateway' else value.get('service') == role
                        if response.status_code == 200 and valid and value.get('status') == 'ok':
                            break
                    except Exception:
                        pass
                    if time.monotonic() > deadline:
                        raise RuntimeError(role + ' startup timeout')
                    time.sleep(.3)
            lab.emit({'started': role, 'port': port})
    except BaseException:
        stop()
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'start', 'stop', 'main', 'memory', 'gateway', 'usage'])
    action = parser.parse_args().action
    if action in ('main', 'memory', 'gateway'):
        serve(action)
    elif action == 'prepare':
        configure()
    elif action == 'usage':
        lab.emit(Ledger(ROOT / 's1-usage.sqlite3').snapshot())
    else:
        globals()[action]()

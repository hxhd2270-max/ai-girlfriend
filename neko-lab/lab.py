"""Pinned N.E.K.O. startup lab with loopback-only application guards."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import runpy
import socket
import subprocess
import sys
import tarfile
import time
from urllib.parse import quote
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parent
UPSTREAM = ROOT / 'upstream'
PIN = '435a033983152e13520b1f6cb9912ea0e8c9dc75'
DATA = ROOT / 'runtime'
ANCHOR = ROOT / 'local-appdata' / 'N.E.K.O'
LOGS = ROOT / 'logs'
STATE = ROOT / 'processes.json'
PORTS = {'MAIN_SERVER_PORT': 49011, 'MEMORY_SERVER_PORT': 49012,
         'MONITOR_SERVER_PORT': 49013, 'COMMENTER_SERVER_PORT': 49014,
         'TOOL_SERVER_PORT': 49015, 'USER_PLUGIN_SERVER_PORT': 49016,
         'AGENT_MQ_PORT': 49017, 'MAIN_AGENT_EVENT_PORT': 49018}


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def setup():
    actual = subprocess.check_output(['git', '-C', str(UPSTREAM), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != PIN:
        raise RuntimeError('Upstream commit differs from the pinned version')
    # Restrict child configuration; never print values inherited from the caller.
    for key in list(os.environ):
        upper = key.upper()
        if any(part in upper for part in ('API_KEY', 'APIKEY', 'ACCESS_TOKEN', 'SECRET', 'PROXY')):
            os.environ.pop(key, None)
    for key in list(os.environ):
        if key.startswith('NEKO_'):
            os.environ.pop(key, None)
    for name, value in PORTS.items():
        os.environ['NEKO_' + name] = str(value)
    os.environ.update({
        'LOCALAPPDATA': str(ROOT / 'local-appdata'),
        'APPDATA': str(ROOT / 'roaming-appdata'),
        'NEKO_STORAGE_SELECTED_ROOT': str(DATA),
        'NEKO_STORAGE_ANCHOR_ROOT': str(ANCHOR),
        'NEKO_CLOUDSAVE_DISABLED': 'isolated_lab_offline',
        # Plain HTTP to a non-loopback host is rejected by upstream registration
        # before it can load/send its generated local client proof.
        'NEKO_SOCIAL_BASE_URL': 'http://offline.invalid',
        'NEKO_DO_NOT_TRACK': '1', 'DO_NOT_TRACK': '1',
        'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8',
        'PLAYWRIGHT_BROWSERS_PATH': str(ROOT / 'cache' / 'playwright'),
    })
    for path in (DATA, ANCHOR, LOGS, ROOT / 'roaming-appdata'):
        path.mkdir(parents=True, exist_ok=True)
    os.chdir(UPSTREAM)
    sys.path.insert(0, str(UPSTREAM))


def offline_guard():
    # Smoke-only guard, installed before importing upstream. The normal application
    # must not use this wrapper for real inference. No local forward proxy is used.
    allowed = {'127.0.0.1', '::1', 'localhost', 'localhost.'}

    def audit(event, args):
        address = None
        if event in ('socket.connect', 'socket.sendto'):
            address = args[-1]
        elif event == 'socket.getaddrinfo':
            address = (args[0],)
        if isinstance(address, tuple) and address:
            host = address[0]
            if isinstance(host, bytes):
                host = host.decode('ascii', errors='replace')
            if host not in allowed and host not in (None, ''):
                raise PermissionError('NEKO lab offline guard: external network disabled')
    sys.addaudithook(audit)
    # Windows Proactor ConnectEx can bypass socket.connect audit events. Guard
    # HTTP requests as well as loop connection setup, before importing upstream.
    import httpx
    original_send = httpx.Client.send
    original_async_send = httpx.AsyncClient.send

    def validate_host(host):
        if host not in allowed:
            raise PermissionError('NEKO lab offline guard: external network disabled')

    def guarded_send(self, request, *args, **kwargs):
        validate_host(request.url.host)
        return original_send(self, request, *args, **kwargs)

    async def guarded_async_send(self, request, *args, **kwargs):
        validate_host(request.url.host)
        return await original_async_send(self, request, *args, **kwargs)

    httpx.Client.send = guarded_send
    httpx.AsyncClient.send = guarded_async_send
    original_create_connection = asyncio.BaseEventLoop.create_connection

    async def guarded_connection(self, protocol_factory, host=None, port=None, **kwargs):
        if host is not None:
            validate_host(host)
        sock = kwargs.get('sock')
        if sock is not None:
            validate_host(sock.getpeername()[0])
        return await original_create_connection(self, protocol_factory, host, port, **kwargs)

    asyncio.BaseEventLoop.create_connection = guarded_connection
    if sys.platform == 'win32':
        from asyncio.proactor_events import BaseProactorEventLoop
        original_sock_connect = BaseProactorEventLoop.sock_connect

        async def guarded_sock_connect(self, sock, address):
            validate_host(address[0])
            return await original_sock_connect(self, sock, address)

        BaseProactorEventLoop.sock_connect = guarded_sock_connect
    try:
        httpx.get('https://example.invalid/lab-guard-test')
    except PermissionError:
        pass
    else:
        raise RuntimeError('Offline HTTP guard self-check failed')


def prepare():
    from utils.config_manager import get_config_manager
    from utils.storage.policy import save_storage_policy
    from utils.storage.location_bootstrap import build_storage_location_bootstrap_payload
    from utils.cloudsave_runtime import assert_cloudsave_writable

    manager = get_config_manager(migrate=False)
    legacy = [str(p) for p in manager.get_legacy_app_root_candidates() if p.exists()]
    if legacy:
        raise RuntimeError('Legacy N.E.K.O. root exists; refuse automatic migration: ' + ', '.join(legacy))
    for path in (manager.app_docs_dir, manager.anchor_root):
        Path(path).resolve().relative_to(ROOT)
    if not (ANCHOR / 'state' / 'storage_policy.json').exists():
        save_storage_policy(manager, selected_root=DATA, selection_source='user_selected', anchor_root=ANCHOR)
    manager = get_config_manager()
    manager.ensure_memory_directory()
    assert_cloudsave_writable(manager, operation='lab_smoke_probe')
    probe = manager.memory_dir / '.lab-write-probe'
    probe.write_text('offline storage probe', encoding='utf-8')
    probe.unlink()
    result = build_storage_location_bootstrap_payload(manager)
    if result.get('blocking_reason'):
        raise RuntimeError(result['blocking_reason'])
    emit({'commit': PIN, 'storage': result, 'memory_write_probe': True, 'legacy_roots_present': legacy})


def assets():
    runpy.run_path(str(UPSTREAM / 'scripts' / 'unpack_builtin_pngtuber.py'), run_name='__main__')
    for name in ('yui-origin', 'yui-lolita'):
        destination = UPSTREAM / 'static' / name
        expected = [f'{name}.moc3', f'{name}.model3.json', f'{name}.physics3.json',
                    f'{name}.vtube.json', f'{name}.4096/texture_00.png']
        if not all((destination / f).is_file() for f in expected):
            # Pristine source has no built-in model folders. Refuse partial overwrite.
            if destination.exists():
                raise RuntimeError('Partial asset directory requires review: ' + str(destination))
            archive = UPSTREAM / 'assets' / f'{name}.tar.gz'
            with tarfile.open(archive, 'r:gz') as tar:
                for member in tar.getmembers():
                    (UPSTREAM / 'static' / member.name).resolve().relative_to(destination.resolve())
                tar.extractall(UPSTREAM / 'static', filter='data')
        if not all((destination / f).is_file() for f in expected):
            raise RuntimeError('Incomplete built-in assets: ' + name)
        (destination / '.unpacked').touch()
        emit({'asset': name, 'required_files_present': True})


def request(path, port=49011):
    with build_opener(ProxyHandler({})).open(f'http://127.0.0.1:{port}{path}', timeout=12) as response:
        data = response.read()
        kind = response.headers.get('Content-Type', '')
        return response.status, json.loads(data) if 'application/json' in kind else len(data)


def tracked():
    import psutil
    if not STATE.exists():
        return []
    live = []
    for record in json.loads(STATE.read_text(encoding='utf-8')):
        try:
            process = psutil.Process(record['pid'])
            command = process.cmdline()
            if abs(process.create_time() - record['created']) < 0.1 and str(Path(__file__).resolve()) in command and record['role'] in command:
                live.append((record, process))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return live


def start():
    import psutil
    if tracked():
        raise RuntimeError('Lab already running; use check or stop')
    for port in PORTS.values():
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', port))
    records = []
    try:
        for role, port in [('memory', 49012), ('main', 49011)]:
            stamp = time.strftime('%Y%m%d-%H%M%S')
            log = LOGS / f'{stamp}-{role}.log'
            with log.open('wb') as output:
                child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), role],
                    cwd=UPSTREAM, stdout=output, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            record = {'role': role, 'pid': child.pid, 'created': psutil.Process(child.pid).create_time(), 'log': str(log)}
            records.append(record)
            STATE.write_text(json.dumps(records, indent=2), encoding='utf-8')
            deadline = time.monotonic() + 55
            while True:
                if child.poll() is not None:
                    raise RuntimeError(f'{role} exited with {child.returncode}; see {log}')
                try:
                    status, health = request('/health', port)
                    if status == 200 and health.get('service') == role:
                        break
                except Exception:
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError(f'{role} did not become healthy within 55 seconds; see {log}')
                time.sleep(0.4)
            emit(record)
    except Exception:
        stop()
        raise


async def websocket_probe(character):
    import websockets
    async with websockets.connect('ws://127.0.0.1:49011/ws/' + quote(character, safe=''),
                                  origin='http://127.0.0.1:49011', open_timeout=10) as ws:
        await ws.send(json.dumps({'action': 'ping'}))
        for _ in range(15):
            event = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
            if event.get('type') == 'pong':
                return {'connected': True, 'pong': True, 'inference_requested': False}
        raise RuntimeError('No pong received')


def check():
    results = {'commit': PIN, 'network_policy': 'loopback_only_application_guards',
               'provider_credentials_loaded': False,
               'observed_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')}
    for role, port in [('memory', 49012), ('main', 49011)]:
        status, data = request('/health', port)
        if status != 200 or data.get('service') != role or data.get('app') != 'N.E.K.O' or data.get('status') != 'ok':
            raise RuntimeError('Unexpected health signature: ' + role)
        results[role + '_health'] = data
    _, storage = request('/api/storage/location/bootstrap')
    if storage.get('blocking_reason') or any(storage.get(flag) for flag in
            ('selection_required', 'migration_pending', 'recovery_required')):
        raise RuntimeError('Storage still blocks startup')
    Path(storage['current_root']).resolve().relative_to(ROOT)
    results['storage'] = storage
    paths = ['/', '/api/characters', '/static/react/neko-chat/neko-chat-window.iife.js',
             '/static/yui-lolita/yui-lolita.model3.json', '/static/yui-lolita/yui-lolita.4096/texture_00.png']
    results['http'] = {}
    for path in paths:
        status, data = request(path)
        if status != 200:
            raise RuntimeError('HTTP check failed: ' + path)
        results['http'][path] = {'status': status, 'bytes_or_json': data if isinstance(data, int) else 'json'}
        if path == '/api/characters':
            characters = data
    character = next(iter(characters.get('猫娘', {})), None)
    if not character:
        raise RuntimeError('No lab character available for transport probe')
    results['websocket'] = asyncio.run(websocket_probe(character))
    results['limitations'] = ['No model inference', 'No memory recall or forget acceptance',
        'No TTS or Wallpaper Engine integration', 'No browser rendering acceptance',
        'Application network guards are not an OS firewall', 'Graceful shutdown and memory flush not tested']
    evidence = ROOT / 'evidence'
    evidence.mkdir(exist_ok=True)
    (evidence / 'startup-check.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    emit(results)


def stop():
    for record, process in reversed(tracked()):
        process.terminate()
        try:
            process.wait(timeout=8)
        except Exception:
            process.kill()
            process.wait(timeout=5)
        emit({'stopped': record['role'], 'pid': record['pid']})
    if STATE.exists():
        STATE.unlink()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'assets', 'start', 'check', 'stop', 'main', 'memory'])
    action = parser.parse_args().action
    if action == 'stop':
        # Cleanup must still work if the checkout or network configuration changed.
        stop()
        sys.exit(0)
    setup()
    offline_guard()
    if action in ('main', 'memory'):
        # Each service process independently checks legacy roots before any migration.
        prepare()
        sys.argv = ['app.' + action + '_server']
        runpy.run_module('app.' + action + '_server', run_name='__main__')
    else:
        globals()[action]()

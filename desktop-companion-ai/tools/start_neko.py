"""Start/stop the complete loopback integration without touching other services."""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
PORTABLE = (ROOT / 'portable.json').is_file()
LAB = ROOT / 'backend/neko' if PORTABLE else ROOT.parent / 'neko-lab'
PYTHON = ROOT / 'runtime/python.exe' if PORTABLE else LAB / 'upstream/.venv/Scripts/python.exe'
STATE = ROOT / 'data/neko-processes.json'
ROLES = [('gateway', 49019), ('memory', 49012), ('main', 49011), ('companion', 8765)]


def records():
    import psutil
    if not STATE.exists():
        return []
    result = []
    for record in json.loads(STATE.read_text(encoding='utf-8')):
        try:
            process = psutil.Process(record['pid'])
            command = process.cmdline()
            if (abs(process.create_time() - record['created']) < .1
                    and str(Path(__file__).resolve()) in command and '--child' in command
                    and record['role'] in command):
                result.append((record, process))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return result


def health(role, port):
    import httpx
    with httpx.Client(trust_env=False, timeout=3) as client:
        response = client.get(f'http://127.0.0.1:{port}/health')
        value = response.json()
        if response.status_code != 200:
            return False
        if role == 'companion':
            return value.get('ok') is True and value.get('dialogueBackend') == 'neko'
        if role == 'gateway':
            return value.get('app') == 'NEKO-COMPANION-GATEWAY' and value.get('status') == 'ok'
        return value.get('service') == role and value.get('status') == 'ok'


def stop():
    import httpx
    import psutil
    live = records()
    for record, process in reversed(live):
        try:
            with httpx.Client(trust_env=False, timeout=3) as client:
                client.post(f"http://127.0.0.1:{record['port']}/integration/shutdown",
                    headers={'x-integration-control': record['control'],
                             'origin': 'http://127.0.0.1:8765'})
            process.wait(30)
        except (httpx.HTTPError, psutil.TimeoutExpired):
            # Only the exact PID + creation time + launcher command identified
            # above can be stopped. Never kill by port or executable name.
            try:
                process.terminate()
                process.wait(5)
            except psutil.NoSuchProcess:
                pass
        except psutil.NoSuchProcess:
            pass
    if records():
        raise RuntimeError('Some integration processes are still running.')
    if STATE.exists():
        STATE.unlink()
    print('N.E.K.O. integration stopped.', flush=True)


def start(preflight_done=False):
    import psutil
    if PORTABLE and not preflight_done:
        from tools.portable_runtime import preflight
        preflight(ROOT, cloud=True)
    live = records()
    if live:
        if len(live) == len(ROLES) and all(health(r['role'], r['port']) for r, _ in live):
            print('N.E.K.O. integration already running. Open http://127.0.0.1:8765/', flush=True)
            return
        raise RuntimeError('Partial integration found; use stop-neko.cmd before restarting.')
    for _, port in ROLES:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', port))
    logdir = ROOT / 'data/neko-logs'
    logdir.mkdir(parents=True, exist_ok=True)
    spawned = []
    from server.provider_routes import RouteStore, RuntimeRoutes
    store = RouteStore(ROOT)
    revision = store.revision() if store.path.exists() else ''
    if revision:
        RuntimeRoutes.load(ROOT)  # Fail before starting any service on invalid credentials.
    try:
        for role, port in ROLES:
            log = logdir / (time.strftime('%Y%m%d-%H%M%S') + '-' + role + '.log')
            token = secrets.token_urlsafe(32)
            env = dict(os.environ, COMPANION_INTEGRATION_CONTROL=token,
                       PYTHONUTF8='1', PYTHONIOENCODING='utf-8', COMPANION_ROUTES_REVISION=revision)
            # This is a non-secret loopback stop capability, not a provider key.
            # It never appears in process arguments or logs.
            with log.open('wb') as output:
                child = subprocess.Popen([str(PYTHON)] + (['-I', '-B', '-X', 'utf8'] if PORTABLE else []) + [str(Path(__file__).resolve()), '--child', role],
                    cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            spawned.append(dict(pid=child.pid, created=psutil.Process(child.pid).create_time(),
                                role=role, port=port, control=token, log=str(log)))
            STATE.write_text(json.dumps(spawned, indent=2), encoding='utf-8')
            deadline = time.monotonic() + 60
            while True:
                if child.poll() is not None:
                    raise RuntimeError(role + ' exited. Check data/neko-logs.')
                try:
                    if health(role, port):
                        break
                except Exception:
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError(role + ' startup timeout.')
                time.sleep(.3)
            print(role + ' ready on loopback port ' + str(port), flush=True)
    except BaseException:
        stop()
        raise
    print('N.E.K.O. services ready; configured provider access is not verified by startup.', flush=True)
    print('Open http://127.0.0.1:8765/standalone or use start-standalone.cmd for the independent window.', flush=True)
    print('Use stop-neko.cmd to stop all four integration services.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stop', action='store_true')
    parser.add_argument('--child', choices=[r for r, _ in ROLES])
    args = parser.parse_args()
    if args.child:
        sys.path.insert(0, str(LAB))
        from integration import serve
        serve(args.child, os.environ.pop('COMPANION_INTEGRATION_CONTROL'))
    else:
        try:
            stop() if args.stop else start()
        except (Exception, KeyboardInterrupt):
            print('Integration did not start/stop cleanly; check local ports and data/neko-logs. No unrelated service was stopped.', flush=True)
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

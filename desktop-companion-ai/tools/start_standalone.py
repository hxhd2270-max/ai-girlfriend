"""Launch the independent window, preserving existing service ownership."""
import argparse
from http.client import HTTPConnection
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.start_plan import probe_service, StartupError


def check_entry():
    connection = HTTPConnection('127.0.0.1', 8765, timeout=3)
    try:
        connection.request('GET', '/standalone')
        response = connection.getresponse()
        if response.status != 200 or b'/web/standalone.js' not in response.read(32768):
            raise StartupError('Existing service does not have the independent entry. Restart it from its own launcher after saving your work.')
    finally:
        connection.close()


def launch(simulation=False, acceptance=False, desktop=False, checked=False):
    if (ROOT / 'portable.json').is_file() and not checked:
        from tools.portable_runtime import preflight
        preflight(ROOT, cloud=not simulation)
    executable = ROOT / 'desktop-host/bin/Companion.Desktop.exe'
    if not executable.is_file():
        raise StartupError('Build desktop-host/build.ps1 first; the independent window is missing.')
    from server.standalone import ASSET_ROOT, MEDIA
    for name in MEDIA:
        path = ASSET_ROOT / name
        if not path.is_file() or path.resolve() != path.absolute():
            raise StartupError('Independent assets are missing or linked. Run tools/prepare_standalone_assets.py.')
    result = probe_service()
    owned = None
    log = None
    try:
        if result.state == 'unknown':
            raise StartupError('Port 8765 belongs to an unknown or unavailable service; it was left running.')
        if result.state == 'project' and result.mode != ('simulation' if simulation else 'cloud'):
            raise StartupError('Existing service mode differs from the requested mode; stop it from its own launcher before switching.')
        if result.state == 'free':
            if simulation:
                data = Path(os.environ.get('COMPANION_DATA_DIR') or ROOT / 'data/standalone-simulation')
                data.mkdir(parents=True, exist_ok=True)
                log = (data / 'service.log').open('ab')
                env = dict(os.environ, COMPANION_DATA_DIR=str(data), COMPANION_CLOUD_ENABLED='0',
                           COMPANION_DIALOGUE_BACKEND='simulation', COMPANION_PROVIDER='simulation',
                           PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
                owned = subprocess.Popen([sys.executable] + (['-I', '-B', '-X', 'utf8'] if (ROOT / 'portable.json').is_file() else []) + [str(Path(__file__).resolve()), '--simulation-service'],
                    cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                deadline = time.monotonic() + 25
                while probe_service().state != 'project':
                    if owned.poll() is not None or time.monotonic() > deadline:
                        raise StartupError('Local simulation did not start; check its service.log.')
                    time.sleep(.2)
            else:
                # The existing supervisor owns NEKO/memory/gateway/voice. No WE calls.
                from tools.start_neko import start
                start(preflight_done=True) if (ROOT / 'portable.json').is_file() else start()
        elif acceptance:
            raise StartupError('Acceptance needs its own fresh simulation service; existing service was left running.')
        check_entry()
        print(('Desktop wallpaper' if desktop else 'Independent window') + ': http://127.0.0.1:8765/standalone', flush=True)
        print('Mode: ' + ('local simulation; no cloud calls' if simulation else 'configured dialogue and voice backend'), flush=True)
        process = subprocess.Popen([str(executable)] + (['--acceptance'] if acceptance else []) + (['--desktop'] if desktop else []), cwd=ROOT)
        exit_code = process.wait()
        return exit_code if isinstance(exit_code, int) else 0
    finally:
        # Stop only the simulation child created by this launcher instance.
        if owned is not None and owned.poll() is None:
            owned.terminate()
            try:
                owned.wait(8)
            except subprocess.TimeoutExpired:
                owned.kill()
                owned.wait(3)
        if log is not None:
            log.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--simulation', action='store_true')
    parser.add_argument('--desktop', action='store_true', help='Run below desktop icons, with a tray and native chat panel.')
    parser.add_argument('--acceptance', action='store_true', help='Fresh local simulation only; synthetic input/audio.')
    parser.add_argument('--simulation-service', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.simulation_service:
        os.environ['COMPANION_CLOUD_ENABLED'] = '0'
        os.environ['COMPANION_DIALOGUE_BACKEND'] = 'simulation'
        import uvicorn
        uvicorn.run('server.app:app', host='127.0.0.1', port=8765, access_log=False, log_level='warning')
        return 0
    if args.acceptance and not args.simulation:
        parser.error('--acceptance requires --simulation')
    try:
        return launch(args.simulation, args.acceptance, args.desktop)
    except StartupError as error:
        print(str(error), flush=True)
        return 1
    except (Exception, KeyboardInterrupt):
        print('Independent startup stopped. Existing services were left running; check local dependencies and ports.', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

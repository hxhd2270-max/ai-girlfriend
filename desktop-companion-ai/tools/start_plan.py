"""Daily loopback launcher. Importing this module does not start or probe anything."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import re
import sys
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
HOST, PORT = '127.0.0.1', 8765
URL = f'http://{HOST}:{PORT}/'
DEFAULT_KEY_FILE = ROOT / 'config/provider-key.txt'
KEY_FILE_ENV = 'COMPANION_KEY_FILE'
HEALTH_TIMEOUT = 4.0
MAX_HEALTH_BYTES = 16384


class StartupError(Exception):
    """A fixed, credential-free message safe to display."""


class ProbeResult(NamedTuple):
    state: str
    mode: str | None = None


def probe_service() -> ProbeResult:
    """Identify only via GET /health, directly on loopback, without proxies."""
    connection = HTTPConnection(HOST, PORT, timeout=HEALTH_TIMEOUT)
    try:
        connection.request('GET', '/health', headers={'Accept': 'application/json'})
        response = connection.getresponse()
        if response.status != 200:
            return ProbeResult('unknown')
        raw = response.read(MAX_HEALTH_BYTES + 1)
        if len(raw) > MAX_HEALTH_BYTES:
            return ProbeResult('unknown')
        body = json.loads(raw)
        if not isinstance(body, dict):
            return ProbeResult('unknown')
        # /health currently has no application ID. Require its complete stable
        # signature, rather than accepting an unrelated {"ok": true} service.
        mode = body.get('mode')
        calls = body.get('cloudCalls')
        last_error = body.get('lastCloudError')
        valid_error = (last_error is None or isinstance(last_error, str)
                       or (isinstance(last_error, dict)
                           and isinstance(last_error.get('scope'), str)
                           and isinstance(last_error.get('code'), str)
                           and type(last_error.get('time')) is int))
        if (body.get('ok') is True
                and type(body.get('protocol')) is int and body['protocol'] == 1
                and mode in ('cloud', 'simulation')
                and 'lastCloudError' in body
                and valid_error
                and ((mode == 'cloud' and isinstance(calls, dict))
                     or (mode == 'simulation' and type(calls) is int and calls == 0))):
            return ProbeResult('project', mode)
        return ProbeResult('unknown')
    except ConnectionRefusedError:
        return ProbeResult('free')
    except (OSError, HTTPException, ValueError, UnicodeError):
        return ProbeResult('unknown')
    finally:
        connection.close()


def report_listener(result: ProbeResult, simulation: bool) -> int | None:
    if result.state == 'free':
        return None
    if result.state == 'project':
        print(f'Desktop Companion is already running ({result.mode} mode).')
        print(f'Open {URL} directly; no new process was started.')
        requested = 'simulation' if simulation else 'cloud'
        if result.mode != requested:
            print(f'Requested {requested}; the existing service is still {result.mode}.')
        print('To restart or change mode, press Ctrl+C in its original terminal,')
        print('wait for it to exit, then run the desired launcher again.')
        return 0
    print(f'Port {PORT} is occupied or unavailable; GET /health could not identify Desktop Companion.')
    print('The existing service was not stopped. Check its own terminal before retrying.')
    return 1


def read_credential(path: Path) -> str:
    """Read only the selected file, preserving the previous UTF-16 compatibility."""
    managed = ROOT / 'config/provider-key.dpapi'
    path = Path(path)
    if ((path == DEFAULT_KEY_FILE and not os.environ.get(KEY_FILE_ENV))
            or path.absolute() == ROOT / 'config/provider-key.txt') and managed.exists():
        path = managed
    if path.suffix == '.dpapi':
        from server.provider_config import read_encrypted
        try:
            if path.resolve() != path.absolute():
                raise ValueError()
            return read_encrypted(path)
        except (OSError, ValueError, UnicodeError):
            raise StartupError('Cannot read encrypted account settings for this Windows user. Open API settings to replace the credential.') from None
    try:
        raw = path.read_bytes()
        encoding = 'utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
        content = raw.decode(encoding)
    except (OSError, UnicodeError, ValueError):
        raise StartupError('Cannot read the selected key file. Use --key-file or COMPANION_KEY_FILE.') from None
    keys = re.findall(r'sk-[A-Za-z0-9_.-]+', content)
    if len(keys) != 1:
        raise StartupError('The selected key file must contain exactly one sk- credential.')
    return keys[0]


@contextmanager
def runtime_environment(simulation: bool, key: str | None):
    """Set process-only credentials/mode and restore inherited values on exit."""
    settings = {
        'DASHSCOPE_API_KEY': key,
        'COMPANION_PROVIDER': 'simulation' if simulation else 'token-plan',
        'COMPANION_CHAT_MODEL': 'qwen3.8-flash',
        'COMPANION_CLOUD_ENABLED': '0' if simulation else '1',
        'COMPANION_PAID_TEST_AUTHORIZED': None if simulation else '1',
        'COMPANION_FREE_ONLY_CONFIRMED': None,
    }
    previous = {name: os.environ.get(name) for name in settings}
    try:
        for name, value in settings.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def run_server(simulation: bool, key_file: Path):
    # Importing the launcher, --help and repeated launch never import the server.
    import uvicorn

    from server.provider_routes import RuntimeRoutes
    routes = None if simulation else RuntimeRoutes.load(ROOT)
    with runtime_environment(simulation, None if simulation else '' if routes else read_credential(key_file)):
        previous_dir, previous_path = Path.cwd(), sys.path[:]
        try:
            os.chdir(ROOT)
            sys.path.insert(0, str(ROOT))
            if simulation:
                print('Desktop Companion - simulation: local replies and test tones; cloud disabled.')
            else:
                print('Desktop Companion - configured task routes.' if routes else 'Desktop Companion - token-plan text + TTS.')
                if not routes:
                    print('Text: qwen3.8-flash; TTS: qwen-audio-3.0-tts-plus / longanhuan_v3.6.')
            print(f'Open {URL} after startup. Ctrl+C stops this service.')
            print('For a restart, wait for this process to exit, then run the launcher again.')
            uvicorn.run('server.app:app', host=HOST, port=PORT, access_log=False,
                        log_level='warning', reload=False, workers=1)
        finally:
            sys.path[:] = previous_path
            os.chdir(previous_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Start Desktop Companion token-plan text + TTS on loopback.')
    parser.add_argument('--key-file', type=Path, help=f'Credential file: CLI > {KEY_FILE_ENV} > compatibility default; ignored in simulation.')
    parser.add_argument('--simulation', action='store_true', help='Local replies/test tones only; no credentials or cloud.')
    args = parser.parse_args(argv)

    result = report_listener(probe_service(), args.simulation)
    if result is not None:
        return result
    key_file = args.key_file if args.key_file is not None else Path(os.environ.get(KEY_FILE_ENV) or DEFAULT_KEY_FILE)
    try:
        run_server(args.simulation, key_file)
    except StartupError as error:
        print(str(error))
        return 1
    except KeyboardInterrupt:
        pass
    except (OSError, SystemExit) as error:
        if isinstance(error, SystemExit) and error.code in (None, 0, 130):
            print('Desktop Companion stopped. Run the same launcher to restart.')
            return 0
        # A second launcher may win the port between the first probe and bind.
        # Recheck health, never terminate or replace that process.
        result = report_listener(probe_service(), args.simulation)
        if result is not None:
            return result
        print('Startup failed. No existing service was stopped; check local dependencies and retry.')
        return 1
    except Exception:
        # Never echo exception details that could contain upstream credentials.
        print('Startup failed. Check local dependencies (requirements.txt) and retry.')
        return 1
    print('Desktop Companion stopped. Run the same launcher to restart.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

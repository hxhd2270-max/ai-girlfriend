"""Offline launcher checks: no real credentials, network, or service startup."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import runpy
import socket
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

with patch.object(Path, 'read_bytes', side_effect=AssertionError('Import must not read credentials')), \
        patch.object(socket, 'socket', side_effect=AssertionError('Import must not access network')):
    from tools import start_plan as launcher


FAKE_KEY = 'sk-offline-fictional-key'


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # Fail closed: even a mistaken test path cannot touch a credential file
        # or real socket. Individual cases supply bytes and HTTP responses.
        self.file_read = self.stack.enter_context(patch.object(
            Path, 'read_bytes', side_effect=AssertionError('Real file reads forbidden')))
        # A developer's managed account must not select DPAPI in fictional
        # text-file launcher cases. Preserve all unrelated existence checks.
        original_exists = Path.exists
        managed_key = launcher.ROOT / 'config/provider-key.dpapi'
        self.stack.enter_context(patch.object(
            Path, 'exists', lambda path: False if path == managed_key else original_exists(path)))
        self.stack.enter_context(patch.object(socket, 'socket', side_effect=AssertionError('Network forbidden')))
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.server_run = Mock()
        self.stack.enter_context(patch.dict(sys.modules, {'uvicorn': SimpleNamespace(run=self.server_run)}))
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        self.stack.enter_context(redirect_stderr(self.output))

    def response(self, body, status=200):
        response = Mock(status=status)
        response.read.return_value = body if isinstance(body, bytes) else json.dumps(body).encode()
        connection = Mock()
        connection.getresponse.return_value = response
        factory = self.stack.enter_context(patch.object(launcher, 'HTTPConnection', return_value=connection))
        return factory, connection, response

    def invoke(self, argv=(), service=None):
        result = service or launcher.ProbeResult('free')
        with patch.object(launcher, 'probe_service', return_value=result):
            return launcher.main(list(argv))

    def supply_key(self, raw=FAKE_KEY.encode()):
        self.file_read.side_effect = None
        self.file_read.return_value = raw

    def test_import_has_no_startup_side_effects(self):
        previous_dir, previous_path, previous_env = Path.cwd(), sys.path[:], dict(os.environ)
        with patch.object(os, 'chdir') as chdir, patch.object(launcher, 'HTTPConnection') as http:
            runpy.run_path(str(launcher.ROOT / 'tools' / 'start_plan.py'), run_name='offline_import')
        chdir.assert_not_called()
        http.assert_not_called()
        self.file_read.assert_not_called()
        self.server_run.assert_not_called()
        self.assertEqual(Path.cwd(), previous_dir)
        self.assertEqual(sys.path, previous_path)
        self.assertEqual(dict(os.environ), previous_env)
        self.assertEqual(self.output.getvalue(), '')

    def test_help_does_not_probe_or_read_key(self):
        with patch.object(launcher, 'probe_service') as probe, self.assertRaises(SystemExit) as result:
            launcher.main(['--help'])
        self.assertEqual(result.exception.code, 0)
        probe.assert_not_called()
        self.file_read.assert_not_called()
        self.server_run.assert_not_called()
        self.assertIn('--key-file', self.output.getvalue())

    def test_health_recognizes_both_project_modes_and_added_usage_fields(self):
        for mode, calls in [('simulation', 0), ('cloud', {'chat_calls': 2, 'tts_calls': 1})]:
            with self.subTest(mode=mode):
                factory, connection, response = self.response({
                    'ok': True, 'protocol': 1, 'mode': mode,
                    'cloudCalls': calls, 'lastCloudError': None,
                    'dailyUsage': {'persisted': True},
                })
                self.assertEqual(launcher.probe_service(), launcher.ProbeResult('project', mode))
                factory.assert_called_once_with('127.0.0.1', 8765, timeout=launcher.HEALTH_TIMEOUT)
                connection.request.assert_called_once_with('GET', '/health', headers={'Accept': 'application/json'})
                response.read.assert_called_once_with(launcher.MAX_HEALTH_BYTES + 1)
                connection.close.assert_called_once()

    def test_project_health_still_reuses_service_after_a_cloud_error(self):
        self.response({
            'ok': True, 'protocol': 1, 'mode': 'cloud',
            'cloudCalls': {'chat_calls': 2}, 'lastCloudError': 'tts_transport_or_protocol_error',
        })
        self.assertEqual(launcher.main([]), 0)
        self.file_read.assert_not_called()
        self.server_run.assert_not_called()
        self.assertIn('already running', self.output.getvalue())

    def test_structured_cloud_error_still_identifies_existing_project(self):
        self.response({
            'ok': True, 'protocol': 1, 'mode': 'cloud',
            'cloudCalls': {'chat_calls': 2, 'tts_calls': 1},
            'lastCloudError': {
                'scope': 'tts', 'code': 'tts_transport_or_protocol_error', 'time': 1792368000,
            },
        })
        self.assertEqual(launcher.main([]), 0)
        self.file_read.assert_not_called()
        self.server_run.assert_not_called()
        self.assertIn('already running', self.output.getvalue())
        self.assertIn(launcher.URL, self.output.getvalue())
        self.assertNotIn('could not identify', self.output.getvalue())

    def test_health_rejects_unknown_and_invalid_responses(self):
        valid = dict(ok=True, protocol=1, mode='simulation', cloudCalls=0, lastCloudError=None)
        bodies = [b'<html>another app</html>', b'not json', b'\xff', [], None,
                  {'ok': True}, {**valid, 'protocol': True},
                  {**valid, 'mode': 'other'}, {**valid, 'ok': 1},
                  {**valid, 'cloudCalls': False}, {**valid, 'lastCloudError': {}},
                  {k: v for k, v in valid.items() if k != 'lastCloudError'},
                  b'x' * (launcher.MAX_HEALTH_BYTES + 1)]
        for body in bodies:
            with self.subTest(body_type=type(body).__name__):
                _, connection, _ = self.response(body)
                self.assertEqual(launcher.probe_service().state, 'unknown')
                connection.close.assert_called_once()

    def test_health_does_not_follow_redirects(self):
        for status in (301, 302, 403, 404, 500):
            with self.subTest(status=status):
                _, connection, response = self.response({}, status=status)
                self.assertEqual(launcher.probe_service().state, 'unknown')
                connection.request.assert_called_once()
                response.read.assert_not_called()

    def test_only_connection_refused_is_treated_as_free(self):
        for error, state in [(ConnectionRefusedError(), 'free'), (TimeoutError(), 'unknown'),
                             (ConnectionResetError(), 'unknown'), (PermissionError(), 'unknown'),
                             (launcher.HTTPException(), 'unknown')]:
            with self.subTest(error=type(error).__name__):
                _, connection, _ = self.response({})
                connection.request.side_effect = error
                self.assertEqual(launcher.probe_service().state, state)
                connection.close.assert_called_once()

    def test_existing_project_returns_url_without_key_or_new_server(self):
        for mode in ('cloud', 'simulation'):
            with self.subTest(mode=mode):
                self.assertEqual(self.invoke(service=launcher.ProbeResult('project', mode)), 0)
        self.file_read.assert_not_called()
        self.server_run.assert_not_called()
        self.assertIn(launcher.URL, self.output.getvalue())
        self.assertIn('already running', self.output.getvalue())
        self.assertIn('Ctrl+C in its original terminal', self.output.getvalue())

    def test_mode_mismatch_is_explicit_in_both_directions(self):
        self.invoke(service=launcher.ProbeResult('project', 'simulation'))
        self.invoke(['--simulation'], launcher.ProbeResult('project', 'cloud'))
        self.assertIn('Requested cloud; the existing service is still simulation', self.output.getvalue())
        self.assertIn('Requested simulation; the existing service is still cloud', self.output.getvalue())
        self.file_read.assert_not_called()
        self.server_run.assert_not_called()

    def test_unknown_listener_is_preserved_without_reading_key(self):
        self.assertEqual(self.invoke(service=launcher.ProbeResult('unknown')), 1)
        self.file_read.assert_not_called()
        self.server_run.assert_not_called()
        self.assertIn('could not identify Desktop Companion', self.output.getvalue())
        self.assertIn('not stopped', self.output.getvalue())

    def test_cli_environment_default_path_priority(self):
        with patch.object(launcher, 'run_server') as run:
            self.assertEqual(self.invoke(), 0)
            run.assert_called_with(False, launcher.DEFAULT_KEY_FILE)
            os.environ[launcher.KEY_FILE_ENV] = 'environment-only.txt'
            self.assertEqual(self.invoke(), 0)
            run.assert_called_with(False, Path('environment-only.txt'))
            self.assertEqual(self.invoke(['--key-file', 'chosen folder/密钥.txt']), 0)
            run.assert_called_with(False, Path('chosen folder/密钥.txt'))
        self.file_read.assert_not_called()

    def test_key_file_encoding_compatibility(self):
        text = '说明：只用于离线测试\n' + FAKE_KEY + '\n'
        for encoding in ('utf-8', 'utf-8-sig', 'utf-16', 'utf-16-be'):
            with self.subTest(encoding=encoding):
                raw = text.encode(encoding)
                if encoding == 'utf-16-be':
                    raw = b'\xfe\xff' + raw
                self.supply_key(raw)
                self.assertEqual(launcher.read_credential(Path('fictional.txt')), FAKE_KEY)

    def test_key_file_errors_are_sanitized(self):
        for raw in (b'empty', b'sk-first sk-second', b'\xffinvalid'):
            with self.subTest(raw=raw):
                self.supply_key(raw)
                with self.assertRaises(launcher.StartupError):
                    launcher.read_credential(Path('fictional.txt'))
        self.file_read.side_effect = PermissionError(FAKE_KEY)
        with self.assertRaises(launcher.StartupError) as error:
            launcher.read_credential(Path('fictional.txt'))
        self.assertNotIn(FAKE_KEY, str(error.exception))

    def test_selected_key_is_read_once_and_cloud_environment_exists_only_during_run(self):
        self.supply_key()
        previous_env = {
            'DASHSCOPE_API_KEY': 'inherited-fictional-key',
            'COMPANION_CHAT_MODEL': 'auto',
            'COMPANION_PROVIDER': 'previous-provider',
            'COMPANION_FREE_ONLY_CONFIRMED': '1',
            'COMPANION_CLOUD_ENABLED': '0',
            'COMPANION_PAID_TEST_AUTHORIZED': '0',
        }
        os.environ.update(previous_env)
        previous_dir, previous_path = Path.cwd(), sys.path[:]
        observed = {}

        def fake_run(*args, **kwargs):
            observed.update(os.environ)
            self.assertEqual(Path.cwd(), launcher.ROOT)
            self.assertEqual(sys.path[0], str(launcher.ROOT))

        self.server_run.side_effect = fake_run
        self.assertEqual(self.invoke(['--key-file', 'offline-selected.txt']), 0)
        self.file_read.assert_called_once()
        self.assertEqual(observed['DASHSCOPE_API_KEY'], FAKE_KEY)
        self.assertEqual(observed['COMPANION_PROVIDER'], 'token-plan')
        self.assertEqual(observed['COMPANION_CHAT_MODEL'], 'qwen3.8-flash')
        self.assertEqual(observed['COMPANION_CLOUD_ENABLED'], '1')
        self.assertEqual(observed['COMPANION_PAID_TEST_AUTHORIZED'], '1')
        self.assertNotIn('COMPANION_FREE_ONLY_CONFIRMED', observed)
        self.assertEqual(dict(os.environ), previous_env)
        self.assertEqual(Path.cwd(), previous_dir)
        self.assertEqual(sys.path, previous_path)
        self.server_run.assert_called_once_with(
            'server.app:app', host='127.0.0.1', port=8765, access_log=False,
            log_level='warning', reload=False, workers=1)
        self.assertNotIn(FAKE_KEY, self.output.getvalue())
        self.assertIn('Ctrl+C stops this service', self.output.getvalue())

    def test_relative_key_is_opened_before_directory_changes(self):
        with patch.object(launcher, 'read_credential', return_value=FAKE_KEY) as read:
            self.assertEqual(self.invoke(['--key-file', 'relative/fictional.txt']), 0)
        read.assert_called_once_with(Path('relative/fictional.txt'))

    def test_simulation_overrides_inherited_cloud_without_reading_key(self):
        inherited = {
            'DASHSCOPE_API_KEY': FAKE_KEY, 'COMPANION_CLOUD_ENABLED': '1',
            'COMPANION_PAID_TEST_AUTHORIZED': '1', 'COMPANION_FREE_ONLY_CONFIRMED': '1',
            'COMPANION_PROVIDER': 'token-plan', launcher.KEY_FILE_ENV: 'never-read.txt',
        }
        os.environ.update(inherited)

        def fake_run(*args, **kwargs):
            self.assertEqual(os.environ['COMPANION_CLOUD_ENABLED'], '0')
            self.assertEqual(os.environ['COMPANION_PROVIDER'], 'simulation')
            self.assertNotIn('DASHSCOPE_API_KEY', os.environ)
            self.assertNotIn('COMPANION_PAID_TEST_AUTHORIZED', os.environ)
            self.assertNotIn('COMPANION_FREE_ONLY_CONFIRMED', os.environ)

        self.server_run.side_effect = fake_run
        self.assertEqual(self.invoke(['--simulation', '--key-file', 'also-never-read.txt']), 0)
        self.file_read.assert_not_called()
        self.assertEqual(dict(os.environ), inherited)
        self.assertIn('cloud disabled', self.output.getvalue())

    def test_missing_key_stops_before_service_start(self):
        self.file_read.side_effect = FileNotFoundError(FAKE_KEY)
        self.assertEqual(self.invoke(), 1)
        self.server_run.assert_not_called()
        self.assertEqual(dict(os.environ), {})
        self.assertIn('--key-file', self.output.getvalue())
        self.assertNotIn(FAKE_KEY, self.output.getvalue())

    def test_error_and_interrupt_restore_environment_and_directory(self):
        self.supply_key()
        previous_dir, previous_path = Path.cwd(), sys.path[:]
        for error, exit_code in [(RuntimeError(FAKE_KEY), 1), (KeyboardInterrupt(), 0),
                                 (SystemExit(0), 0), (OSError(FAKE_KEY), 1)]:
            with self.subTest(error=type(error).__name__):
                self.server_run.side_effect = error
                self.assertEqual(self.invoke(), exit_code)
                self.assertEqual(dict(os.environ), {})
                self.assertEqual(Path.cwd(), previous_dir)
                self.assertEqual(sys.path, previous_path)
        self.assertNotIn(FAKE_KEY, self.output.getvalue())

    def test_bind_race_rechecks_health_and_preserves_winning_service(self):
        for result, exit_code in [(launcher.ProbeResult('project', 'cloud'), 0),
                                  (launcher.ProbeResult('unknown'), 1)]:
            with self.subTest(state=result.state):
                self.supply_key()
                self.server_run.side_effect = SystemExit(1)
                with patch.object(launcher, 'probe_service', side_effect=[launcher.ProbeResult('free'), result]) as probe:
                    self.assertEqual(launcher.main([]), exit_code)
                self.assertEqual(probe.call_count, 2)
                self.assertEqual(dict(os.environ), {})


@unittest.skipUnless(os.name == 'nt', 'Windows batch launchers')
class WindowsEntryTests(unittest.TestCase):
    def test_all_entries_forward_help_and_quoted_paths_without_starting(self):
        # --help exits before health or credential access, including through cmd.
        for name in ('start.cmd', 'start-cloud.cmd', 'start-simulation.cmd'):
            with self.subTest(entry=name):
                env = dict(os.environ, COMPANION_NO_PAUSE='1', PYTHONDONTWRITEBYTECODE='1')
                command = 'call "{}" --key-file "fictional folder/密钥.txt" --help'.format(launcher.ROOT / name)
                result = subprocess.run(
                    '"{}" /d /c {}'.format(env.get('COMSPEC', 'cmd.exe'), command),
                    cwd=launcher.ROOT.parent, env=env, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(b'--key-file', result.stdout)
                self.assertIn(b'--simulation', result.stdout)

    def test_entries_preserve_argument_error_exit_status(self):
        for name in ('start.cmd', 'start-cloud.cmd', 'start-simulation.cmd'):
            with self.subTest(entry=name):
                env = dict(os.environ, COMPANION_NO_PAUSE='1', PYTHONDONTWRITEBYTECODE='1')
                command = 'call "{}" --unknown-offline-argument'.format(launcher.ROOT / name)
                result = subprocess.run(
                    '"{}" /d /c {}'.format(env.get('COMSPEC', 'cmd.exe'), command),
                    cwd=launcher.ROOT.parent, env=env, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()

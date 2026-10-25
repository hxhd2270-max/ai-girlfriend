"""Offline daily-limit tests; every database lives in a temporary data directory."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from server import usage as u


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.data = Path(self.directory) / 'data'
        self.config = Path(self.directory) / 'limits.json'
        self.enterContext(patch.dict(os.environ, {
            'COMPANION_DATA_DIR': str(self.data),
            'COMPANION_USAGE_LIMITS_PATH': str(self.config),
        }))
        self.enterContext(patch.object(socket.socket, 'connect',
                                       side_effect=AssertionError('network forbidden')))
        self.enterContext(patch.object(socket, 'getaddrinfo',
                                       side_effect=AssertionError('DNS forbidden')))
        # Deterministic except tests explicitly exercising the Shanghai boundary.
        self.enterContext(patch.object(u, '_now',
                                       return_value=datetime(2026, 10, 19, 4, tzinfo=timezone.utc)))
        self.write_limits()

    def write_limits(self, chat=40, tts=32, characters=3000):
        self.config.write_text(json.dumps(dict(chat_calls=chat, tts_calls=tts,
                                               tts_estimated_characters=characters)), encoding='utf-8')

    def test_default_config_and_status_without_credentials(self):
        os.environ.pop('COMPANION_USAGE_LIMITS_PATH')
        status = u.usage_status()
        self.assertEqual(status['date'], '2026-10-19')
        self.assertEqual(status['timezone'], 'Asia/Shanghai')
        self.assertEqual(status['limits'], dict.fromkeys(u.COUNTER_KEYS, None))
        self.assertEqual(status['reserved'], dict.fromkeys(u.COUNTER_KEYS, 0))
        self.assertFalse(status['freeBalanceVerified'])
        self.assertFalse(status['finalBillKnown'])
        self.assertIn('不是供应商账单', status['disclaimer'])
        status['reserved']['chat_calls'] = -1
        status['limits']['chat_calls'] = -1
        self.assertEqual(u.counters()['chat_calls'], 0)
        self.assertIsNone(u.usage_status()['limits']['chat_calls'])
        self.assertTrue((self.data / 'usage.sqlite3').is_file())

    def test_config_is_reloadable_finite_and_preserves_usage(self):
        self.write_limits(chat=1, tts=1, characters=4)
        u.reserve('chat')
        u.reserve('tts', 4)
        for kind, estimate, code in [('chat', 0, 'chat_call_limit'), ('tts', 0, 'tts_call_limit')]:
            with self.assertRaisesRegex(u.UsageError, '^' + code + '$'):
                u.reserve(kind, estimate)
        self.write_limits(chat=2, tts=2, characters=5)
        u.reserve('chat')
        with self.assertRaisesRegex(u.UsageError, '^tts_character_limit$'):
            u.reserve('tts', 2)
        # A rejected character reservation must not consume a TTS attempt.
        u.reserve('tts', 1)
        self.assertEqual(u.counters(), dict(chat_calls=2, tts_calls=2, tts_estimated_characters=5))
        self.write_limits(chat=1, tts=0, characters=0)
        with self.assertRaisesRegex(u.UsageError, '^chat_call_limit$'):
            u.reserve('chat')
        self.assertEqual(u.usage_status()['reserved']['chat_calls'], 2)

    def test_zero_budgets_disable_without_unlimited_fallback(self):
        self.write_limits(chat=0, tts=0, characters=0)
        with self.assertRaisesRegex(u.UsageError, '^chat_call_limit$'):
            u.reserve('chat')
        with self.assertRaisesRegex(u.UsageError, '^tts_call_limit$'):
            u.reserve('tts', 1)
        self.write_limits(chat=0, tts=1, characters=0)
        with self.assertRaisesRegex(u.UsageError, '^tts_character_limit$'):
            u.reserve('tts', 1)
        self.assertEqual(sum(u.counters().values()), 0)

    def test_unlimited_keeps_usage_and_can_restore_independent_limits(self):
        self.write_limits(chat=1, tts=1, characters=4)
        u.reserve('chat')
        u.reserve('tts', 4)
        self.write_limits(chat=None, tts=None, characters=None)
        with ThreadPoolExecutor(max_workers=8) as workers:
            list(workers.map(lambda _: u.reserve('chat'), range(45)))
            list(workers.map(lambda _: u.reserve('tts', 100), range(35)))
        self.assertEqual(u.counters(), dict(chat_calls=46, tts_calls=36, tts_estimated_characters=3504))
        self.assertEqual(u.usage_status()['limits'], dict.fromkeys(u.COUNTER_KEYS, None))
        self.write_limits(chat=46, tts=None, characters=None)
        with self.assertRaisesRegex(u.UsageError, '^chat_call_limit$'):
            u.reserve('chat')
        u.reserve('tts', 1)
        self.write_limits(chat=None, tts=37, characters=None)
        u.reserve('chat')
        with self.assertRaisesRegex(u.UsageError, '^tts_call_limit$'):
            u.reserve('tts', 1)
        self.write_limits(chat=None, tts=None, characters=3505)
        with self.assertRaisesRegex(u.UsageError, '^tts_character_limit$'):
            u.reserve('tts', 1)

    def test_invalid_config_never_falls_back_or_resets_usage(self):
        u.reserve('chat')
        valid = dict(chat_calls=40, tts_calls=32, tts_estimated_characters=3000)
        cases = ['{', '[]', 'null', '{}', json.dumps(dict(valid, extra=1)),
                 '{"chat_calls":1,"chat_calls":40,"tts_calls":32,"tts_estimated_characters":3000}',
                 ' ' * 4097]
        for key in valid:
            for value in (True, False, -1, 1.5, '40', float('inf'), 2**63):
                cases.append(json.dumps(dict(valid, **{key: value})))
            cases.append(json.dumps({k: v for k, v in valid.items() if k != key}))
        for raw in cases:
            with self.subTest(raw=raw[:100]):
                self.config.write_text(raw, encoding='utf-8')
                for call in (lambda: u.reserve('chat'), lambda: u.reserve('tts', 1), u.usage_status):
                    with self.assertRaisesRegex(u.UsageError, '^usage_config_invalid$'):
                        call()
        self.config.write_bytes(b'\xff')
        with self.assertRaisesRegex(u.UsageError, '^usage_config_invalid$'):
            u.reserve('chat')
        self.config.unlink()
        with self.assertRaisesRegex(u.UsageError, '^usage_config_invalid$'):
            u.reserve('chat')
        self.write_limits()
        self.assertEqual(u.counters(), dict(chat_calls=1, tts_calls=0, tts_estimated_characters=0))

    def test_unreadable_config_and_storage_fail_closed_with_fixed_codes(self):
        with patch.object(Path, 'open', side_effect=PermissionError('private-placeholder')):
            with self.assertRaisesRegex(u.UsageError, '^usage_config_invalid$'):
                u.reserve('chat')
        self.data.mkdir(exist_ok=True)
        database = self.data / 'usage.sqlite3'
        database.write_bytes(b'not a SQLite database; private-placeholder')
        with self.assertRaisesRegex(u.UsageError, '^usage_storage_unavailable$'):
            u.reserve('chat')
        self.assertEqual(database.read_bytes(), b'not a SQLite database; private-placeholder')
        file_path = Path(self.directory) / 'file-instead-of-directory'
        file_path.touch()
        os.environ['COMPANION_DATA_DIR'] = str(file_path)
        with self.assertRaisesRegex(u.UsageError, '^usage_storage_unavailable$'):
            u.reserve('tts', 1)

    def test_storage_commit_failure_prevents_reservation(self):
        u.reserve('chat')
        real_connect = sqlite3.connect

        class CommitFailure:
            def __init__(self, *args, **kwargs):
                self.db = real_connect(*args, **kwargs)

            def execute(self, sql, *args):
                if sql == 'COMMIT':
                    raise sqlite3.OperationalError('private-placeholder')
                return self.db.execute(sql, *args)

            def close(self):
                self.db.close()

        with patch.object(u.sqlite3, 'connect', side_effect=CommitFailure):
            with self.assertRaisesRegex(u.UsageError, '^usage_storage_unavailable$'):
                u.reserve('tts', 2)
        self.assertEqual(u.counters(), dict(chat_calls=1, tts_calls=0, tts_estimated_characters=0))

    def test_invalid_reservations_cannot_decrease_counts(self):
        for kind, estimate in [('other', 0), ('chat', 1), ('tts', -1), ('tts', True),
                               ('tts', 1.5), ('tts', 2**63)]:
            with self.subTest(kind=kind, estimate=estimate):
                with self.assertRaisesRegex(u.UsageError, '^usage_invalid_reservation$'):
                    u.reserve(kind, estimate)
        self.assertEqual(sum(u.counters().values()), 0)

    def test_shanghai_midnight_uses_new_day_and_retains_previous_day(self):
        before = datetime(2026, 10, 19, 15, 59, 59, tzinfo=timezone.utc)
        after = datetime(2026, 10, 19, 16, tzinfo=timezone.utc)
        self.write_limits(chat=1)
        with patch.object(u, '_now', return_value=before):
            u.reserve('chat')
            u.reserve('tts', 4)
            self.assertEqual(u.usage_status()['date'], '2026-10-19')
        with patch.object(u, '_now', return_value=after):
            self.assertEqual(sum(u.counters().values()), 0)
            u.reserve('chat')
            self.assertEqual(u.usage_status()['date'], '2026-10-20')
        with patch.object(u, '_now', return_value=before):
            self.assertEqual(u.counters(), dict(chat_calls=1, tts_calls=1, tts_estimated_characters=4))
            with self.assertRaisesRegex(u.UsageError, '^chat_call_limit$'):
                u.reserve('chat')

    def test_parallel_reservations_enforce_all_three_limits(self):
        self.write_limits(chat=13, tts=11, characters=17)

        def attempt(kind, estimate):
            try:
                u.reserve(kind, estimate)
                return 'reserved'
            except u.UsageError as error:
                return str(error)

        with ThreadPoolExecutor(max_workers=12) as pool:
            chats = list(pool.map(lambda _: attempt('chat', 0), range(60)))
            speech = list(pool.map(lambda _: attempt('tts', 2), range(60)))
        self.assertEqual(chats.count('reserved'), 13)
        self.assertEqual(chats.count('chat_call_limit'), 47)
        self.assertEqual(speech.count('reserved'), 8)
        self.assertEqual(speech.count('tts_character_limit'), 52)
        self.assertEqual(u.counters(), dict(chat_calls=13, tts_calls=8, tts_estimated_characters=16))
        self.write_limits(chat=13, tts=10, characters=1000)
        with ThreadPoolExecutor(max_workers=8) as pool:
            speech = list(pool.map(lambda _: attempt('tts', 2), range(20)))
        self.assertEqual(speech.count('reserved'), 2)
        self.assertEqual(speech.count('tts_call_limit'), 18)
        self.assertEqual(u.counters()['tts_calls'], 10)

    def run_worker(self, source):
        return subprocess.Popen([sys.executable, '-B', '-c', source], cwd=u.ROOT,
                                env=dict(os.environ), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)

    def worker_output(self, worker):
        try:
            output, error = worker.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.communicate()
            self.fail('offline worker timed out')
        self.assertEqual(worker.returncode, 0, error)
        return json.loads(output)

    def test_separate_processes_and_restart_share_persisted_budget(self):
        self.write_limits(chat=7, tts=32, characters=3000)
        # Each process imports fresh, forbids sockets and reserves against the
        # same file. This also covers simultaneous first-time schema creation.
        source = '''
import json, socket
from datetime import datetime, timezone
from server import usage as u
def forbidden(*args, **kwargs):
    raise AssertionError('network forbidden')
socket.socket.connect = socket.getaddrinfo = forbidden
u._now = lambda: datetime(2026, 10, 19, 4, tzinfo=timezone.utc)
results = []
for _ in range(6):
    try:
        u.reserve('chat')
        results.append('reserved')
    except u.UsageError as error:
        results.append(str(error))
print(json.dumps(results))
'''
        workers = [self.run_worker(source) for _ in range(3)]
        results = [result for worker in workers for result in self.worker_output(worker)]
        self.assertEqual(results.count('reserved'), 7)
        self.assertEqual(results.count('chat_call_limit'), 11)
        restarted = self.worker_output(self.run_worker(source))
        self.assertEqual(restarted, ['chat_call_limit'] * 6)
        self.assertEqual(u.counters()['chat_calls'], 7)

    def test_data_directory_environment_isolation(self):
        u.reserve('chat')
        with patch.dict(os.environ, {'COMPANION_DATA_DIR': str(Path(self.directory) / 'other')}):
            self.assertEqual(sum(u.counters().values()), 0)
            u.reserve('tts', 2)
        self.assertEqual(u.counters(), dict(chat_calls=1, tts_calls=0, tts_estimated_characters=0))

    def test_import_creates_no_database_or_config_access(self):
        source = '''
import json, os, pathlib, socket, sqlite3
def forbidden(*args, **kwargs):
    raise AssertionError('import I/O forbidden')
pathlib.Path.open = pathlib.Path.mkdir = forbidden
sqlite3.connect = socket.socket.connect = socket.getaddrinfo = forbidden
from server import usage
print(json.dumps(pathlib.Path(os.environ['COMPANION_DATA_DIR']).exists()))
'''
        self.assertFalse(self.worker_output(self.run_worker(source)))


if __name__ == '__main__':
    unittest.main()

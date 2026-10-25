"""Daily gateway accounting with temporary databases and no model requests."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'desktop-companion-ai'))
from server import usage
from gateway import Denied, create_app
from integration import DailyLedger


class DailyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        limits = self.root / 'limits.json'
        limits.write_text(json.dumps(dict(chat_calls=4, tts_calls=3,
                                         tts_estimated_characters=80)), encoding='utf-8')
        self.environment = patch.dict(os.environ, {
            'COMPANION_DATA_DIR': str(self.root / 'companion'),
            'COMPANION_USAGE_LIMITS_PATH': str(limits)})
        self.environment.start()
        self.path = self.root / 'native.sqlite3'
        self.ledger = DailyLedger(self.path)

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    async def test_shared_foreground_background_duplicate_and_restart(self):
        self.ledger.reserve('one', 'foreground', 'conversation', 'same-body')
        self.ledger.finish('one', 'cancelled')
        self.ledger.reserve('two', 'background', 'summary', 'summary-body')
        with self.assertRaises(Denied) as duplicate:
            self.ledger.reserve('one', 'foreground', 'conversation', 'other-body')
        self.assertEqual(duplicate.exception.code, 'DUPLICATE_ATTEMPT')
        reopened = DailyLedger(self.path)
        # Same text on a new turn remains usable; failed/cancelled attempts persist.
        reopened.reserve('three', 'foreground', 'conversation', 'same-body')
        usage.reserve('tts', 7)
        state = reopened.snapshot()
        self.assertEqual(len(state['attempts']), 3)
        self.assertEqual(state['policy']['sharedDailyBudget']['reserved'],
                         dict(chat_calls=3, tts_calls=1, tts_estimated_characters=7))
        reopened.reserve('four', 'background', 'correction', 'last-body')
        with self.assertRaises(Denied) as limit:
            reopened.reserve('five', 'foreground', 'conversation', 'blocked')
        self.assertEqual(limit.exception.status, 429)
        self.assertEqual(usage.counters()['chat_calls'], 4)

    async def test_unlimited_native_requests_continue_past_previous_daily_limit(self):
        Path(os.environ['COMPANION_USAGE_LIMITS_PATH']).write_text(json.dumps(
            dict(chat_calls=None, tts_calls=None, tts_estimated_characters=None)), encoding='utf-8')
        for index in range(45):
            category, task = ('foreground', 'conversation') if index % 2 else ('background', 'summary')
            self.ledger.reserve(str(index), category, task, str(index))
        reopened = DailyLedger(self.path)
        self.assertEqual(len(reopened.snapshot()['attempts']), 45)
        self.assertEqual(usage.counters()['chat_calls'], 45)
        app = create_app(reopened, 'fake-unused-key', app_id='NEKO-COMPANION-GATEWAY')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://127.0.0.1') as client:
            response = await client.get('/health')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['policy']['sharedDailyBudget']['limits'],
                             dict.fromkeys(usage.COUNTER_KEYS, None))

    async def test_racing_native_ledgers_share_one_daily_limit(self):
        other = DailyLedger(self.root / 'other.sqlite3')
        def reserve(i):
            try:
                (self.ledger if i % 2 else other).reserve(str(i), 'background', 'summary', str(i))
                return True
            except Denied:
                return False
        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(reserve, range(16)))
        self.assertEqual(sum(results), 4)
        self.assertEqual(usage.counters()['chat_calls'], 4)

    async def test_health_reports_daily_policy_and_invalid_config_fails_closed(self):
        app = create_app(self.ledger, 'fake-unused-key', app_id='NEKO-COMPANION-GATEWAY')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://127.0.0.1') as client:
            health = (await client.get('/health')).json()
            self.assertEqual(health['app'], 'NEKO-COMPANION-GATEWAY')
            self.assertTrue(health['policy']['allNativeTasksCountAsChat'])
            self.assertEqual(health['policy']['sharedDailyBudget']['limits']['chat_calls'], 4)
            Path(os.environ['COMPANION_USAGE_LIMITS_PATH']).write_text('{}')
            self.assertEqual((await client.get('/health')).status_code, 503)
        with self.assertRaises(Denied) as failure:
            self.ledger.reserve('invalid', 'foreground', 'conversation', 'hash')
        self.assertEqual(failure.exception.status, 503)
        with self.ledger.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)

"""Offline tests only: fake connector and fake key; real network is forbidden."""
import asyncio
import base64
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid
import wave

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('audition_qwen', PROJECT / 'tools/audition_qwen.py')
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)
FAKE_KEY = 'offline-test-placeholder'
PCM = b'\x00\x00\xff\x7f\x00\x80' * 80


def normal_events(pcm=PCM):
    return [
        {'type': 'session.created'},
        {'type': 'session.updated', 'session': dict(q.PARAMS, model=q.MODEL)},
        {'type': 'input_text_buffer.committed'},
        {'type': 'response.created', 'response': {'id': 'resp_test'}},
        # Split a 16-bit sample across messages: only aggregate alignment matters.
        {'type': 'response.audio.delta', 'response_id': 'resp_test',
         'delta': base64.b64encode(pcm[:1]).decode()},
        {'type': 'response.audio.delta', 'response_id': 'resp_test',
         'delta': base64.b64encode(pcm[1:]).decode()},
        {'type': 'response.audio.done', 'response_id': 'resp_test'},
        {'type': 'response.done', 'response': {'id': 'resp_test', 'status': 'completed',
                                             'usage': {'characters': 44}}},
        {'type': 'session.finished'},
    ]


class FakeConnector:
    def __init__(self, events=None, fail_connect=False):
        self.events = list(normal_events() if events is None else events)
        self.sent = []
        self.calls = 0
        self.fail_connect = fail_connect
        self.closed = False

    def __call__(self, url, **kwargs):
        self.calls += 1
        assert url == q.URL
        assert kwargs['additional_headers'] == {'Authorization': 'Bearer ' + FAKE_KEY}
        assert kwargs['proxy'] is None
        assert kwargs['max_size'] == q.MAX_MESSAGE
        return self

    async def __aenter__(self):
        if self.fail_connect:
            raise OSError('untrusted exception: ' + FAKE_KEY)
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if not self.events:
            await asyncio.Future()
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return json.dumps(event)


class AuditionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'evidence').mkdir()
        shutil.copyfile(PROJECT / 'evidence/audition-texts.json',
                        self.root / 'evidence/audition-texts.json')
        self.proof = self.root / 'manual-free.json'
        self.obj = dict(schema_version=1, model=q.MODEL, region='cn-beijing',
                        confirmed_by_user=True, stop_when_free_exhausted=True,
                        paid_usage_authorized=False, max_synthesis_requests=1,
                        remaining_billable_characters=438,
                        checked_at=q.utcnow().isoformat(),
                        expires_at=(q.utcnow() + timedelta(hours=2)).isoformat(),
                        confirmation_id=str(uuid.uuid4()))
        self.write_proof()
        self.env = patch.dict(q.os.environ, {'DASHSCOPE_API_KEY': FAKE_KEY}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.network = patch.object(socket, 'create_connection', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.connect_guard = patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden'))
        self.connect_guard.start()
        self.addCleanup(self.connect_guard.stop)

    def write_proof(self):
        self.proof.write_text(json.dumps(self.obj), encoding='utf-8')

    async def execute(self, fake):
        return await q.run(execute=True, confirmation_file=self.proof, root=self.root,
                           connector=fake, source_kind='mock')

    async def test_default_dry_run_never_reads_environment_or_connects(self):
        fake = FakeConnector()
        with patch.object(q.os.environ, 'get', side_effect=AssertionError('credential read')):
            result = await q.run(root=self.root, connector=fake)
        self.assertEqual(result['status'], 'not_run')
        self.assertEqual(result['source_kind'], 'audition_plan')
        self.assertEqual(result['estimated_usage']['billable_characters'], 44)
        self.assertEqual(fake.calls, 0)
        self.assertFalse((self.root / 'data').exists())

    async def test_user_confirmed_free_only_probe_keeps_quota_unknown(self):
        self.obj.update(authorization_basis="user_confirmed_provider_free_only_stop",
                        remaining_billable_characters=None, expires_at=None)
        self.write_proof()
        fake = FakeConnector()
        result = await self.execute(fake)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(fake.calls, 1)
        self.obj["stop_when_free_exhausted"] = False
        self.write_proof()
        with self.assertRaisesRegex(q.Rejected, "free_confirmation_invalid"):
            q.confirmation(self.proof, q.utcnow(), 44)

    async def test_server_normalizes_chinese_language_case(self):
        events = normal_events()
        events[1]["session"]["language_type"] = "chinese"
        result = await self.execute(FakeConnector(events))
        self.assertEqual(result["status"], "ready")

    async def test_no_key_blocks_before_network(self):
        fake = FakeConnector()
        with patch.dict(q.os.environ, {}, clear=True):
            with self.assertRaisesRegex(q.Rejected, 'DASHSCOPE_API_KEY_required'):
                await self.execute(fake)
        self.assertEqual(fake.calls, 0)
        self.assertFalse((self.root / 'data').exists())

    async def test_no_confirmation_blocks_before_key_read_or_network(self):
        fake = FakeConnector()
        with patch.object(q.os.environ, 'get', side_effect=AssertionError('credential read')):
            with self.assertRaisesRegex(q.Rejected, 'free_confirmation_required'):
                await q.run(execute=True, root=self.root, connector=fake)
        self.assertEqual(fake.calls, 0)

    async def test_confirmation_fail_closed(self):
        cases = [dict(model='another-model'), dict(region='singapore'),
                 dict(remaining_billable_characters=43), dict(remaining_billable_characters=True),
                 dict(stop_when_free_exhausted=False), dict(confirmed_by_user=False),
                 dict(paid_usage_authorized=True), dict(max_synthesis_requests=2),
                 dict(expires_at=(q.utcnow() - timedelta(seconds=1)).isoformat()),
                 dict(checked_at=(q.utcnow() - timedelta(days=2)).isoformat()),
                 dict(checked_at=(q.utcnow() + timedelta(hours=1)).isoformat()),
                 dict(expires_at='2030-01-01'), dict(confirmation_id='../escape')]
        original = self.obj.copy()
        for changes in cases:
            with self.subTest(changes=changes):
                self.obj = dict(original, **changes)
                self.write_proof()
                fake = FakeConnector()
                with self.assertRaises(q.Rejected):
                    await self.execute(fake)
                self.assertEqual(fake.calls, 0)

    async def test_audio_protocol_wav_manifest_and_single_use(self):
        fake = FakeConnector()
        result = await self.execute(fake)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['source_kind'], 'mock')
        self.assertTrue(fake.closed)
        self.assertEqual(fake.calls, 1)
        self.assertEqual([e['type'] for e in fake.sent],
                         ['session.update', 'input_text_buffer.append',
                          'input_text_buffer.commit', 'session.finish'])
        self.assertEqual(fake.sent[1]['text'], q.TEXT)
        self.assertEqual(len({e['event_id'] for e in fake.sent}), 4)
        wav_path = self.root / 'data/auditions' / result['audio']['file']
        with wave.open(str(wav_path)) as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, 24000))
            self.assertEqual(wav.readframes(wav.getnframes()), PCM)
        self.assertEqual(q.sha(wav_path.read_bytes()), result['audio']['sha256'])
        self.assertEqual(result['reported_usage']['characters'], 44)
        self.assertEqual(result['synthesis_requests_reserved'], 1)
        self.assertFalse((self.root / 'data/auditions/index.json').exists())
        for path in (self.root / 'data').rglob('*.json'):
            raw = path.read_text(encoding='utf-8')
            self.assertNotIn(FAKE_KEY, raw)
            self.assertNotIn('Authorization', raw)
        with self.assertRaisesRegex(q.Rejected, 'already_reserved'):
            await self.execute(fake)
        self.assertEqual(fake.calls, 1)

    async def test_error_and_timeouts_do_not_retry_or_leak(self):
        for mode in ('upstream', 'idle', 'total', 'connect'):
            with self.subTest(mode=mode):
                self.obj['confirmation_id'] = str(uuid.uuid4())
                self.write_proof()
                events = normal_events()[:4]
                if mode == 'upstream':
                    events.append({'type': 'error', 'error': {'message': FAKE_KEY}})
                fake = FakeConnector(events, fail_connect=mode == 'connect')
                with patch.object(q, 'IDLE_TIMEOUT', 0.01 if mode == 'idle' else 1), patch.object(q, 'TOTAL_TIMEOUT', 0.01 if mode == 'total' else 1):
                    result = await self.execute(fake)
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(fake.calls, 1)
                self.assertEqual(result['retry_count'], 0)
                self.assertEqual(sum(e['type'] == 'input_text_buffer.commit' for e in fake.sent),
                                 0 if mode == 'connect' else 1)
                self.assertNotIn(FAKE_KEY, json.dumps(result))
                self.assertNotIn('audio', result)
                self.assertEqual(result['possibly_billable'], mode != 'connect')
                with self.assertRaises(q.Rejected):
                    await self.execute(fake)

    async def test_bad_audio_finish_and_limits(self):
        bad_base64 = normal_events()
        bad_base64[4]['delta'] = 'not base64!'
        wrong_model = normal_events()
        wrong_model[1]['session']['model'] = 'cosyvoice'
        failed_response = normal_events()
        failed_response[-2]['response']['status'] = 'failed'
        late_audio = normal_events()
        late_audio.insert(-2, normal_events()[4])
        event_flood = normal_events()[:4] + [{'type': 'input_text_buffer.committed'}] * 520
        oversized = normal_events()[:4] + [{'type': 'input_text_buffer.committed', 'padding': 'x' * q.MAX_MESSAGE}]
        cases = [normal_events(b'\x00'), normal_events(b''), bad_base64, wrong_model,
                 failed_response, late_audio, event_flood, oversized,
                 normal_events()[:4] + [{'type': 'session.finished'}],
                 normal_events()[:4] + [normal_events()[3]]]
        for events in cases:
            self.obj['confirmation_id'] = str(uuid.uuid4())
            self.write_proof()
            fake = FakeConnector(events)
            result = await self.execute(fake)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(fake.calls, 1)
        self.obj['confirmation_id'] = str(uuid.uuid4())
        self.write_proof()
        with patch.object(q, 'MAX_PCM_BYTES', 2):
            result = await self.execute(FakeConnector())
        self.assertEqual(result['error_code'], 'audio_duration_limit')

    async def test_changed_text_or_budget_blocks_network(self):
        path = self.root / 'evidence/audition-texts.json'
        original = json.loads(path.read_text(encoding='utf-8'))
        for mode in ('text', 'budget', 'segments'):
            data = json.loads(json.dumps(original))
            if mode == 'text':
                data['texts'][0]['text'] += '改'
            elif mode == 'budget':
                data['texts'][1]['text'] = '字' * 300
                data['texts'][1]['qwen_minimax_billable_estimate'] = 600
            else:
                data['texts'].append(data['texts'][0])
            path.write_text(json.dumps(data), encoding='utf-8')
            fake = FakeConnector()
            with self.assertRaises(q.Rejected):
                await self.execute(fake)
            self.assertEqual(fake.calls, 0)

    async def test_private_symlink_rejected(self):
        target = self.root / 'static'
        target.mkdir()
        try:
            (self.root / 'data').symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest('Windows symlink creation permission unavailable')
        fake = FakeConnector()
        with self.assertRaisesRegex(q.Rejected, 'private_path_redirected'):
            await self.execute(fake)
        self.assertEqual(fake.calls, 0)


class DirectPathTests(unittest.TestCase):
    def test_absolute_path_cli_from_unrelated_directory_without_key(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-B', str(PROJECT / 'tools/audition_qwen.py')],
                                    cwd=directory, env={'SYSTEMROOT': q.os.environ.get('SYSTEMROOT', '')},
                                    capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(result.stdout)
        self.assertEqual(manifest['status'], 'not_run')
        self.assertEqual(manifest['text_id'], 'T01')
        self.assertEqual(manifest['synthesis_requests_reserved'], 0)


if __name__ == '__main__':
    unittest.main()

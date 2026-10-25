"""Offline cloud contract tests. All sockets are forbidden, credentials are fake."""
import asyncio
import base64
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
import wave
from unittest.mock import patch

import httpx
from server import cloud as c

KEY = 'offline-placeholder'
MESSAGES = [{'role': 'user', 'content': '今天有些累。'}]
REAL_HTTP = httpx.AsyncClient


def frame(text):
    return 'data: ' + json.dumps({'choices': [{'index': 0, 'delta': {'content': text}}]}) + '\n\n'


class Stream(httpx.AsyncByteStream):
    def __init__(self, data=b'', block=False):
        self.data, self.block, self.closed = data, block, False
        self.started = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        if self.block:
            await asyncio.Future()
        yield self.data

    async def aclose(self):
        self.closed = True


def events():
    return [
        {'type': 'session.created'},
        {'type': 'session.updated', 'session': dict(c.TTS_PARAMS, model=c.TTS_MODEL, language_type='chinese')},
        {'type': 'response.created', 'response': {'id': 'r'}},
        {'type': 'response.audio.delta', 'response_id': 'r', 'delta': base64.b64encode(b'\0\0' * 240).decode()},
        {'type': 'response.audio.done', 'response_id': 'r'},
        {'type': 'response.done', 'response': {'id': 'r', 'status': 'completed', 'usage': {'characters': 4, 'secret': KEY}}},
        {'type': 'session.finished'},
    ]


class WS:
    def __init__(self, rows=None):
        self.rows = events() if rows is None else rows
        self.sent, self.closed, self.calls = [], False, 0
        self.started = asyncio.Event()

    def __call__(self, url, **kwargs):
        self.calls += 1
        assert url == c.TTS_URL
        assert kwargs['proxy'] is None
        assert kwargs['logger'] is c._logger
        assert not kwargs['logger'].propagate
        assert kwargs['additional_headers'] == {'Authorization': 'Bearer ' + KEY}
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        self.started.set()
        if not self.rows:
            await asyncio.Future()
        row = self.rows.pop(0)
        if isinstance(row, Exception):
            raise row
        return json.dumps(row)


class CloudTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.limits_path = Path(directory) / 'usage-limits.json'
        self.limits_path.write_text(json.dumps(dict(chat_calls=40, tts_calls=32,
                                                   tts_estimated_characters=3000)), encoding='utf-8')
        self.enterContext(patch.dict('os.environ', {
            'DASHSCOPE_API_KEY': KEY, 'COMPANION_FREE_ONLY_CONFIRMED': '1',
            'COMPANION_DATA_DIR': str(Path(directory) / 'data'),
            'COMPANION_USAGE_LIMITS_PATH': str(self.limits_path),
        }, clear=True))
        self.enterContext(patch.object(c, '_default_client', None))
        self.enterContext(patch.object(c, '_blocked_reason', None))
        self.enterContext(patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')))
        self.enterContext(patch.object(socket, 'getaddrinfo', side_effect=AssertionError('DNS forbidden')))
        self.client = c.CloudClient(enabled=True)
        self.requests = []
        # Production constructs a TLS transport before our client factory runs.
        # Keep that constructor offline too (and independent of Windows CA state).
        self.enterContext(patch.object(c.httpx, 'AsyncHTTPTransport', return_value=None))

    def http(self, data=None, status=200, stream=None, error=None):
        stream = stream or Stream((frame('我在听。') + 'data: [DONE]\n\n').encode() if data is None else data)

        async def handler(request):
            self.requests.append(request)
            if error:
                raise error
            return httpx.Response(status, headers={'content-type': 'text/event-stream'}, stream=stream)

        def factory(**kwargs):
            self.assertFalse(kwargs['trust_env'])
            self.assertFalse(kwargs['follow_redirects'])
            # Avoid leaking the unused production transport created by the adapter.
            kwargs['transport'] = httpx.MockTransport(handler)
            return REAL_HTTP(**kwargs)
        self.enterContext(patch.object(c.httpx, 'AsyncClient', side_effect=factory))
        return stream

    async def collect(self, **kwargs):
        return ''.join([d async for d in self.client.stream_reply(MESSAGES, **kwargs)])

    async def test_disabled_and_free_gate(self):
        self.assertIsNone(c.create_cloud_client())
        with self.assertRaisesRegex(c.CloudError, '^cloud_disabled$'):
            c.CloudClient()
        with self.assertRaisesRegex(c.CloudError, '^cloud_disabled$'):
            await c.synthesize('你好')
        with self.assertRaisesRegex(c.CloudError, '^cloud_disabled$'):
            _ = [d async for d in c.stream_reply(MESSAGES)]
        with patch.dict('os.environ', {'COMPANION_FREE_ONLY_CONFIRMED': '0'}):
            with self.assertRaisesRegex(c.CloudError, '^free_confirmation_required$'):
                c.CloudClient(enabled=True)
            with self.assertRaises(c.CloudError):
                await self.collect()
        self.assertEqual(c.counters()['chat_calls'], 0)

    async def test_chat_context_and_bounds(self):
        stream = self.http((frame('好' * 300) + 'data: [DONE]\n\n').encode())
        self.assertEqual(await self.collect(), '好' * 240)
        self.assertTrue(stream.closed)
        body = json.loads(self.requests[0].content)
        self.assertEqual(body['model'], 'qwen-flash')
        self.assertEqual(body['max_tokens'], 180)
        self.assertEqual(body['messages'][1:], MESSAGES)
        self.assertTrue(body['stream'])
        self.assertEqual(str(self.requests[0].url), c.CHAT_URL)
        self.assertEqual(self.client.counters()['chat_calls'], 1)

    async def test_invalid_inputs_do_not_reserve(self):
        for value in (0, 181, True, '180'):
            with self.assertRaisesRegex(c.CloudError, '^invalid_max_tokens$'):
                await self.collect(max_tokens=value)
        for messages in ([], [{'role': 'tool', 'content': 'x'}], [{'role': 'user', 'content': 1}]):
            with self.assertRaises(c.CloudError):
                _ = [d async for d in self.client.stream_reply(messages)]
        for text in ('', 'x' * 241, None):
            with self.assertRaises(c.CloudError):
                await self.client.synthesize(text)
        self.assertEqual(sum(c.counters().values()), 0)

    async def test_missing_key(self):
        with patch.dict('os.environ', {'DASHSCOPE_API_KEY': ''}):
            with self.assertRaisesRegex(c.CloudError, '^credential_required_or_invalid$'):
                await self.collect()
        self.assertEqual(c.counters()['chat_calls'], 0)

    async def test_chat_shared_limit_no_retry(self):
        self.http(error=OSError(KEY))
        for _ in range(40):
            with self.assertRaisesRegex(c.CloudError, '^chat_transport_or_protocol_error$'):
                await self.collect()
        other = c.CloudClient(enabled=True)
        with self.assertRaisesRegex(c.CloudError, '^chat_call_limit$'):
            _ = [d async for d in other.stream_reply(MESSAGES)]
        self.assertEqual(len(self.requests), 40)
        self.assertEqual(c.counters()['chat_calls'], 40)

    async def test_usage_status_does_not_require_key_or_cloud_opt_in(self):
        with patch.dict('os.environ', {'DASHSCOPE_API_KEY': '', 'COMPANION_FREE_ONLY_CONFIRMED': '0'}):
            status = c.usage_status()
            self.assertEqual(status, self.client.usage_status())
            self.assertEqual(status['reserved'], c.counters())
            self.assertEqual(status['timezone'], 'Asia/Shanghai')
            self.assertEqual(status['limits']['chat_calls'], 40)
            self.assertFalse(status['freeBalanceVerified'])
            self.assertFalse(status['finalBillKnown'])

    async def test_invalid_config_and_storage_stop_before_transport(self):
        self.http()
        ws = WS()
        self.enterContext(patch.object(c, 'connect', ws))
        self.limits_path.write_text('{private-placeholder', encoding='utf-8')
        for call in (self.collect, lambda: self.client.synthesize('你好')):
            with self.assertRaisesRegex(c.CloudError, '^usage_config_invalid$'):
                await call()
        with self.assertRaisesRegex(c.CloudError, '^usage_config_invalid$'):
            c.usage_status()
        self.limits_path.write_text(json.dumps(dict(chat_calls=40, tts_calls=32,
                                                   tts_estimated_characters=3000)), encoding='utf-8')
        with patch.object(c._usage.sqlite3, 'connect', side_effect=c._usage.sqlite3.OperationalError(KEY)):
            for call in (self.collect, lambda: self.client.synthesize('你好')):
                with self.assertRaisesRegex(c.CloudError, '^usage_storage_unavailable$'):
                    await call()
        self.assertEqual(self.requests, [])
        self.assertEqual(ws.calls, 0)
        self.assertEqual(sum(c.counters().values()), 0)

    async def test_chat_http_and_malformed_failures(self):
        for status, data, code in [(403, KEY.encode(), 'chat_upstream_error'),
                                   (200, b'data: invalid\n\n', 'chat_transport_or_protocol_error'),
                                   (200, frame('x').encode(), 'chat_incomplete'),
                                   (200, b'x' * 65537, 'chat_event_limit')]:
            with self.subTest(code=code):
                stream = self.http(data, status)
                with self.assertRaises(c.CloudError) as caught:
                    await self.collect()
                self.assertEqual(str(caught.exception), code)
                self.assertNotIn(KEY, str(caught.exception))
                self.assertTrue(stream.closed)

    async def test_chat_cancel_closes(self):
        stream = self.http(stream=Stream(block=True))
        task = asyncio.create_task(self.collect())
        await stream.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stream.closed)
        self.assertEqual(c.counters()['chat_calls'], 1)

    async def test_chat_timeout_closes(self):
        stream = self.http(stream=Stream(block=True))
        with patch.object(c, 'TOTAL_TIMEOUT', .02):
            with self.assertRaisesRegex(c.CloudError, '^chat_timeout$'):
                await self.collect()
        self.assertTrue(stream.closed)
        self.assertEqual(c.counters()['chat_calls'], 1)

    async def test_module_stream_aclose(self):
        stream = self.http()
        c.configure(enabled=True)
        generator = c.stream_reply(MESSAGES)
        self.assertEqual(await anext(generator), '我在听。')
        await generator.aclose()
        self.assertTrue(stream.closed)
        self.assertEqual(c.counters()['chat_calls'], 1)

    async def test_tts_wav_params_usage(self):
        ws = WS()
        self.enterContext(patch.object(c, 'connect', ws))
        c.configure(enabled=True)
        result = await c.synthesize('你好')
        self.assertTrue(ws.closed)
        self.assertEqual(ws.sent[0]['session'], c.TTS_PARAMS)
        self.assertEqual([e['type'] for e in ws.sent], ['session.update', 'input_text_buffer.append', 'input_text_buffer.commit', 'session.finish'])
        self.assertEqual(result['durationMs'], 10)
        self.assertEqual(result['mime'], 'audio/wav')
        with wave.open(io.BytesIO(base64.b64decode(result['audio']))) as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()), (1, 2, 24000, 240))
        self.assertEqual(result['usage']['estimatedCharacters'], 4)
        self.assertEqual(result['usage']['reportedCharacters'], 4)
        self.assertNotIn(KEY, json.dumps(result))
        snapshot = self.client.counters()
        snapshot['tts_calls'] = -1
        self.assertEqual(c.counters()['tts_calls'], 1)

    async def test_tts_session_parameters_fail_before_text(self):
        for key in ('model', 'voice', 'speech_rate', 'volume', 'sample_rate', 'response_format'):
            rows = events()
            rows[1]['session'][key] = 'wrong'
            ws = WS(rows)
            with patch.object(c, 'connect', ws):
                with self.assertRaisesRegex(c.CloudError, '^tts_session_mismatch$'):
                    await self.client.synthesize('你好')
            self.assertTrue(ws.closed)
            self.assertEqual(len(ws.sent), 1)

    async def test_tts_limits_shared_and_failures_reserved(self):
        ws = WS([OSError(KEY)] * 32)
        with patch.object(c, 'connect', ws):
            for _ in range(32):
                with self.assertRaisesRegex(c.CloudError, '^tts_transport_or_protocol_error$'):
                    await self.client.synthesize('a')
            with self.assertRaisesRegex(c.CloudError, '^tts_call_limit$'):
                await c.CloudClient(enabled=True).synthesize('a')
        self.assertEqual(ws.calls, 32)
        self.assertEqual(c.counters()['tts_estimated_characters'], 32)

    async def test_tts_character_boundary(self):
        ws = WS([OSError(KEY)] * 7)
        with patch.object(c, 'connect', ws):
            for text in ['好' * 240] * 6 + ['好' * 60]:
                with self.assertRaises(c.CloudError):
                    await self.client.synthesize(text)
            self.assertEqual(c.counters()['tts_estimated_characters'], 3000)
            with self.assertRaisesRegex(c.CloudError, '^tts_character_limit$'):
                await self.client.synthesize('a')
        self.assertEqual(ws.calls, 7)

    async def test_tts_cancel_and_timeout(self):
        for cancel in (True, False):
            ws = WS([])
            with patch.object(c, 'connect', ws), patch.object(c, 'TOTAL_TIMEOUT', .02):
                task = asyncio.create_task(self.client.synthesize('你好'))
                await ws.started.wait()
                if cancel:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    with self.assertRaisesRegex(c.CloudError, '^tts_timeout$'):
                        await task
            self.assertTrue(ws.closed)
        self.assertEqual(c.counters()['tts_calls'], 2)

    async def test_tts_protocol_failures(self):
        variants = []
        rows = events(); rows[3]['response_id'] = 'other'; variants.append(rows)
        rows = events(); rows[3]['delta'] = '!'; variants.append(rows)
        rows = events(); rows[5]['response']['status'] = 'failed'; variants.append(rows)
        variants.append([{'type': 'error', 'message': KEY}])
        rows = events(); rows[3]['delta'] = base64.b64encode(b'x').decode(); variants.append(rows)
        for rows in variants:
            ws = WS(rows)
            with patch.object(c, 'connect', ws):
                with self.assertRaises(c.CloudError) as caught:
                    await self.client.synthesize('你好')
            self.assertTrue(ws.closed)
            self.assertNotIn(KEY, str(caught.exception))

    async def test_free_exhaustion_latches_all_calls(self):
        ws = WS([{'type': 'error', 'error': {'code': 'AllocationQuota.FreeTierOnly', 'message': KEY}}])
        with patch.object(c, 'connect', ws):
            with self.assertRaisesRegex(c.CloudError, '^free_tier_exhausted$'):
                await self.client.synthesize('你好')
            with self.assertRaisesRegex(c.CloudError, '^free_tier_exhausted$'):
                await self.collect()
        self.assertEqual(c.counters()['chat_calls'], 0)
        self.assertEqual(ws.calls, 1)

    async def test_http_error_whitelist(self):
        self.http(json.dumps({'code': 'AllocationQuota.FreeTierOnly', 'message': KEY}).encode(), 403)
        with self.assertRaisesRegex(c.CloudError, '^free_tier_exhausted$'):
            await self.collect()
        self.assertEqual(c._blocked_reason, 'free_tier_exhausted')
        self.assertEqual(str(c._upstream_error({'code': KEY}, 'chat_upstream_error')), 'chat_upstream_error')

    async def test_arrearage_preserves_reason_and_blocks_both_adapters(self):
        self.http(json.dumps({'code': 'Arrearage', 'message': KEY}).encode(), 400)
        with self.assertRaisesRegex(c.CloudError, '^account_arrearage$'):
            await self.collect()
        before = c.counters()
        for call in (self.collect, lambda: self.client.synthesize('你好')):
            with self.assertRaisesRegex(c.CloudError, '^account_arrearage$'):
                await call()
        self.assertEqual(c.counters(), before)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(c._blocked_reason, 'account_arrearage')

    async def test_plan_download_normalizes_streaming_wav_and_never_forwards_key(self):
        raw = io.BytesIO()
        with wave.open(raw,'wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
            w.writeframes(b'\0\0'*240)
        audio = bytearray(raw.getvalue())
        audio[40:44] = (2147483546).to_bytes(4,'little')
        requests = []
        async def handler(request):
            requests.append(request)
            if request.method == 'POST':
                return httpx.Response(200,json={'output':{'audio':{'data':'','url':'http://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/test.wav'}},'usage':{'characters':4}})
            return httpx.Response(200,content=bytes(audio))
        def factory(**kwargs):
            return REAL_HTTP(transport=httpx.MockTransport(handler),**kwargs)
        with patch.dict('os.environ',{'COMPANION_PROVIDER':'token-plan'}), patch.object(c.httpx,'AsyncClient',side_effect=factory):
            result = await c.CloudClient(enabled=True).synthesize('你好')
        self.assertEqual(result['durationMs'],10)
        self.assertEqual(requests[1].url.scheme,'https')
        self.assertNotIn('authorization',requests[1].headers)
        with wave.open(io.BytesIO(base64.b64decode(result['audio']))) as w:
            self.assertEqual(w.getnframes(),240)

    async def test_plan_audio_url_rejects_untrusted_host(self):
        async def handler(request):
            return httpx.Response(200,json={'output':{'audio':{'data':'','url':'https://untrusted.example/audio.wav'}}})
        def factory(**kwargs):
            return REAL_HTTP(transport=httpx.MockTransport(handler),**kwargs)
        with patch.dict('os.environ',{'COMPANION_PROVIDER':'token-plan'}), patch.object(c.httpx,'AsyncClient',side_effect=factory):
            with self.assertRaisesRegex(c.CloudError,'^tts_invalid_audio_url$'):
                await c.CloudClient(enabled=True).synthesize('你好')


if __name__ == '__main__':
    unittest.main()

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest

import httpx
from gateway import Denied, Ledger, LOCAL_TOKEN, MODEL, POLICY, create_app


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.path = Path(self.temp.name) / 'ledger.sqlite3'
        self.ledger = Ledger(self.path)
        self.sent = []
        self.handler = self.ok
        async def dispatch(request):
            self.sent.append(request)
            return await self.handler(request)
        self.transport = httpx.MockTransport(dispatch)
        self.app = create_app(self.ledger, 'test-secret-must-not-leak', self.transport, .15)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                      base_url='http://127.0.0.1',
                                      headers={'Authorization': 'Bearer ' + LOCAL_TOKEN})

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def ok(self, request):
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': '好'}}],
                                       'usage': {'prompt_tokens': 11, 'completion_tokens': 2, 'total_tokens': 13}})

    async def call(self, text='测试', task='conversation', **extra):
        body = dict(model=MODEL, messages=[{'role': 'system', 'content': '原生人设'},
                                          {'role': 'user', 'content': text}], **extra)
        return await self.client.post('/' + task + '/v1/chat/completions', json=body)

    async def test_preserves_prompt_tools_clamps_and_records_usage(self):
        tools = [{'type': 'function', 'function': {'name': 'lookup', 'parameters': {'type': 'object'}}}]
        r = await self.call(tools=tools, max_completion_tokens=9999, enable_thinking=True)
        self.assertEqual(r.status_code, 200)
        wire = json.loads(self.sent[0].content)
        self.assertEqual(wire['messages'][0], {'role': 'system', 'content': '原生人设'})
        self.assertEqual(wire['tools'], tools)
        self.assertEqual(wire['max_tokens'], 3000)
        self.assertFalse(wire['enable_thinking'])
        self.assertNotIn('max_completion_tokens', wire)
        self.assertEqual(json.loads(self.ledger.snapshot()['attempts'][0]['usage'])['total_tokens'], 13)

    async def test_duplicates_and_restart_do_not_refund(self):
        async def reject(request):
            return httpx.Response(500, text='test-secret-must-not-leak')
        self.handler = reject
        self.assertEqual((await self.call()).status_code, 502)
        other = Ledger(self.path)
        self.assertEqual(len(other.snapshot()['attempts']), 1)
        self.assertEqual((await self.call()).status_code, 409)
        self.assertEqual(len(self.sent), 1)

    async def test_same_request_id_different_body_denied(self):
        headers = {'x-neko-request-id': 'stable-turn-1'}
        body = {'model': MODEL, 'messages': [{'role': 'user', 'content': '一'}]}
        self.assertEqual((await self.client.post('/conversation/v1/chat/completions', json=body, headers=headers)).status_code, 200)
        body['messages'][0]['content'] = '二'
        self.assertEqual((await self.client.post('/conversation/v1/chat/completions', json=body, headers=headers)).status_code, 409)
        self.assertEqual(len(self.sent), 1)

    async def test_persistent_category_limits_under_race(self):
        def reserve(i):
            try:
                self.ledger.reserve(str(i), 'foreground', 'conversation', str(i))
                return True
            except Denied:
                return False
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(reserve, range(50)))
        self.assertEqual(sum(results), 12)
        reopened = Ledger(self.path)
        with self.assertRaises(Denied):
            reopened.reserve('next', 'foreground', 'conversation', 'next')
        for i in range(20):
            reopened.reserve('bg' + str(i), 'background', 'summary', 'bg' + str(i))
        self.assertEqual(len(reopened.snapshot()['attempts']), 32)
        self.assertEqual((await self.call(task='summary')).status_code, 429)
        self.assertFalse(self.sent)

    async def test_timeout_cancel_and_busy_keep_reservation(self):
        entered = asyncio.Event()
        async def hanging(request):
            entered.set()
            await asyncio.Event().wait()
        self.handler = hanging
        pending = asyncio.create_task(self.call('slow'))
        await entered.wait()
        self.assertEqual((await self.call('busy')).status_code, 409)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(self.ledger.snapshot()['attempts'][0]['status'], 'cancelled')
        result = await self.call('timeout')
        self.assertEqual(result.status_code, 504)
        self.assertEqual(self.ledger.snapshot()['attempts'][1]['status'], 'timeout')
        self.assertEqual(len(self.sent), 2)

    async def test_disallowed_calls_and_failclosed_storage(self):
        self.assertEqual((await self.call(task='tts')).status_code, 403)
        r = await self.client.post('/conversation/v1/chat/completions', json={
            'model': MODEL, 'messages': [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'x'}}]}]})
        self.assertEqual(r.status_code, 403)
        self.assertEqual((await self.call('字' * 21000)).status_code, 413)
        self.assertFalse(self.sent)
        self.ledger.path = Path(self.temp.name) / 'missing' / 'db'
        self.assertEqual((await self.call()).status_code, 503)
        self.assertFalse(self.sent)

    async def test_stream_usage_and_incomplete_stream(self):
        async def stream(request):
            return httpx.Response(200, content=b'data: {"choices": [], "usage": {"total_tokens": 9}}\n\ndata: [DONE]\n\n',
                                  headers={'Content-Type': 'text/event-stream'})
        self.handler = stream
        r = await self.call(stream=True)
        self.assertIn('[DONE]', r.text)
        self.assertEqual(self.ledger.snapshot()['attempts'][0]['status'], 'ok')
        async def broken(request):
            return httpx.Response(200, content=b'data: {"choices": []}\n\n')
        self.handler = broken
        r = await self.call('broken', stream=True)
        self.assertIn('INCOMPLETE_STREAM', r.text)
        self.assertNotIn('[DONE]', r.text)
        self.assertNotIn('test-secret', r.text)
        self.assertEqual(self.ledger.snapshot()['attempts'][1]['status'], 'failed')

    async def test_stream_cancel_releases_lock_without_refund(self):
        entered = asyncio.Event()
        class Hanging(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices": []}\n\n'
                entered.set()
                await asyncio.Event().wait()
        async def hanging(request):
            return httpx.Response(200, stream=Hanging())
        self.handler = hanging
        pending = asyncio.create_task(self.call('cancel-stream', stream=True))
        await entered.wait()
        self.assertEqual((await self.call('busy-stream')).status_code, 409)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(self.ledger.snapshot()['attempts'][0]['status'], 'cancelled')
        self.handler = self.ok
        self.assertEqual((await self.call('after-cancel')).status_code, 200)
        self.assertEqual(len(self.sent), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)

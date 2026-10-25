"""Pinned native retry guards and SDK failure propagation; no provider calls."""
import ast
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
import s1
from gateway import Ledger, LOCAL_TOKEN, MODEL, create_app


class NativeBoundsTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        s1.configure()
        s1.install_bounds()

    async def test_pinned_retry_transforms_and_sdk_disabled(self):
        for module, relative in s1.TARGETS.items():
            tree = ast.parse((s1.lab.UPSTREAM / relative).read_text(encoding='utf-8-sig'))
            transform = s1.OneAttempt()
            compile(ast.fix_missing_locations(transform.visit(tree)), relative, 'exec')
            self.assertEqual(transform.changes, s1.EXPECTED_RETRY_PATCHES[module])
        from utils.llm_client.openai_client import ChatOpenAI
        client = ChatOpenAI(model=MODEL, base_url='http://127.0.0.1/unused',
                            api_key=LOCAL_TOKEN, max_retries=99)
        try:
            self.assertEqual(client._aclient.max_retries, 0)
            self.assertEqual(client._client.max_retries, 0)
        finally:
            await client.aclose()
            client.close()

    async def test_partial_gateway_stream_raises_in_native_sdk(self):
        from openai import AsyncOpenAI, APIError
        from utils.llm_client.openai_client import ChatOpenAI
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            ledger = Ledger(Path(folder) / 'test.sqlite3')
            sent = []
            async def truncated(request):
                sent.append(request)
                chunk = {'id': 'fake', 'object': 'chat.completion.chunk', 'created': 0,
                         'model': MODEL, 'choices': [{'index': 0, 'delta': {'content': '半截回复'},
                                                      'finish_reason': None}]}
                return httpx.Response(200, content=('data: ' + json.dumps(chunk) + '\n\n').encode())
            app = create_app(ledger, 'dummy-provider-token', httpx.MockTransport(truncated))
            client = ChatOpenAI(model=MODEL, base_url='http://127.0.0.1/unused', api_key=LOCAL_TOKEN)
            await client.aclose()
            client.close()
            client._aclient = AsyncOpenAI(api_key=LOCAL_TOKEN,
                base_url='http://127.0.0.1/conversation/v1', max_retries=0,
                http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app), trust_env=False))
            chunks = []
            try:
                # The native process guard also wraps MockTransport clients. Keep
                # the fake provider on loopback without weakening that guard.
                with patch('gateway.ENDPOINT', 'http://127.0.0.1/mock-provider'), self.assertRaises(APIError) as caught:
                    async for chunk in client.astream([{'role': 'user', 'content': '模拟流失败'}]):
                        chunks.append(chunk.content)
                self.assertIn('INCOMPLETE_STREAM', str(caught.exception))
                self.assertEqual(''.join(chunks), '半截回复')
                self.assertNotIn('dummy-provider-token', str(caught.exception))
                self.assertEqual(len(sent), 1)
                self.assertEqual(ledger.snapshot()['attempts'][0]['status'], 'failed')
            finally:
                await client.aclose()


if __name__ == '__main__':
    unittest.main(verbosity=2)

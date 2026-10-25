import asyncio
import json
import unittest

from server.cloud import CloudError
from server.neko import NekoClient


class FakeSocket:
    def __init__(self, frames=(), wait=False):
        self.frames = asyncio.Queue()
        for frame in frames:
            self.frames.put_nowait(frame)
        self.sent = []
        self.closed = False
        self.wait = wait

    async def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        if message['action'] == 'start_session':
            self.frames.put_nowait({'type': 'session_started', 'request_id': 'companion-start'})
        if message['action'] == 'end_session':
            self.frames.put_nowait({'type': 'status', 'message': json.dumps({'code': 'CHARACTER_LEFT'})})

    async def recv(self):
        return json.dumps(await self.frames.get())

    async def close(self):
        self.closed = True


class NekoTests(unittest.IsolatedAsyncioTestCase):
    def client(self, sockets):
        async def connector(*a, **kw):
            return sockets.pop(0)
        return NekoClient(connector=connector, voice=object())

    def turn_frames(self, turn, text):
        return [{'type': 'gemini_response', 'request_id': turn, 'turn_id': 'native-' + turn, 'text': text},
                {'type': 'system', 'request_id': turn, 'data': 'turn end'}]

    async def test_native_identity_and_one_connection_no_context_wrapper(self):
        socket = FakeSocket()
        client = self.client([socket])
        for turn in ('a', 'b'):
            # Set replies only once start has been ACKed.
            if client.ws is None:
                await client._connect()
            socket.frames.put_nowait({'type': 'gemini_response', 'request_id': 'stale', 'text': '旧回答'})
            for f in self.turn_frames(turn, '你好'):
                socket.frames.put_nowait(f)
            self.assertEqual(''.join([d async for d in client.stream_turn('s', turn, '新输入')]), '你好')
        self.assertEqual(sum(m['action'] == 'start_session' for m in socket.sent), 1)
        sent = [m for m in socket.sent if m['action'] == 'stream_data']
        self.assertEqual([m['data'] for m in sent], ['新输入', '新输入'])
        self.assertTrue(all('messages' not in m for m in sent))
        await client.close()
        self.assertTrue(socket.closed)

    async def test_cancel_teardown_ack_then_next_turn(self):
        first, second = FakeSocket(), FakeSocket()
        client = self.client([first, second])
        task = asyncio.create_task(self.consume(client, 'old'))
        while not any(m['action'] == 'stream_data' for m in first.sent):
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(first.closed)
        self.assertFalse(client.unsafe)
        await client._connect()
        for f in self.turn_frames('new', '新回答'):
            second.frames.put_nowait(f)
        self.assertEqual(await self.consume(client, 'new'), '新回答')
        await client.close()

    async def consume(self, client, turn):
        return ''.join([d async for d in client.stream_turn('s', turn, '输入')])

    async def test_discard_is_failure_no_retry(self):
        socket = FakeSocket()
        client = self.client([socket])
        await client._connect()
        socket.frames.put_nowait({'type': 'response_discarded', 'request_id': 'a'})
        with self.assertRaisesRegex(CloudError, 'neko_response_discarded'):
            await self.consume(client, 'a')
        self.assertEqual(sum(m['action'] == 'stream_data' for m in socket.sent), 1)
        self.assertTrue(socket.closed)

    async def test_reply_not_truncated_for_tts(self):
        client = self.client([])
        with self.assertRaisesRegex(CloudError, 'neko_reply_too_long'):
            await client.synthesize('长' * 241)


if __name__ == '__main__':
    unittest.main()

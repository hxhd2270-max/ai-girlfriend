"""Offline voice lifetime, cancellation, dispatch and ASR boundary tests."""
import asyncio
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from server.cloud import CloudError
from server.voice import BLOCK_BYTES, SpeechSegments, VoiceBroker, PlanRecognizer

SILENT = bytes(BLOCK_BYTES)
SPEECH = struct.pack('<1600h', *([2000, -2000]*800))


class FakeMic:
    opened = 0
    closed = 0
    instance = None

    def __init__(self):
        self.queue = asyncio.Queue()
        FakeMic.instance = self

    async def __aenter__(self):
        FakeMic.opened += 1
        return self

    async def __aexit__(self, *args):
        FakeMic.closed += 1

    async def read(self):
        return await self.queue.get()

    def discard(self):
        while not self.queue.empty():
            self.queue.get_nowait()

    def utterance(self):
        for frame in [SPEECH]*5+[SILENT]*9:
            self.queue.put_nowait(frame)


class VoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeMic.opened = FakeMic.closed = 0
        self.owner = object()
        self.states = []
        self.listening = asyncio.Event()
        self.recognizing = asyncio.Event()

    async def state(self, value, code=None):
        self.states.append((value, code))
        if value == 'listening': self.listening.set()
        if value == 'recognizing': self.recognizing.set()

    async def test_silence_exclusive_owner_and_close(self):
        recognizer = type('Recognizer', (), {'transcribe': AsyncMock(return_value='你好')})()
        broker = VoiceBroker(FakeMic, recognizer)
        self.assertEqual(FakeMic.opened, 0)
        self.assertTrue(broker.open(self.owner, object(), self.state, AsyncMock(), AsyncMock()))
        await asyncio.wait_for(self.listening.wait(), 1)
        self.assertFalse(broker.open(object(), object(), self.state, AsyncMock(), AsyncMock()))
        for _ in range(30): FakeMic.instance.queue.put_nowait(SILENT)
        await asyncio.sleep(.06)
        recognizer.transcribe.assert_not_called()
        await broker.close(object())
        self.assertIs(broker.owner, self.owner)
        await broker.close(self.owner)
        self.assertEqual(FakeMic.closed, 1)
        self.assertIsNone(broker.task)

    async def test_one_final_submit_waits_for_playback_and_discards_echo(self):
        playback = asyncio.Event()
        submitted = asyncio.Event()
        values = []
        async def submit(text):
            values.append(text)
            submitted.set()
        recognizer = type('Recognizer', (), {'transcribe': AsyncMock(return_value='语音原文')})()
        broker = VoiceBroker(FakeMic, recognizer)
        broker.open(self.owner, object(), self.state, submit, playback.wait)
        await asyncio.wait_for(self.listening.wait(), 1)
        FakeMic.instance.utterance()
        await asyncio.wait_for(submitted.wait(), 1)
        self.listening.clear()
        FakeMic.instance.utterance()
        await asyncio.sleep(.12)
        self.assertEqual(values, ['语音原文'])
        self.assertFalse(self.listening.is_set())
        playback.set()
        await asyncio.wait_for(self.listening.wait(), 2)
        self.assertEqual(recognizer.transcribe.await_count, 1)
        await broker.close(self.owner)

    async def test_cancel_discards_late_asr(self):
        cancelled = asyncio.Event()
        async def delayed(pcm):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        recognizer = type('Recognizer', (), {'transcribe': staticmethod(delayed)})()
        submit = AsyncMock()
        broker = VoiceBroker(FakeMic, recognizer)
        broker.open(self.owner, object(), self.state, submit, AsyncMock())
        await asyncio.wait_for(self.listening.wait(), 1)
        FakeMic.instance.utterance()
        await asyncio.wait_for(self.recognizing.wait(), 1)
        await asyncio.sleep(.03)
        await asyncio.wait_for(broker.close(self.owner), 1)
        self.assertTrue(cancelled.is_set())
        submit.assert_not_called()
        self.assertEqual(FakeMic.closed, 1)

    async def test_cancel_before_start_allows_reopen(self):
        broker = VoiceBroker(FakeMic)
        broker.open(self.owner, object(), self.state, AsyncMock(), AsyncMock())
        await broker.close(self.owner)
        self.assertIsNone(broker.task)
        self.assertIsNone(broker.owner)
        self.assertEqual(FakeMic.opened, 0)
        self.assertTrue(broker.open(self.owner, object(), self.state, AsyncMock(), AsyncMock()))
        await asyncio.wait_for(self.listening.wait(), 1)
        await broker.close(self.owner)

    async def test_empty_recognition_resumes_without_turn(self):
        recognizer = type('Recognizer', (), {'transcribe': AsyncMock(return_value='')})()
        submit, playback = AsyncMock(), AsyncMock()
        broker = VoiceBroker(FakeMic, recognizer)
        broker.open(self.owner, object(), self.state, submit, playback)
        await asyncio.wait_for(self.listening.wait(), 1)
        self.listening.clear()
        FakeMic.instance.utterance()
        await asyncio.wait_for(self.listening.wait(), 2)
        submit.assert_not_called()
        playback.assert_not_called()
        await broker.close(self.owner)

    async def test_asr_error_stops_without_retry_or_empty_turn(self):
        recognizer = type('Recognizer', (), {'transcribe': AsyncMock(side_effect=CloudError('voice_asr_timeout'))})()
        submit = AsyncMock()
        broker = VoiceBroker(FakeMic, recognizer)
        broker.open(self.owner, object(), self.state, submit, AsyncMock())
        await asyncio.wait_for(self.listening.wait(), 1)
        FakeMic.instance.utterance()
        await asyncio.wait_for(broker.task, 2)
        submit.assert_not_called()
        self.assertIn(('error', 'voice_asr_timeout'), self.states)
        self.assertEqual(FakeMic.closed, 1)
        self.assertEqual(recognizer.transcribe.await_count, 1)

    async def test_asr_official_schema_and_separate_usage(self):
        from server import usage
        original = httpx.AsyncClient
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={'output': {'text': '测试原文'}, 'usage': {'duration': 1}})
        with tempfile.TemporaryDirectory() as folder:
            limits = Path(folder)/'limits.json'
            limits.write_text(json.dumps(dict.fromkeys(usage.COUNTER_KEYS, None)))
            with (
                patch.dict(os.environ, {'COMPANION_DATA_DIR': folder, 'COMPANION_USAGE_LIMITS_PATH': str(limits)}),
                patch('server.voice._key', return_value='test-key'),
                patch('server.voice.httpx.AsyncClient', side_effect=lambda **kw: original(transport=httpx.MockTransport(handler), **kw)),
            ):
                result = await PlanRecognizer().transcribe(SPEECH*3)
                self.assertEqual(result, '测试原文')
                snapshot = usage.usage_status()
                self.assertEqual(snapshot['reserved'], dict.fromkeys(usage.COUNTER_KEYS, 0))
                self.assertEqual(snapshot['voiceReserved'], {'asr_calls': 1, 'audio_milliseconds': 300})
                self.assertEqual(requests[0]['model'], 'qwen-audio-3.0-asr-flash')
                self.assertTrue(requests[0]['input']['messages'][0]['content'][0]['input_audio']['data'].startswith('data:audio/wav;base64,'))


class SegmentTests(unittest.TestCase):
    def test_silence_short_noise_and_maximum_bound(self):
        vad = SpeechSegments()
        for _ in range(500): self.assertIsNone(vad.feed(SILENT))
        self.assertIsNone(vad.feed(SPEECH))
        for _ in range(9): self.assertIsNone(vad.feed(SILENT))
        outputs = [result for _ in range(400) if (result := vad.feed(SPEECH)) is not None]
        self.assertEqual([len(p) for p in outputs], [640000, 640000])

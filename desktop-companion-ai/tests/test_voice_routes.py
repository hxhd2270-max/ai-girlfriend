"""Authenticated voice dispatch through the existing NEKO/playback route."""
import asyncio
import hashlib
import secrets
import time
import unittest
import uuid
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from tests.test_shared_chat import service, WS, ORIGIN
from tests.test_voice import FakeMic
from server.voice import VoiceBroker


class FakeNeko:
    backend = 'neko'
    def __init__(self):
        self.inputs = []
    async def stream_turn(self, sid, turn, text):
        self.inputs.append(text)
        yield '我听到了。'
    async def synthesize(self, text):
        return dict(audio=service.tone(0), mime='audio/wav', durationMs=420)
    async def close(self):
        pass


class VoiceRouteTests(unittest.TestCase):
    def setUp(self):
        FakeMic.opened = FakeMic.closed = 0
        self.recognizer = type('Recognizer', (), {'transcribe': AsyncMock(return_value='语音识别原文')})()
        self.broker = VoiceBroker(FakeMic, self.recognizer)
        self.cloud = FakeNeko()
        self.patches = [patch.object(service, 'VOICE', self.broker),
                        patch.object(service, 'voice_available', return_value=True)]
        for p in self.patches: p.start()
        self.client = TestClient(service.app, base_url=service.ORIGIN)
        self.client.__enter__()
        self.client.get('/api/bootstrap')
        self.sid = self.client.cookies.get(service.COOKIE)
        self.token = secrets.token_urlsafe(32)
        self.digest = hashlib.sha256(self.token.encode()).hexdigest()
        service.DB.execute('INSERT INTO wallpaper_chat_pairs VALUES(?,?,?)',
                           (self.digest, self.sid, time.time()+60))
        service.DB.commit()
        self.url = WS+'/ws/wallpaper-chat?token='+self.token
        self.cloud_patch = patch.object(service, 'CLOUD', self.cloud)
        self.cloud_patch.start()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.cloud_patch.stop()
        for p in reversed(self.patches): p.stop()

    def recv(self, ws, kind, state=None):
        seen = []
        for _ in range(200):
            event = ws.receive_json(); seen.append(event)
            if event['type'] == kind and (state is None or event.get('voiceState') == state):
                return event, seen
        self.fail('Missing '+kind)

    def start(self, ws):
        self.assertEqual(FakeMic.opened, 0)
        ws.send_json(dict(v=1, type='voice_start'))
        self.recv(ws, 'voice_state', 'listening')
        self.assertEqual(FakeMic.opened, 1)

    def test_voice_uses_neko_once_and_waits_for_played_receipt(self):
        with self.client.websocket_connect(self.url, headers={'origin':'null'}) as ws:
            self.assertTrue(ws.receive_json()['voiceAvailable'])
            self.start(ws)
            self.client.portal.call(FakeMic.instance.utterance)
            turn, seen = self.recv(ws, 'voice_turn')
            self.assertEqual(turn['userText'], '语音识别原文')
            audio, _ = self.recv(ws, 'audio_segment')
            self.assertEqual(audio['turnId'], turn['turnId'])
            self.assertEqual(self.cloud.inputs, ['语音识别原文'])
            self.assertEqual(self.broker.session.voice_state, 'replying')
            for phase in ('started', 'ended'):
                ws.send_json(dict(v=1,type='played',turnId=turn['turnId'],segmentId=0,phase=phase))
            self.recv(ws, 'turn_done')
            self.recv(ws, 'voice_state', 'listening')
            # A stale player cancellation must not close a new listening cycle.
            ws.send_json(dict(v=1,type='cancel',turnId=str(uuid.uuid4())))
            ws.send_json(dict(v=1,type='refresh'))
            self.assertEqual(self.recv(ws, 'snapshot')[0]['voiceState'], 'listening')
            ws.send_json(dict(v=1,type='voice_stop'))
            self.recv(ws, 'voice_state', 'off')
        self.assertEqual(FakeMic.closed, 1)

    def test_disconnect_and_revoke_release_microphone(self):
        for revoke in (False, True):
            FakeMic.opened = 0
            before = FakeMic.closed
            with self.client.websocket_connect(self.url, headers={'origin':'null'}) as ws:
                ws.receive_json(); self.start(ws)
                if revoke:
                    self.client.delete('/api/wallpaper-pair', headers=ORIGIN)
                    with self.assertRaises(WebSocketDisconnect):
                        while True: ws.receive_json()
            self.client.portal.call(asyncio.sleep, .03)
            self.assertEqual(FakeMic.closed, before+1)
            self.assertIsNone(self.broker.owner)

    def test_text_takeover_closes_mic_and_cancels_pending_asr(self):
        async def pending(pcm):
            await asyncio.Future()
        self.recognizer.transcribe.side_effect = pending
        with self.client.websocket_connect(self.url, headers={'origin':'null'}) as voice:
            voice.receive_json(); self.start(voice)
            self.client.portal.call(FakeMic.instance.utterance)
            self.recv(voice, 'voice_state', 'recognizing')
            with self.client.websocket_connect(self.url, headers={'origin':'null'}) as text:
                text.receive_json()
                text.send_json(dict(v=1,type='send',turnId=str(uuid.uuid4()),text='文字接管'))
                self.recv(text, 'audio_segment')
                self.assertEqual(FakeMic.closed, 1)
                self.assertEqual(self.cloud.inputs, ['文字接管'])
                self.assertIsNone(self.broker.owner)

    def test_state_capability_cannot_open_microphone(self):
        token = secrets.token_urlsafe(32)
        service.DB.execute('INSERT INTO wallpaper_pairs VALUES(?,?,?)',
                           (hashlib.sha256(token.encode()).hexdigest(), self.sid,time.time()+60))
        service.DB.commit()
        with self.client.websocket_connect(WS+'/ws/paired-wallpaper?token='+token,headers={'origin':'null'}) as ws:
            ws.receive_json()
            ws.send_json(dict(v=1,type='voice_start'))
            with self.assertRaises(WebSocketDisconnect): ws.receive_json()
        self.assertEqual(FakeMic.opened, 0)

    def test_unavailable_backend_does_not_open_device(self):
        with patch.object(service, 'voice_available', return_value=False):
            with self.client.websocket_connect(self.url,headers={'origin':'null'}) as ws:
                ws.receive_json()
                ws.send_json(dict(v=1,type='voice_start'))
                error, _ = self.recv(ws, 'voice_state', 'error')
                self.assertEqual(error['code'], 'voice_unavailable')
        self.assertEqual(FakeMic.opened, 0)

import os
import tempfile
import unittest
import uuid
from pathlib import Path

_test_root = Path(__file__).resolve().parents[1] / 'data' / 'tests'
_test_root.mkdir(parents=True, exist_ok=True)
_tmp = tempfile.TemporaryDirectory(dir=_test_root, ignore_cleanup_errors=True)
os.environ['COMPANION_DATA_DIR'] = _tmp.name
from fastapi.testclient import TestClient
from server.app import app

ORIGIN={'origin':'http://127.0.0.1:8765'}

class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.client=TestClient(app,base_url='http://127.0.0.1:8765')
        self.client.__enter__()
        self.client.get('/api/bootstrap')

    def tearDown(self):
        self.client.__exit__(None,None,None)

    def recv_type(self,ws,kind,turn=None):
        for _ in range(200):
            event=ws.receive_json()
            self.assertEqual(event['v'],1)
            if event['type']==kind and (turn is None or event['turnId']==turn):
                return event
        self.fail('missing event '+kind)

    def send(self,ws,text='中文输入',**extra):
        turn=str(uuid.uuid4())
        ws.send_json(dict(v=1,type='send',turnId=turn,text=text,**extra))
        return turn

    def test_health_origin_and_host(self):
        self.assertEqual(self.client.get('/health').json()['cloudCalls'],0)
        self.assertEqual(self.client.get('/health',headers={'host':'evil.test'}).status_code,403)

    def test_context_excludes_legacy_mock_but_keeps_real_conversation(self):
        from server.app import DB, context_messages, mock_reply
        sid = str(uuid.uuid4())
        for user, reply in [('你好', mock_reply('你好')), ('我喜欢雨天', '记住了。')]:
            DB.execute("INSERT INTO turns(id,session_id,user_text,generated_text,status,created) VALUES(?,?,?,?,?,?)",
                       (str(uuid.uuid4()), sid, user, reply, 'completed', 0))
        DB.commit()
        messages = context_messages(sid, 'new-turn')
        self.assertEqual([m['content'] for m in messages[1:]], ['我喜欢雨天', '记住了。'])

    def test_spoken_sentences_keeps_punctuation_and_bounds_requests(self):
        from server.app import spoken_sentences
        self.assertEqual(spoken_sentences('你好。今天还好吗？'), ['你好。', '今天还好吗？'])
        self.assertEqual(spoken_sentences('先休息一下。你想看书，还是听会儿音乐？我都陪你。'),
                         ['先休息一下。', '你想看书，还是听会儿音乐？', '我都陪你。'])
        self.assertEqual(spoken_sentences(''), [])

    def test_wallpaper_pair_scope_origin_and_revoke(self):
        self.assertEqual(self.client.post('/api/wallpaper-pair').status_code,403)
        pair = self.client.post('/api/wallpaper-pair',headers=ORIGIN).json()
        url = 'ws://127.0.0.1:8765/ws/paired-wallpaper?token=' + pair['token']
        from starlette.websockets import WebSocketDisconnect
        for origin in ('https://other.invalid', ''):
            with self.assertRaises(WebSocketDisconnect):
                with self.client.websocket_connect(url,headers={'origin':origin}):
                    pass
        with self.client.websocket_connect(url,headers={'origin':'null'}) as viewer:
            state = viewer.receive_json()
            self.assertEqual(state['type'], 'state')
            self.assertNotIn('sessionId', state)
            self.assertNotIn('history', state)
            viewer.send_json(dict(v=1,type='send',text='forbidden'))
            with self.assertRaises(WebSocketDisconnect):
                viewer.receive_json()
        self.client.delete('/api/wallpaper-pair',headers=ORIGIN)
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(url,headers={'origin':'null'}):
                pass

    def test_wallpaper_pair_receives_playback_state_only(self):
        token = self.client.post('/api/wallpaper-pair',headers=ORIGIN).json()['token']
        url = 'ws://127.0.0.1:8765/ws/paired-wallpaper?token=' + token
        with self.client.websocket_connect('ws://127.0.0.1:8765/ws',headers=ORIGIN) as owner:
            owner.receive_json()
            with self.client.websocket_connect(url,headers={'origin':'null'}) as viewer:
                viewer.receive_json()
                turn = self.send(owner)
                audio = self.recv_type(owner,'audio_segment',turn)
                owner.send_json(dict(v=1,type='played',turnId=turn,segmentId=audio['segmentId'],phase='started'))
                for _ in range(20):
                    event = viewer.receive_json()
                    self.assertNotIn('audio', event)
                    self.assertNotIn('sessionId', event)
                    if event.get('state') == 'speaking':
                        self.assertEqual(event['subtitle'], audio['text'])
                        break
                else:
                    self.fail('no speaking state')
                owner.send_json(dict(v=1,type='cancel',turnId=turn))
                self.recv_type(owner,'cancelled',turn)
        self.assertEqual(self.client.delete('/api/history',headers={'origin':'http://evil.test'}).status_code,403)

    def test_pair_binary_command_and_live_revocation(self):
        from starlette.websockets import WebSocketDisconnect
        token = self.client.post('/api/wallpaper-pair',headers=ORIGIN).json()['token']
        url = 'ws://127.0.0.1:8765/ws/paired-wallpaper?token=' + token
        with self.client.websocket_connect(url,headers={'origin':'null'}) as viewer:
            viewer.receive_json()
            viewer.send_bytes(b'not-a-command')
            with self.assertRaises(WebSocketDisconnect) as caught:
                viewer.receive_json()
            self.assertEqual(caught.exception.code,1008)
        with self.client.websocket_connect(url,headers={'origin':'null'}) as viewer:
            viewer.receive_json()
            self.client.delete('/api/wallpaper-pair',headers=ORIGIN)
            with self.assertRaises(WebSocketDisconnect) as caught:
                viewer.receive_json()
            self.assertEqual(caught.exception.code,1008)

    def test_cloud_context_audio_and_failure_preserves_text(self):
        import asyncio
        from unittest.mock import patch
        from server.app import tone
        class FakeCloud:
            counters = {}
            calls = []
            async def stream_reply(inner,messages):
                inner.calls.append(messages)
                for delta in ("我记得，", "你喜欢安静。"): 
                    await asyncio.sleep(0)
                    yield delta
            async def synthesize(inner,text):
                return dict(audio=tone(),mime="audio/wav",durationMs=420)
        fake=FakeCloud()
        with patch("server.app.CLOUD",fake):
            with self.client.websocket_connect("ws://127.0.0.1:8765/ws",headers=ORIGIN) as ws:
                ws.receive_json()
                first=self.send(ws,"我喜欢安静")
                event=self.recv_type(ws,"audio_segment",first)
                self.assertFalse(event["testAudio"])
                for phase in ("started","ended"):
                    ws.send_json(dict(v=1,type="played",turnId=first,segmentId=0,phase=phase))
                self.recv_type(ws,"turn_done",first)
                second=self.send(ws,"你记得我的偏好吗？",fault="tts")
                error=self.recv_type(ws,"error",second)
                self.assertEqual(error["scope"],"tts")
                self.assertTrue(any(m["role"]=="user" and m["content"]=="我喜欢安静" for m in fake.calls[-1]))
                self.assertTrue(any(m["role"]=="assistant" for m in fake.calls[-1]))
                ws.send_json(dict(v=1,type="refresh"))
                self.assertEqual(self.recv_type(ws,"snapshot")["history"][-1]["generatedText"],"我记得，你喜欢安静。")

    def test_busy_owner_and_snapshot_revision(self):
        from starlette.websockets import WebSocketDisconnect
        with self.client.websocket_connect('ws://127.0.0.1:8765/ws',headers=ORIGIN) as ws:
            ws.receive_json()
            with self.client.websocket_connect('ws://127.0.0.1:8765/ws',headers=ORIGIN) as other:
                with self.assertRaises(WebSocketDisconnect) as failure:
                    other.receive_json()
                self.assertEqual(failure.exception.code,4009)
            ws.send_json(dict(v=1,type='refresh',requestRevision=7))
            self.assertEqual(self.recv_type(ws,'snapshot')['requestRevision'],7)
        with self.client.websocket_connect('ws://127.0.0.1:8765/ws',headers=ORIGIN) as ws:
            self.assertEqual(ws.receive_json()['type'],'snapshot')

    def test_private_audition_catalog(self):
        import json
        from server.app import DATA
        folder=DATA/'auditions'
        folder.mkdir(exist_ok=True)
        index=folder/'index.json'
        index.write_text(json.dumps([dict(id='sample-a',label='声音 A',text='测试',sourceKind='api_generated',status='ready',file='../sessions.sqlite3',secret='must not leak')]),encoding='utf-8')
        try:
            response=self.client.get('/api/auditions')
            self.assertEqual(response.status_code,200)
            self.assertNotIn('secret',response.text)
            self.assertNotIn('sqlite',response.text)
            self.assertEqual(self.client.get('/api/auditions/sample-a/audio').status_code,404)
            from server.app import tone
            import base64
            wave_file=folder/'test.wav'
            wave_file.write_bytes(base64.b64decode(tone()))
            index.write_text(json.dumps([dict(id='sample-a',label='声音 A',text='测试',sourceKind='api_generated',status='ready',file='test.wav')]),encoding='utf-8')
            audio=self.client.get('/api/auditions/sample-a/audio')
            self.assertEqual(audio.status_code,200)
            self.assertEqual(audio.content[:4],b'RIFF')
            self.assertEqual(audio.headers['cache-control'],'no-store')
            wave_file.unlink()
            with TestClient(app,base_url='http://127.0.0.1:8765') as guest:
                self.assertEqual(guest.get('/api/auditions').status_code,401)
                self.assertEqual(guest.get('/api/auditions/sample-a/audio').status_code,401)
        finally:
            index.unlink()

    def test_tts_failure_retry_and_refresh_no_audio(self):
        with self.client.websocket_connect('ws://127.0.0.1:8765/ws',headers=ORIGIN) as ws:
            ws.receive_json()
            old=self.send(ws,fault='tts')
            err=self.recv_type(ws,'error',old)
            self.assertEqual(err['scope'],'tts')
            ws.send_json(dict(v=1,type='refresh'))
            history=self.recv_type(ws,'snapshot')['history']
            self.assertTrue(history[-1]['generatedText'])
            new=str(uuid.uuid4())
            ws.send_json(dict(v=1,type='retry_audio',turnId=new,sourceTurnId=old))
            self.recv_type(ws,'turn_started',new)
            segments=[]
            while True:
                event=ws.receive_json()
                self.assertNotEqual(event['type'],'text_delta')
                if event['type']=='audio_segment':
                    segments.append(event)
                    for phase in ('started','ended'):
                        ws.send_json(dict(v=1,type='played',turnId=new,segmentId=event['segmentId'],phase=phase))
                if event['type']=='turn_done':break
            self.assertLessEqual(len(segments),3)
            ws.send_json(dict(v=1,type='refresh'))
            snap=self.recv_type(ws,'snapshot')
            self.assertIsNone(snap['activeTurnId'])
            self.assertEqual(snap['history'][-1]['status'],'completed')
            self.assertTrue(snap['history'][-1]['playedText'])
            self.assertNotIn('audio',str(snap['history']))

    def test_takeover_late_receipt_and_delete(self):
        with self.client.websocket_connect('ws://127.0.0.1:8765/ws',headers=ORIGIN) as ws:
            ws.receive_json()
            old=self.send(ws,'旧轮')
            self.recv_type(ws,'audio_segment',old)
            new=self.send(ws,'最新一轮')
            self.recv_type(ws,'turn_started',new)
            ws.send_json(dict(v=1,type='played',turnId=old,segmentId=0,phase='ended'))
            ws.send_json(dict(v=1,type='cancel',turnId=new))
            self.recv_type(ws,'cancelled',new)
            ws.send_json(dict(v=1,type='refresh'))
            snap=self.recv_type(ws,'snapshot')
            self.assertIsNone(snap['activeTurnId'])
            for row in snap['history']:
                self.assertFalse(row['playedText'])
            self.assertEqual(self.client.delete('/api/history',headers=ORIGIN).status_code,200)
            self.assertEqual(self.recv_type(ws,'snapshot')['history'],[])

    def test_disconnect_interrupts_and_reconnect_never_replays(self):
        with self.client.websocket_connect('ws://127.0.0.1:8765/ws',headers=ORIGIN) as ws:
            ws.receive_json()
            old=self.send(ws)
            self.recv_type(ws,'audio_segment',old)
        with self.client.websocket_connect('ws://127.0.0.1:8765/ws',headers=ORIGIN) as ws:
            snap=ws.receive_json()
            self.assertEqual(snap['type'],'snapshot')
            self.assertIsNone(snap['activeTurnId'])
            self.assertEqual(snap['history'][-1]['status'],'interrupted')

if __name__=='__main__':unittest.main(verbosity=2)


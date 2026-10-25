"""Shared wallpaper chat: capability separation, ownership and replay fencing."""
import hashlib
import os
from pathlib import Path
import secrets
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

_root = Path(__file__).resolve().parents[1] / 'data' / 'tests'
_root.mkdir(parents=True, exist_ok=True)
_tmp = tempfile.TemporaryDirectory(dir=_root, ignore_cleanup_errors=True)
os.environ.setdefault('COMPANION_DATA_DIR', _tmp.name)
import server.app as service

ORIGIN = {'origin': service.ORIGIN}
WS = service.ORIGIN.replace('http:', 'ws:')

class SharedChatTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(service.app, base_url=service.ORIGIN)
        self.client.__enter__()
        self.client.get('/api/bootstrap')
        self.sid = self.client.cookies.get(service.COOKIE)
        self.token = secrets.token_urlsafe(32)
        self.digest = hashlib.sha256(self.token.encode()).hexdigest()
        service.DB.execute('INSERT INTO wallpaper_chat_pairs VALUES(?,?,?)',
                           (self.digest, self.sid, time.time()+60))
        service.DB.commit()
        self.chat_url = WS+'/ws/wallpaper-chat?token='+self.token

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def recv(self, ws, kind, turn=None):
        events=[]
        for _ in range(200):
            event=ws.receive_json(); events.append(event)
            if event['type']==kind and (turn is None or event['turnId']==turn):
                return event, events
        self.fail('Missing '+kind)

    def send(self, ws, text='测试'): 
        turn=str(uuid.uuid4())
        ws.send_json(dict(v=1,type='send',turnId=turn,text=text))
        return turn

    def test_capabilities_are_separate_and_origins_checked(self):
        state=secrets.token_urlsafe(32)
        service.DB.execute('INSERT INTO wallpaper_pairs VALUES(?,?,?)',
                           (hashlib.sha256(state.encode()).hexdigest(),self.sid,time.time()+60))
        service.DB.commit()
        for url, origin in [(WS+'/ws/wallpaper-chat?token='+state, 'null'),
                            (self.chat_url, 'https://evil.invalid'),
                            (WS+'/ws/paired-wallpaper?token='+self.token, 'null')]:
            with self.assertRaises(WebSocketDisconnect):
                with self.client.websocket_connect(url, headers={'origin':origin}):
                    pass

    def test_neko_bootstrap_shares_display_without_importing_old_history(self):
        from types import SimpleNamespace
        old_session = self.sid
        old_turn = str(uuid.uuid4())
        service.DB.execute("INSERT INTO turns(id,session_id,user_text,status,created) VALUES(?,?,?,?,?)",
                           (old_turn, old_session, 'local fixture, never imported', 'completed', time.time()))
        service.DB.commit()
        with patch.object(service, 'CLOUD', SimpleNamespace(backend='neko')):
            first = self.client.get('/api/bootstrap')
            shared = self.client.cookies.get(service.COOKIE)
            self.assertEqual(first.json()['dialogueBackend'], 'neko')
            self.assertNotEqual(shared, old_session)
            other = TestClient(service.app, base_url=service.ORIGIN)
            try:
                other.get('/api/bootstrap')
                self.assertEqual(other.cookies.get(service.COOKIE), shared)
            finally:
                other.close()
            self.assertEqual(service.DB.execute('SELECT session_id FROM turns WHERE id=?',
                                               (old_turn,)).fetchone()[0], old_session)
            self.assertFalse(service.DB.execute('SELECT 1 FROM turns WHERE session_id=?',
                                                (shared,)).fetchone())
            self.client.cookies.set(service.COOKIE, old_session, domain='127.0.0.1', path='/')
            with self.assertRaises(WebSocketDisconnect):
                with self.client.websocket_connect(WS+'/ws?shared=1', headers=ORIGIN):
                    pass

    def test_takeover_sync_invalid_input_and_old_disconnect(self):
        with self.client.websocket_connect(self.chat_url,headers={'origin':'null'}) as wallpaper:
            self.assertFalse(wallpaper.receive_json()['ownsPlayback'])
            with self.client.websocket_connect(WS+'/ws?shared=1',headers=ORIGIN) as web:
                self.assertFalse(web.receive_json()['ownsPlayback'])
                first=self.send(web,'第一轮')
                self.recv(web,'audio_segment',first)
                _, passive=self.recv(wallpaper,'synthesis_done',first)
                self.assertTrue(any(e['type']=='turn_started' and e['userText']=='第一轮' for e in passive))
                self.assertFalse(any(e['type']=='audio_segment' for e in passive))
                wallpaper.send_json(dict(v=1,type='send',turnId='bad',text=''))
                self.recv(wallpaper,'error')
                web.send_json(dict(v=1,type='refresh'))
                snap,events=self.recv(web,'snapshot')
                self.assertEqual(snap['activeTurnId'],first)
                self.assertTrue(snap['ownsPlayback'])
                self.assertFalse(any(e['type']=='error' for e in events))
                second=self.send(wallpaper,'第二轮')
                self.recv(web,'control_lost')
                self.recv(wallpaper,'audio_segment',second)
                web.send_json(dict(v=1,type='played',turnId=second,segmentId=0,phase='started'))
                web.send_json(dict(v=1,type='cancel',turnId=second))
                web.send_json(dict(v=1,type='refresh'))
                snap,events=self.recv(web,'snapshot')
                self.assertFalse(snap['ownsPlayback'])
                self.assertEqual(snap['activeTurnId'],second)
                self.assertFalse(any(e['type']=='audio_segment' and e['turnId']==second for e in events))
            wallpaper.send_json(dict(v=1,type='refresh'))
            snap,_=self.recv(wallpaper,'snapshot')
            self.assertTrue(snap['ownsPlayback'])
            self.assertEqual(snap['activeTurnId'],second)
            self.assertFalse(snap['history'][-1]['playStartedText'])

    def test_revocation_closes_interactive_owner(self):
        with self.client.websocket_connect(self.chat_url,headers={'origin':'null'}) as wallpaper:
            wallpaper.receive_json()
            turn=self.send(wallpaper)
            self.recv(wallpaper,'audio_segment',turn)
            self.client.delete('/api/wallpaper-pair',headers=ORIGIN)
            with self.assertRaises(WebSocketDisconnect) as error:
                while True: wallpaper.receive_json()
            self.assertEqual(error.exception.code,1008)
        row=service.DB.execute('SELECT status FROM turns WHERE id=?',(turn,)).fetchone()
        self.assertEqual(row[0],'interrupted')

    def test_clear_requires_version_and_http_snapshot_keeps_ownership(self):
        with self.client.websocket_connect(WS+'/ws?shared=1',headers=ORIGIN) as web:
            web.receive_json()
            turn=self.send(web)
            self.recv(web,'audio_segment',turn)
            with self.client.websocket_connect(self.chat_url,headers={'origin':'null'}) as wallpaper:
                wallpaper.receive_json()
                wallpaper.send_json(dict(v=2,type='clear_history'))
                self.recv(wallpaper,'error')
                self.assertTrue(service.DB.execute('SELECT 1 FROM turns WHERE id=?',(turn,)).fetchone())
                self.client.delete('/api/history',headers=ORIGIN)
                self.assertTrue(self.recv(web,'snapshot')[0]['ownsPlayback'])
                snap,_=self.recv(wallpaper,'snapshot')
                self.assertFalse(snap['ownsPlayback'])
                self.assertEqual(snap['history'],[])
                wallpaper.send_json(dict(v=1,type='clear_history'))
                self.recv(wallpaper,'history_cleared')
                self.recv(web,'history_cleared')

    def test_native_editor_targets_requester_and_checks_capability(self):
        async def runner(script, draft, layout=None):
            return {'action':'send', 'text':draft+'完成'}
        with patch.object(service.NATIVE_INPUT, 'runner', runner):
            with self.client.websocket_connect(WS+'/ws?shared=1',headers=ORIGIN) as web:
                web.receive_json()
                web.send_json(dict(v=1,type='open_input',requestId=str(uuid.uuid4()),text='浏览器'))
                self.recv(web,'error')
                with self.client.websocket_connect(self.chat_url,headers={'origin':'null'}) as wallpaper:
                    wallpaper.receive_json()
                    for message in [dict(v=2,type='open_input'), dict(v=1,type='open_input',requestId='bad'),
                                    dict(v=1,type='open_input',requestId=str(uuid.uuid4()),text='x'*2001),
                                    dict(v=1,type='open_input',requestId=str(uuid.uuid4()),text='中文'),
                                    dict(v=1,type='open_input',requestId=str(uuid.uuid4()),text='中文',layout={'left':-1})]:
                        wallpaper.send_json(message)
                        self.recv(wallpaper,'error')
                    request = str(uuid.uuid4())
                    wallpaper.send_json(dict(v=1,type='open_input',requestId=request,text='中文',layout=dict(left=50,top=400,width=500,height=110,viewportWidth=1000,viewportHeight=700,fontSize=13)))
                    result,_ = self.recv(wallpaper,'native_input_result')
                    self.assertEqual(result['requestId'],request)
                    self.assertEqual(result['text'],'中文完成')
                    self.assertEqual(result['action'],'send')
                    web.send_json(dict(v=1,type='refresh'))
                    snap,events = self.recv(web,'snapshot')
                    self.assertFalse(any(e['type']=='native_input_result' for e in events))
                    self.assertFalse(snap['ownsPlayback'])

    def test_disconnect_and_revoke_cancel_native_editor(self):
        import threading
        import asyncio
        for revoke in (False, True):
            entered, stopped = threading.Event(), threading.Event()
            async def runner(script, draft, layout=None):
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    stopped.set()
            with patch.object(service.NATIVE_INPUT,'runner',runner):
                with self.client.websocket_connect(self.chat_url,headers={'origin':'null'}) as wallpaper:
                    wallpaper.receive_json()
                    wallpaper.send_json(dict(v=1,type='open_input',requestId=str(uuid.uuid4()),text='',layout=dict(left=50,top=400,width=500,height=110,viewportWidth=1000,viewportHeight=700,fontSize=13)))
                    self.assertTrue(entered.wait(2))
                    if revoke:
                        self.client.delete('/api/wallpaper-pair',headers=ORIGIN)
                        with self.assertRaises(WebSocketDisconnect):
                            while True: wallpaper.receive_json()
                self.assertTrue(stopped.wait(2))

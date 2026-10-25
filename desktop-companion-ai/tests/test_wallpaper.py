import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
_test_root = Path(__file__).resolve().parents[1] / 'data' / 'tests'
_test_root.mkdir(parents=True, exist_ok=True)
_tmp = tempfile.TemporaryDirectory(dir=_test_root, ignore_cleanup_errors=True)
os.environ.setdefault('COMPANION_DATA_DIR', _tmp.name)
import server.app as service
from server.wallpaper import install_pair

ORIGIN = {'origin': service.ORIGIN}


class WallpaperTests(unittest.TestCase):
    def test_health_survives_usage_failure_without_leaking_details(self):
        class BrokenCloud:
            def usage_status(self):
                raise RuntimeError('private detail')
        with patch.object(service, 'CLOUD', BrokenCloud()):
            response = self.client.get('/health')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['cloudCalls'], {})
            self.assertFalse(response.json()['usageAvailable'])
            self.assertNotIn('private detail', response.text)

    def setUp(self):
        self.client = TestClient(service.app, base_url=service.ORIGIN)
        self.client.__enter__()
        self.client.get('/api/bootstrap')

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def test_pair_survives_reopened_database_and_new_runtime(self):
        pair = self.client.post('/api/wallpaper-pair', headers=ORIGIN).json()
        digest = hashlib.sha256(pair['token'].encode()).hexdigest()
        row = service.DB.execute('SELECT * FROM wallpaper_pairs WHERE digest=?', (digest,)).fetchone()
        self.assertNotIn(pair['token'], tuple(row))
        path = service.DB.execute('PRAGMA database_list').fetchone()[2]
        other = sqlite3.connect(path, check_same_thread=False)
        other.row_factory = sqlite3.Row
        try:
            with patch.object(service, 'DB', other), patch.object(service, 'runtimes', {}):
                with self.client.websocket_connect(service.ORIGIN.replace('http:', 'ws:')+'/ws/paired-wallpaper?token='+pair['token'], headers={'origin':'null'}) as ws:
                    event = ws.receive_json()
                    self.assertEqual(event['state'], 'idle')
                    self.assertNotIn('history', event)
                    status = self.client.get('/api/wallpaper-pair').json()
                    self.assertEqual(status['connected'], 1)
                    self.assertNotIn('token', status)
        finally:
            other.close()

    def test_expired_pair_rejected(self):
        pair = self.client.post('/api/wallpaper-pair', headers=ORIGIN).json()
        service.DB.execute('UPDATE wallpaper_pairs SET expires=0 WHERE digest=?',
                           (hashlib.sha256(pair['token'].encode()).hexdigest(),))
        service.DB.commit()
        self.assertFalse(self.client.get('/api/wallpaper-pair').json()['paired'])
        with self.assertRaises(WebSocketDisconnect) as caught:
            with self.client.websocket_connect(service.ORIGIN.replace('http:', 'ws:')+'/ws/paired-wallpaper?token='+pair['token'], headers={'origin':'null'}):
                pass
        self.assertEqual(caught.exception.code, 1008)

    def test_install_fixed_target_private_backup_and_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wallpaper = root/'wallpaper'; wallpaper.mkdir()
            (wallpaper/'we-bridge.js').write_text('// bridge', encoding='utf-8')
            config = wallpaper/'ai-pair.local.js'
            original = b'window.PURPLE_AI_CONFIG={enabled:false};'
            config.write_bytes(original)
            with patch.object(service, 'WALLPAPER_DIR', wallpaper), patch.object(service, 'DATA', root/'private'):
                self.assertEqual(self.client.post('/api/wallpaper-pair/install').status_code, 403)
                response = self.client.post('/api/wallpaper-pair/install', headers=ORIGIN)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('token', response.json())
                text = config.read_text(encoding='utf-8')
                payload = json.loads(text.split('Object.freeze(', 1)[1].rsplit(');', 1)[0])
                self.assertTrue(payload['enabled'])
                self.assertEqual(len(payload['token']), 43)
                self.assertEqual((root/'private'/'wallpaper-backup'/'ai-pair.previous.js').read_bytes(), original)
                self.assertTrue(self.client.get('/api/wallpaper-pair').json()['paired'])
                self.assertNotIn(payload['token'], response.text)
        no_cookie = TestClient(service.app, base_url=service.ORIGIN)
        self.assertEqual(no_cookie.get('/api/usage').status_code, 401)
        self.assertEqual(no_cookie.post('/api/wallpaper-pair/install', headers=ORIGIN).status_code, 401)

    def test_missing_target_is_not_created_and_replace_failure_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'we-bridge.js').write_text('// bridge', encoding='utf-8')
            with self.assertRaises(OSError):
                install_pair(root, root/'backup', 'test')
            target = root/'ai-pair.local.js'; target.write_bytes(b'original')
            import server.wallpaper as wallpaper
            replace = wallpaper.os.replace
            def fail_target(source, destination):
                if Path(destination) == target:
                    raise OSError('test failure')
                return replace(source, destination)
            with patch.object(wallpaper.os, 'replace', side_effect=fail_target):
                with self.assertRaises(OSError):
                    install_pair(root, root/'backup', 'test')
            self.assertEqual(target.read_bytes(), b'original')
            self.assertEqual(list(root.glob('.ai-pair-*')), [])

    def test_reparse_target_is_rejected_before_reading(self):
        from types import SimpleNamespace
        with patch('server.wallpaper.Path.lstat', return_value=SimpleNamespace(st_file_attributes=0x400)):
            with self.assertRaisesRegex(OSError, 'reparse_target'):
                install_pair(Path('unused'), Path('unused-backup'), 'test')

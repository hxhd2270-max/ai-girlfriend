import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

_test_root = Path(__file__).resolve().parents[1] / 'data/tests'
_test_root.mkdir(parents=True, exist_ok=True)
_data = tempfile.TemporaryDirectory(dir=_test_root, ignore_cleanup_errors=True)
os.environ['COMPANION_DATA_DIR'] = _data.name
os.environ['COMPANION_CLOUD_ENABLED'] = '0'
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server import app as service
from server.standalone import create_router, ASSET_ROOT, MEDIA


class IndependentEntryTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(service.app, base_url=service.ORIGIN)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def test_entry_uses_regular_bootstrap_without_pair_or_we_scripts(self):
        response = self.client.get('/standalone')
        self.assertEqual(response.status_code, 200)
        self.assertIn('/web/standalone.js', response.text)
        for old in ('pair.js', 'chat-ui.js', 'ai-pair', 'wallpaperPropertyListener'):
            self.assertNotIn(old, response.text)
        self.assertIn("script-src 'self'", response.headers['content-security-policy'])
        self.assertNotIn("'unsafe-inline'", response.headers['content-security-policy'])
        self.client.get('/api/bootstrap')
        with self.client.websocket_connect(service.ORIGIN.replace('http:', 'ws:') + '/ws?shared=1', headers={'origin': service.ORIGIN}) as ws:
            first = ws.receive_json()
            self.assertEqual(first['type'], 'snapshot')
            self.assertEqual(first['history'], [])

    def test_cookie_auth_host_and_origin_still_required(self):
        from starlette.websockets import WebSocketDisconnect
        self.assertEqual(self.client.get('/standalone', headers={'host': 'evil.test'}).status_code, 403)
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(service.ORIGIN.replace('http:', 'ws:') + '/ws?shared=1', headers={'origin': service.ORIGIN}):
                pass
        self.client.get('/api/bootstrap')
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(service.ORIGIN.replace('http:', 'ws:') + '/ws?shared=1', headers={'origin': 'null'}):
                pass

    @unittest.skipUnless((ASSET_ROOT / 'manifest.json').is_file(),
                         'redistributable character media is not installed')
    def test_all_independent_assets_are_regular_files_with_verified_hashes(self):
        manifest = json.loads((ASSET_ROOT / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual({row['path'] for row in manifest['files']}, MEDIA)
        for row in manifest['files']:
            with self.subTest(asset=row['path']):
                path = ASSET_ROOT / row['path']
                self.assertEqual(path.resolve(), path.absolute())
                self.assertEqual(path.stat().st_size, row['bytes'])
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), row['sha256'])
                response = self.client.get('/standalone-assets/' + row['path'], headers={'range': 'bytes=0-15'})
                self.assertEqual(response.status_code, 206)
                self.assertEqual(len(response.content), 16)
                self.assertEqual(response.headers['content-range'], f"bytes 0-15/{row['bytes']}")

    def test_unknown_assets_and_traversal_are_denied(self):
        for path in ('manifest.json', 'actions/not-present.webm', 'actions/reading.mp4',
                     'actions/../../server/app.py', '%2e%2e/server/app.py'):
            self.assertEqual(self.client.get('/standalone-assets/' + path).status_code, 404)

    def test_route_does_not_fall_back_to_workshop_or_old_data_when_bundle_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            isolated = FastAPI()
            isolated.include_router(create_router(Path(directory)))
            with TestClient(isolated) as client:
                self.assertEqual(client.get('/standalone-assets/actions/reading.webm').status_code, 404)

    def test_neko_bootstrap_reuses_authoritative_session(self):
        class FakeNeko:
            backend = 'neko'

            async def close(self):
                pass
        service.DB.execute('CREATE TABLE IF NOT EXISTS backend_sessions(backend TEXT PRIMARY KEY, session_id TEXT NOT NULL)')
        service.DB.execute("DELETE FROM backend_sessions WHERE backend='neko'")
        service.DB.commit()
        with patch.object(service, 'CLOUD', FakeNeko()):
            self.assertEqual(self.client.get('/api/bootstrap').json()['dialogueBackend'], 'neko')
            sid = self.client.cookies[service.COOKIE]
            with TestClient(service.app, base_url=service.ORIGIN) as second:
                second.get('/api/bootstrap')
                self.assertEqual(second.cookies[service.COOKIE], sid)
            with self.client.websocket_connect(service.ORIGIN.replace('http:', 'ws:') + '/ws?shared=1', headers={'origin': service.ORIGIN}) as ws:
                snapshot = ws.receive_json()
                self.assertEqual(snapshot['dialogueBackend'], 'neko')


if __name__ == '__main__':
    unittest.main()

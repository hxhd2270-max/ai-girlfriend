"""Fictional credentials only; no provider network calls or live config writes."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from server.admin import create_router
from server.provider_config import ProviderSettings, PREFIX, read_encrypted
from tools import start_plan

FAKE = 'sk-offline-fictional-key'
OTHER = 'sk-offline-fictional-replacement'
TEST_ROOT = Path(__file__).resolve().parents[1] / 'data/tests'
TEST_ROOT.mkdir(parents=True, exist_ok=True)


class ProviderPathTests(unittest.TestCase):
    def test_launcher_default_key_path_stays_inside_project(self):
        self.assertEqual(start_plan.DEFAULT_KEY_FILE, start_plan.ROOT / 'config/provider-key.txt')


@unittest.skipUnless(os.name == 'nt', 'Windows DPAPI required')
class ProviderConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=TEST_ROOT, ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'DASHSCOPE_API_KEY': FAKE}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.provider = ProviderSettings(self.root, object())

    def test_encrypted_roundtrip_redacted_response_and_no_live_mutation(self):
        response = self.provider.save({'revision': 'empty', 'key': OTHER})
        raw = self.provider.path.read_bytes()
        self.assertTrue(raw.startswith(PREFIX))
        self.assertNotIn(OTHER.encode(), raw)
        self.assertEqual(read_encrypted(self.provider.path), OTHER)
        self.assertNotIn(OTHER, json.dumps(response))
        self.assertNotIn(FAKE, json.dumps(response))
        self.assertEqual(os.environ['DASHSCOPE_API_KEY'], FAKE)
        self.assertTrue(response['restartRequired'])
        self.assertEqual(response['validation'], 'not_tested')
        self.assertFalse(list(self.provider.path.parent.glob('.provider-*.tmp')))
        self.assertFalse(self.provider.path.with_suffix('.txt').exists())

    def test_stale_revision_preserves_saved_credential(self):
        response = self.provider.save({'revision': 'empty', 'key': FAKE})
        self.assertFalse(response['restartRequired'])
        with self.assertRaises(HTTPException) as error:
            self.provider.save({'revision': 'empty', 'key': OTHER})
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(read_encrypted(self.provider.path), FAKE)

    def test_failed_encryption_leaves_previous_file_and_generic_error(self):
        response = self.provider.save({'revision': 'empty', 'key': FAKE})
        before = self.provider.path.read_bytes()
        with patch('server.provider_config.crypt', side_effect=OSError(OTHER)):
            with self.assertRaises(HTTPException) as error:
                self.provider.save({'revision': response['revision'], 'key': OTHER})
        self.assertNotIn(OTHER, error.exception.detail)
        self.assertEqual(self.provider.path.read_bytes(), before)

    def test_bad_formats_rejected_without_echo_or_files(self):
        for value in ('', 'sk-short', OTHER+' ', OTHER+'\n', None, {}, 7):
            with self.subTest(kind=type(value).__name__), self.assertRaises(HTTPException) as error:
                self.provider.save({'revision': 'empty', 'key': value})
            self.assertEqual(error.exception.status_code, 422)
            self.assertNotIn(OTHER, error.exception.detail)
        for body in ([], None, {'key': OTHER}, {'revision': 'empty', 'key': OTHER, 'extra': True}):
            with self.assertRaises(HTTPException):
                self.provider.save(body)
        self.assertFalse(self.provider.path.exists())

    def test_launcher_prefers_managed_default_but_honors_external_file(self):
        self.provider.save({'revision': 'empty', 'key': OTHER})
        with patch.object(start_plan, 'ROOT', self.root):
            self.assertEqual(start_plan.read_credential(start_plan.DEFAULT_KEY_FILE), OTHER)
            self.assertEqual(start_plan.read_credential(self.root/'config/provider-key.txt'), OTHER)
            external = self.root/'external.txt'
            external.write_text(FAKE, encoding='utf-8')
            with patch.dict(os.environ, {'COMPANION_KEY_FILE': str(external)}):
                self.assertEqual(start_plan.read_credential(external), FAKE)
                self.assertTrue(self.provider.public()['externalOverride'])
            self.provider.path.write_bytes(b'corrupt')
            with self.assertRaises(start_plan.StartupError):
                start_plan.read_credential(self.root/'config/provider-key.txt')
            recovered = self.provider.public()
            self.assertTrue(recovered['savedUnreadable'])
            self.provider.save({'revision': recovered['revision'], 'key': FAKE})
            self.assertEqual(read_encrypted(self.provider.path), FAKE)

    def test_api_auth_origin_validation_and_redaction(self):
        app = FastAPI()
        app.include_router(create_router(SimpleNamespace(root=self.root, cloud=object()), lambda value: value == 'test-session', 'test-cookie'))
        with TestClient(app, base_url='http://127.0.0.1:8765') as client:
            self.assertEqual(client.get('/api/admin/provider').status_code, 401)
            client.cookies.set('test-cookie', 'test-session')
            self.assertEqual(client.get('/api/admin/provider').json()['revision'], 'empty')
            payload = {'revision': 'empty', 'key': OTHER}
            self.assertEqual(client.post('/api/admin/provider', json=payload).status_code, 403)
            headers = {'Origin': 'http://127.0.0.1:8765'}
            self.assertEqual(client.post('/api/admin/provider', content='{', headers=headers).status_code, 422)
            self.assertEqual(client.post('/api/admin/provider', content='x'*4097, headers=headers).status_code, 413)
            result = client.post('/api/admin/provider', json=payload, headers=headers)
            self.assertEqual(result.status_code, 200)
            self.assertNotIn(OTHER, result.text)
            self.assertEqual(read_encrypted(self.provider.path), OTHER)

    def test_portable_inventory_excludes_both_credential_stores(self):
        try:
            from tools.build_portable import file_inventory
        except ModuleNotFoundError:
            self.skipTest('portable distribution builder is not included')
        config = self.root/'config'
        config.mkdir()
        (config/'provider-key.txt').write_text(FAKE, encoding='utf-8')
        (config/'provider-key.dpapi').write_bytes(b'encrypted-fixture')
        (config/'safe.json').write_text('{}', encoding='utf-8')
        inventory = file_inventory(self.root)
        rendered = json.dumps(inventory)
        self.assertNotIn('provider-key', rendered)
        self.assertIn('safe.json', rendered)

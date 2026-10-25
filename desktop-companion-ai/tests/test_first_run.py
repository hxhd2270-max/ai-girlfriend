"""Account setup must fail closed and never save plaintext credentials."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools import first_run
from server.provider_config import read_encrypted


class FirstRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'config').mkdir()
        self.limits = self.root / 'config/usage-limits.json'
        self.limits.write_text('{"chat_calls":0,"tts_calls":0,"tts_estimated_characters":0}')
        self.values = dict(chat_calls='100', tts_calls='30', tts_estimated_characters='5000')
        self.key = 'sk-offline-first-run-fixture-only'

    def test_invalid_budget_cannot_enable_or_replace_account(self):
        for raw in ('0', '-1', '1.5', '', 'null', str(2**63)):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                first_run.save_settings(self.root, self.key, dict(self.values, chat_calls=raw), keep_key=False)
            self.assertFalse((self.root / 'config/provider-key.dpapi').exists())
            self.assertEqual(json.loads(self.limits.read_text())['chat_calls'], 0)

    def test_failed_encryption_keeps_cloud_disabled(self):
        with patch('server.provider_config.crypt', side_effect=OSError('fixture')):
            with self.assertRaises(Exception):
                first_run.save_settings(self.root, self.key, self.values, keep_key=False)
        self.assertEqual(json.loads(self.limits.read_text())['chat_calls'], 0)
        self.assertEqual(list((self.root / 'config').glob('.*.tmp')), [])

    def test_source_simulation_uses_development_launcher_without_package_preflight(self):
        (self.root / 'tools').mkdir()
        (self.root / 'tools/build_portable.py').touch()
        with patch.object(first_run, 'ROOT', self.root), \
             patch('tools.portable_runtime.preflight') as preflight, \
             patch.object(first_run, 'current_state') as state, \
             patch('tools.start_standalone.launch', return_value=0) as launch:
            self.assertEqual(first_run.main(['--development', '--simulation']), 0)
            preflight.assert_not_called()
            state.assert_not_called()
            launch.assert_called_once_with(simulation=True, acceptance=False, desktop=True, checked=False)

    def test_distribution_always_keeps_preflight_even_with_development_flag(self):
        (self.root / 'portable.json').write_text('{}')
        with patch.object(first_run, 'ROOT', self.root), \
             patch('tools.portable_runtime.preflight', side_effect=ValueError('damaged')) as preflight, \
             patch('tools.start_standalone.launch') as launch:
            with self.assertRaises(ValueError):
                first_run.main(['--development', '--simulation'])
            preflight.assert_called_once_with(self.root)
            launch.assert_not_called()

    def test_missing_manifest_in_incomplete_package_does_not_start(self):
        with patch.object(first_run, 'ROOT', self.root), \
             patch('tools.start_standalone.launch') as launch:
            self.assertEqual(first_run.main(['--development', '--simulation']), 1)
            launch.assert_not_called()

    def test_development_configure_does_not_launch_services(self):
        (self.root / 'tools').mkdir()
        (self.root / 'tools/build_portable.py').touch()
        with patch.object(first_run, 'ROOT', self.root), \
             patch.object(first_run, 'current_state', return_value=(True, {}, True)), \
             patch.object(first_run, 'setup_window', return_value='cloud') as setup, \
             patch('tools.start_standalone.launch') as launch:
            self.assertEqual(first_run.main(['--development', '--configure']), 0)
            setup.assert_called_once_with(self.root)
            launch.assert_not_called()

    def test_saved_simulation_mode_skips_setup_on_next_open(self):
        (self.root / 'tools').mkdir()
        (self.root / 'tools/build_portable.py').touch()
        first_run.save_startup_mode(self.root, 'simulation')
        with patch.object(first_run, 'ROOT', self.root), \
             patch.object(first_run, 'current_state') as state, \
             patch.object(first_run, 'setup_window') as setup, \
             patch('tools.start_standalone.launch', return_value=0) as launch:
            self.assertEqual(first_run.main(['--development']), 0)
            state.assert_not_called()
            setup.assert_not_called()
            launch.assert_called_once_with(simulation=True, acceptance=False, desktop=True, checked=False)

    def test_startup_mode_rejects_unknown_value(self):
        with self.assertRaises(ValueError):
            first_run.save_startup_mode(self.root, 'unknown')
        self.assertFalse((self.root / 'config/startup-mode.json').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_real_dpapi_roundtrip_and_no_plaintext_file(self):
        first_run.save_settings(self.root, self.key, self.values, keep_key=False)
        encrypted = self.root / 'config/provider-key.dpapi'
        self.assertEqual(read_encrypted(encrypted), self.key)
        self.assertNotIn(self.key.encode(), encrypted.read_bytes())
        self.assertFalse((self.root / 'config/provider-key.txt').exists())
        first_run.save_settings(self.root, '', dict(self.values, chat_calls='不限'), keep_key=True)
        self.assertEqual(read_encrypted(encrypted), self.key)
        self.assertIsNone(json.loads(self.limits.read_text())['chat_calls'])


if __name__ == '__main__':
    unittest.main()

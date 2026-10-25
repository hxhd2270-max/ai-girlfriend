import asyncio
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from fastapi import HTTPException
from server.admin import Settings, FIELDS, validate


class AdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raw = dict.fromkeys(FIELDS, '')
        self.raw.update({'昵称': '知夏', '核心特质': '尊重事实', '额外档案': '保留', 'voice_id': 'native'})
        self.cloud = SimpleNamespace(voice=SimpleNamespace(plan=True), unsafe=False)
        async def close():
            pass
        self.cloud.close = close
        self.settings = Settings(Path(self.tmp.name), Path(self.tmp.name), self.cloud, {}, SimpleNamespace(task=None))
        self.puts = []
        async def native(method='GET', payload=None):
            if method == 'PUT':
                self.puts.append(copy.deepcopy(payload))
                self.raw.update(payload)
                return {'success': True}
            return copy.deepcopy(self.raw)
        self.settings.native = native
        await self.settings.ensure()

    async def asyncTearDown(self):
        if self.settings.task and not self.settings.task.done():
            self.settings.task.cancel()
            await asyncio.gather(self.settings.task, return_exceptions=True)
        self.tmp.cleanup()

    async def test_apply_restore_and_preserve(self):
        original = copy.deepcopy(self.settings.state['active'])
        candidate = copy.deepcopy(original)
        candidate['persona']['核心特质'] = '坦率、自然'
        candidate['voice']['gain'] = .5
        await self.settings.queue(candidate, self.settings.state['revision'])
        await asyncio.wait_for(self.settings.task, 2)
        self.assertEqual(self.settings.state['status'], 'applied')
        self.assertEqual(self.puts[0]['额外档案'], '保留')
        self.assertNotIn('voice_id', self.puts[0])
        self.assertEqual(self.settings.voice_settings()['gain'], .5)
        await self.settings.queue(None, self.settings.state['revision'], restore=True)
        await asyncio.wait_for(self.settings.task, 2)
        self.assertEqual(self.settings.state['active'], original)

    async def test_busy_waits_and_conflict_rejects(self):
        self.settings.previewing = True
        candidate = copy.deepcopy(self.settings.state['active'])
        candidate['persona']['昵称'] = '测试'
        await self.settings.queue(candidate, self.settings.state['revision'])
        await asyncio.sleep(.01)
        self.assertEqual(self.settings.state['status'], 'pending')
        self.assertFalse(self.puts)
        self.settings.previewing = False
        await asyncio.wait_for(self.settings.task, 2)
        with self.assertRaises(HTTPException):
            await self.settings.queue(candidate, 'stale-revision')

    async def test_native_failure_is_not_success(self):
        candidate = copy.deepcopy(self.settings.state['active'])
        candidate['persona']['昵称'] = '测试'
        await self.settings.queue(candidate, self.settings.state['revision'])
        async def broken(*args):
            raise HTTPException(502, 'unavailable')
        self.settings.native = broken
        await asyncio.wait_for(self.settings.task, 2)
        self.assertEqual(self.settings.state['status'], 'error')
        self.assertEqual(self.settings.state['active']['persona']['昵称'], '知夏')

    async def test_reject_invalid_voice(self):
        candidate = copy.deepcopy(self.settings.state['active'])
        for value in (True, float('nan'), 2, -1):
            candidate['voice']['gain'] = value
            with self.assertRaises(HTTPException):
                validate(candidate)

    async def test_restore_after_provider_switch_keeps_current_voice(self):
        voice = self.cloud.voice
        voice.voice_binding = 'provider-one'
        voice.voice_default = {'id': 'one', 'label': 'one', 'gain': 1.0}
        candidate = copy.deepcopy(self.settings.state['active'])
        candidate['voice'] = voice.voice_default.copy()
        await self.settings.queue(candidate, self.settings.state['revision'])
        await asyncio.wait_for(self.settings.task, 2)
        voice.voice_binding = 'provider-two'
        voice.voice_default = {'id': 'two', 'label': 'two', 'gain': 1.0}
        self.assertEqual(self.settings.public()['active']['voice']['id'], 'two')
        await self.settings.queue(None, self.settings.state['revision'], restore=True)
        await asyncio.wait_for(self.settings.task, 2)
        self.assertEqual(self.settings.voice_settings()['id'], 'two')

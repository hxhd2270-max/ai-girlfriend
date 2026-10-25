import asyncio
import os
import tempfile
import unittest
import uuid
from pathlib import Path

_test_root = Path(__file__).resolve().parents[1] / 'data' / 'tests'
_test_root.mkdir(parents=True, exist_ok=True)
_tmp = tempfile.TemporaryDirectory(dir=_test_root, ignore_cleanup_errors=True)
os.environ['COMPANION_DATA_DIR'] = _tmp.name
os.environ['COMPANION_CLOUD_ENABLED'] = '0'
import server.app as app

class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous = app.CLOUD
        self.sid, self.turn = uuid.uuid4().hex, uuid.uuid4().hex
        app.DB.execute('INSERT INTO sessions(id) VALUES(?)', (self.sid,))
        app.DB.execute('INSERT INTO turns(id,session_id,user_text,status,created) VALUES(?,?,?,?,?)',
                       (self.turn,self.sid,'test','generating',0))
        app.DB.commit()
        self.rt = app.Runtime(self.sid)
        self.rt.active = self.turn
        self.events = []
        self.delivered = asyncio.Event()
        async def emit(kind,turn=None,segment=None,**kw):
            self.events.append((kind,segment,kw))
            if kind=='audio_segment': self.delivered.set()
        self.rt.emit = emit

    async def asyncTearDown(self): app.CLOUD = self.previous

    async def test_first_audio_arrives_before_text_stream_finishes_and_tail_once(self):
        outer=self
        class Provider:
            backend='neko'
            calls=[]
            async def stream_turn(self,*args):
                yield '第一句话超过四个字。'
                await asyncio.wait_for(outer.delivered.wait(),1)
                yield '第二句话没有句号'
            async def synthesize(self,text):
                self.calls.append(text)
                return dict(audio='YQ==',mime='audio/wav',durationMs=1)
        p=Provider();app.CLOUD=p
        await self.rt.generate_cloud(self.turn,'test',None)
        kinds=[e[0] for e in self.events]
        self.assertLess(kinds.index('audio_segment'),kinds.index('text_done'))
        self.assertEqual(p.calls,['第一句话超过四个字。','第二句话没有句号'])
        self.assertEqual([e[1] for e in self.events if e[0]=='audio_segment'],[0,1])

    async def test_cancel_during_generation_cancels_inflight_tts(self):
        started,cancelled=asyncio.Event(),asyncio.Event()
        class Provider:
            backend='neko'
            async def stream_turn(self,*args):
                yield '第一句话超过四个字。'
                await asyncio.Event().wait()
            async def synthesize(self,text):
                started.set()
                try: await asyncio.Event().wait()
                finally: cancelled.set()
        app.CLOUD=Provider()
        job=asyncio.create_task(self.rt.generate_cloud(self.turn,'test',None))
        await asyncio.wait_for(started.wait(),1)
        self.rt.active=None;job.cancel();await job
        self.assertTrue(cancelled.is_set())
        self.assertFalse(any(e[0]=='audio_segment' for e in self.events))

    async def test_later_tts_finishes_first_but_delivery_stays_ordered_and_bounded(self):
        text='第一句话足够长。第二句话足够长。第三句话足够长。第四句话足够长。尾巴'
        class Provider:
            backend='neko'
            calls=[]
            async def stream_turn(self,*args): yield text
            async def synthesize(self,text):
                self.calls.append(text)
                if text.startswith('第一'): await asyncio.sleep(.02)
                return dict(audio='YQ==',mime='audio/wav',durationMs=1)
        p=Provider();app.CLOUD=p
        await self.rt.generate_cloud(self.turn,'test',None)
        delivered=[e[2]['text'] for e in self.events if e[0]=='audio_segment']
        self.assertEqual(len(p.calls),3)
        self.assertEqual(''.join(delivered),text)
        self.assertEqual(delivered,p.calls)

    async def test_stop_cancels_audio_before_native_stream_teardown_finishes(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()
        teardown = asyncio.Event()
        release = asyncio.Event()

        class Provider:
            backend = 'neko'

            async def stream_turn(self, *args):
                try:
                    yield '第一句话超过四个字。'
                    await asyncio.Event().wait()
                finally:
                    teardown.set()
                    await release.wait()

            async def synthesize(self, text):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        app.CLOUD = Provider()
        job = asyncio.create_task(self.rt.generate_cloud(self.turn, 'test', None))
        self.rt.task = job
        try:
            await asyncio.wait_for(started.wait(), 1)
            await self.rt.cancel()
            await asyncio.wait_for(teardown.wait(), 1)
            await asyncio.wait_for(cancelled.wait(), 1)
            self.assertFalse(job.done(), 'native teardown still awaits its acknowledgement')
            self.assertFalse(any(e[0] == 'audio_segment' for e in self.events))
        finally:
            release.set()
            await asyncio.wait_for(job, 1)

if __name__=='__main__': unittest.main()

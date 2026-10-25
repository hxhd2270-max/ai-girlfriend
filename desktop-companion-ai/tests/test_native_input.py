import asyncio
from pathlib import Path
import unittest
from server.native_input import NativeInputBroker, run_dialog, checked_layout


class InlineLayoutTests(unittest.TestCase):
    def test_rejects_invalid_and_nonfinite_geometry(self):
        good = dict(left=50, top=400, width=500, height=110, viewportWidth=1000, viewportHeight=700, fontSize=13)
        self.assertEqual(checked_layout(good), good)
        for change in [dict(left=-1), dict(width=2000), dict(fontSize=float('nan')),
                       dict(top=True), dict(viewportWidth=0), dict(height=1)]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                checked_layout({**good, **change})

    def test_position_is_relative_to_real_host_client_and_dpi(self):
        from tools.native_chat_input import composer_bounds
        layout = dict(left=100, top=400, width=400, height=100, viewportWidth=1000, viewportHeight=700)
        self.assertEqual(composer_bounds(layout, (-1920, -200, 1500, 1050)),
                         (-1770, 400, 600, 150, 1.5))


class NativeInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_dialog_and_cancel_owner(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        delivered = []

        async def runner(script, draft, layout=None):
            self.assertEqual(draft, '草稿')
            started.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        async def deliver(result):
            delivered.append(result)

        broker = NativeInputBroker(Path(__file__).resolve().parents[1], runner)
        owner, stranger = object(), object()
        self.assertTrue(broker.open(owner, '草稿', deliver))
        await started.wait()
        self.assertFalse(broker.open(stranger, '', deliver))
        await broker.close(stranger)
        self.assertIs(broker.owner, owner)
        await broker.close(owner)
        self.assertTrue(stopped.is_set())
        self.assertEqual(delivered, [])
        self.assertIsNone(broker.task)
        # Cancellation before the task's first execution must also release it.
        self.assertTrue(broker.open(owner, '草稿', deliver))
        await broker.close(owner)
        self.assertIsNone(broker.task)

    async def test_delivery_and_fixed_error_release_dialog(self):
        delivered = []

        async def runner(script, draft, layout=None):
            return {'action':'draft', 'text':draft}

        async def deliver(result):
            delivered.append(result)

        broker = NativeInputBroker('.', runner)
        broker.open(object(), '中文草稿', deliver)
        await broker.task
        self.assertEqual(delivered, [{'action':'draft','text':'中文草稿'}])
        self.assertIsNone(broker.owner)

        async def fail(script, draft, layout=None):
            raise OSError('private details')

        broker.runner = fail
        broker.open(object(), '', deliver)
        await broker.task
        self.assertEqual(delivered[-1], {'action':'error'})

    async def test_process_does_not_inherit_secrets_and_cancel_terminates(self):
        from unittest.mock import patch
        entered = asyncio.Event()

        class Process:
            returncode = None
            terminated = False
            async def communicate(self, payload):
                self.payload = payload
                entered.set()
                await asyncio.Future()
            def terminate(self):
                self.terminated = True
                self.returncode = -1
            async def wait(self):
                return self.returncode

        process = Process()
        async def spawn(*args, **kwargs):
            self.assertNotIn('DASHSCOPE_API_KEY', kwargs['env'])
            self.assertNotIn('secret', ' '.join(args))
            return process

        with patch.dict('os.environ', {'DASHSCOPE_API_KEY':'secret'}), \
                patch('asyncio.create_subprocess_exec', spawn):
            task = asyncio.create_task(run_dialog('editor.py', '测试'))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(process.terminated)

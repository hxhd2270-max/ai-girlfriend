"""One authoritative native N.E.K.O. connection, with existing TTS playback."""
import asyncio
import json
from urllib.parse import quote

import websockets

from server.cloud import CloudClient, CloudError


class NekoClient:
    backend = 'neko'

    def __init__(self, *, connector=websockets.connect, voice=None, character='知夏'):
        self.connector = connector
        self.voice = voice or CloudClient(enabled=True)
        self.url = 'ws://127.0.0.1:49011/ws/' + quote(character)
        self.lock = asyncio.Lock()
        self.ws = None
        self.unsafe = False

    def usage_status(self):
        return self.voice.usage_status()

    async def synthesize(self, text):
        # Preserve the whole displayed answer. Never silently truncate speech.
        if len(text) > 240:
            raise CloudError('neko_reply_too_long')
        return await self.voice.synthesize(text)

    async def _event(self):
        raw = await self.ws.recv()
        if not isinstance(raw, str):
            raise CloudError('neko_protocol_error')
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise CloudError('neko_protocol_error')
        return event

    async def _connect(self):
        if self.unsafe:
            raise CloudError('neko_restart_required')
        if self.ws is not None:
            return
        self.ws = await self.connector(self.url, origin='http://127.0.0.1:49011',
            open_timeout=8, close_timeout=3, max_size=2**21, max_queue=64, proxy=None)
        await self.ws.send(json.dumps(dict(action='start_session', input_type='text',
            new_session=False, language='zh-CN', request_id='companion-start')))
        async with asyncio.timeout(35):
            while True:
                event = await self._event()
                if event.get('type') in ('session_failed', 'error'):
                    raise CloudError('neko_session_failed')
                if event.get('type') == 'session_started':
                    if event.get('request_id') != 'companion-start':
                        raise CloudError('neko_protocol_error')
                    return

    async def _disconnect(self):
        ws = self.ws
        if ws is None:
            return
        # Wait for the native teardown ACK before allowing a new session.
        # Closing a socket alone is not a native generation cancellation ACK.
        try:
            async with asyncio.timeout(18):
                await ws.send(json.dumps({'action': 'end_session', 'reason': 'user_stop'}))
                while True:
                    event = await self._event()
                    if event.get('type') == 'status':
                        message = event.get('message', '')
                        try:
                            value = json.loads(message) if isinstance(message, str) else message
                        except ValueError:
                            continue
                        if isinstance(value, dict) and value.get('code') == 'CHARACTER_LEFT':
                            break
        except (Exception, asyncio.CancelledError):
            self.unsafe = True
        finally:
            self.ws = None
            await ws.close()

    async def stream_turn(self, sid, turn, text):
        # sid is display/session identity only. The launcher's NEKO bootstrap
        # uses one persistent shared sid; no production context is injected.
        async with self.lock:
            completed = False
            try:
                await self._connect()
                await self.ws.send(json.dumps(dict(action='stream_data', input_type='text',
                    data=text, request_id=turn), ensure_ascii=False))
                total, native_turn = '', None
                async with asyncio.timeout(85):
                    while True:
                        event = await self._event()
                        kind = event.get('type')
                        if kind in ('error', 'session_failed'):
                            raise CloudError('neko_session_failed')
                        if event.get('request_id') != turn:
                            continue
                        if kind == 'response_discarded':
                            raise CloudError('neko_response_discarded')
                        if kind == 'gemini_response':
                            ident, delta = event.get('turn_id'), event.get('text')
                            if not ident or not isinstance(delta, str):
                                raise CloudError('neko_protocol_error')
                            if native_turn is not None and ident != native_turn:
                                raise CloudError('neko_protocol_error')
                            native_turn = ident
                            total += delta
                            if len(total) > 2000:
                                raise CloudError('neko_reply_too_long')
                            if delta:
                                yield delta
                        if kind == 'system' and event.get('data') == 'turn end':
                            if not total.strip():
                                raise CloudError('neko_empty_reply')
                            completed = True
                            return
            except asyncio.CancelledError:
                raise
            except CloudError:
                raise
            except TimeoutError:
                raise CloudError('neko_timeout') from None
            except Exception:
                raise CloudError('neko_unavailable') from None
            finally:
                if not completed:
                    await self._disconnect()

    async def close(self):
        async with self.lock:
            await self._disconnect()

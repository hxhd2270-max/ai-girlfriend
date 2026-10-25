"""Opt-in Qwen adapters. Importing this module never reads keys or opens sockets.

Limits are SQLite-backed Asia/Shanghai daily reservations, not a free balance
or billing guarantee. Configuration/storage failures block new requests.
Only the user's provider-side stop-on-free-exhaustion setting prevents paid use.
No retries, dotenv, credential discovery, or global logging changes.
Only local daily counters are persisted; no text, audio or credentials.
Use configure(enabled=True), or create_cloud_client(enabled=True) for an instance.
Close an abandoned stream with aclose() (or contextlib.aclosing).
"""
from __future__ import annotations

import asyncio
import array
import base64
import io
import json
import logging
import os
import uuid
import wave
from collections.abc import AsyncIterator, Sequence

import httpx
from websockets.asyncio.client import connect
from server import usage as _usage

CHAT_URL = 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'
CHAT_MODEL = 'qwen-flash'
TTS_MODEL = 'qwen3-tts-flash-realtime'
TTS_URL = f'wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model={TTS_MODEL}'
TTS_PARAMS = dict(voice='Cherry', mode='commit', language_type='Chinese',
                  response_format='pcm', sample_rate=24000, speech_rate=0.92, volume=45)
STYLE = '请用温柔、自然的中文口语回应，简短地说一两句话，尊重对方感受，不说教，不使用舞台动作或表情标记。'
CONNECT_TIMEOUT, TOTAL_TIMEOUT, IDLE_TIMEOUT = 10, 45, 10
MAX_MESSAGE, MAX_EVENTS, MAX_PCM_BYTES = 262144, 512, 30 * 24000 * 2
MAX_TEXT, MAX_TOKENS = 240, 180
_logger = logging.Logger('companion.cloud.private')
_logger.addHandler(logging.NullHandler())
_logger.propagate = False
_default_client = None
_blocked_reason = None


class CloudError(Exception):
    """Fixed local error code only; upstream details are never surfaced."""


def counters() -> dict[str, int]:
    """Return today's shared reservations, including failed/cancelled attempts."""
    return usage_status()['reserved']


def usage_status() -> dict:
    """Daily local usage/limits; available without enabling cloud or reading keys."""
    try:
        return _usage.usage_status()
    except _usage.UsageError as error:
        raise CloudError(str(error)) from None


def blocked_reason():
    """A fixed local category, never provider messages or credentials."""
    return _blocked_reason


def _gate():
    if _blocked_reason:
        raise CloudError(_blocked_reason)
    if os.environ.get('COMPANION_FREE_ONLY_CONFIRMED') != '1' and os.environ.get('COMPANION_PAID_TEST_AUTHORIZED') != '1':
        raise CloudError('free_confirmation_required')


def _upstream_error(error, fallback):
    """Only exact known codes leave this boundary; never expose provider text."""
    global _blocked_reason
    code = error.get('code') if isinstance(error, dict) else None
    mapped = {'AllocationQuota.FreeTierOnly': 'free_tier_exhausted',
              'Arrearage': 'account_arrearage',
              'Throttling': 'provider_rate_limited',
              'Throttling.RateQuota': 'provider_rate_limited'}.get(code) if isinstance(code, str) else None
    if mapped in {'free_tier_exhausted', 'account_arrearage'}:
        _blocked_reason = mapped
    return CloudError(mapped or fallback)


def _key():
    _gate()
    key = os.environ.get('DASHSCOPE_API_KEY')
    if not key or not key.isascii() or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in key):
        raise CloudError('credential_required_or_invalid')
    return key


def _reserve(kind, estimate=0):
    _gate()
    try:
        _usage.reserve(kind, estimate)
    except _usage.UsageError as error:
        raise CloudError(str(error)) from None


def _messages(messages):
    if not isinstance(messages, (list, tuple)) or not 1 <= len(messages) <= 100:
        raise CloudError('invalid_messages')
    result = []
    if not any(isinstance(message, dict) and message.get('role') == 'system' for message in messages):
        result.append({'role': 'system', 'content': STYLE})
    size = 0
    for message in messages:
        if not isinstance(message, dict) or message.get('role') not in ('system', 'user', 'assistant'):
            raise CloudError('invalid_messages')
        content = message.get('content')
        if not isinstance(content, str) or not content.strip():
            raise CloudError('invalid_messages')
        size += len(content)
        if size > 16000:
            raise CloudError('context_limit')
        result.append(dict(role=message['role'], content=content))
    return result


async def _sse(response):
    """Bound buffering even for a peer that never sends a line separator."""
    buffer = b''
    data = []
    event_size = events = total = 0
    async for chunk in response.aiter_bytes(chunk_size=4096):
        total += len(chunk)
        if total > 2 * 1024 * 1024:
            raise CloudError('chat_stream_limit')
        buffer += chunk
        while b'\n' in buffer:
            line, buffer = buffer.split(b'\n', 1)
            if len(line) > 65536:
                raise CloudError('chat_event_limit')
            line = line.rstrip(b'\r')
            if not line:
                if data:
                    events += 1
                    if events > 2048:
                        raise CloudError('chat_event_limit')
                    yield b'\n'.join(data).decode('utf-8')
                data, event_size = [], 0
            elif line.startswith(b'data:'):
                value = line[5:].lstrip(b' ')
                event_size += len(value)
                if event_size > 65536:
                    raise CloudError('chat_event_limit')
                data.append(value)
        if len(buffer) > 65536:
            raise CloudError('chat_event_limit')
    # EOF without [DONE] is handled as incomplete by the caller.


async def _tts_protocol(ws, text):
    count = 0

    async def recv():
        nonlocal count
        count += 1
        if count > MAX_EVENTS:
            raise CloudError('tts_event_limit')
        raw = await asyncio.wait_for(ws.recv(), IDLE_TIMEOUT)
        if not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_MESSAGE:
            raise CloudError('tts_invalid_event')
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise CloudError('tts_invalid_event')
        if event.get('type') == 'error':
            raise _upstream_error(event.get('error', event), 'tts_upstream_error')
        return event

    async def send(kind, **fields):
        await ws.send(json.dumps(dict(event_id=str(uuid.uuid4()), type=kind, **fields), ensure_ascii=False))

    if (await recv()).get('type') != 'session.created':
        raise CloudError('tts_session_order')
    await send('session.update', session=TTS_PARAMS.copy())
    updated = await recv()
    if updated.get('type') != 'session.updated':
        raise CloudError('tts_session_order')
    session = updated.get('session')
    if not isinstance(session, dict) or session.get('model') != TTS_MODEL:
        raise CloudError('tts_session_mismatch')
    for key, expected in TTS_PARAMS.items():
        actual = session.get(key)
        if key == 'language_type' and isinstance(actual, str):
            actual, expected = actual.casefold(), expected.casefold()
        if isinstance(actual, bool) or actual != expected:
            raise CloudError('tts_session_mismatch')
    await send('input_text_buffer.append', text=text)
    await send('input_text_buffer.commit')
    pcm = bytearray()
    response_id = None
    audio_done = response_done = False
    reported = None
    while True:
        event = await recv()
        kind = event.get('type')
        if kind == 'response.created':
            if response_id is not None or response_done:
                raise CloudError('tts_response_order')
            response_id = event.get('response', {}).get('id')
            if not isinstance(response_id, str) or not response_id:
                raise CloudError('tts_response_order')
        elif kind in ('response.audio.delta', 'response.audio.done', 'response.done'):
            incoming = event.get('response', {}).get('id') if kind == 'response.done' else event.get('response_id')
            if response_id is None or incoming != response_id or response_done:
                raise CloudError('tts_response_order')
            if kind == 'response.audio.delta':
                if audio_done:
                    raise CloudError('tts_response_order')
                chunk = base64.b64decode(event['delta'], validate=True)
                if len(pcm) + len(chunk) > MAX_PCM_BYTES:
                    raise CloudError('tts_audio_limit')
                pcm.extend(chunk)
            elif kind == 'response.audio.done':
                if audio_done:
                    raise CloudError('tts_response_order')
                audio_done = True
            else:
                if not audio_done or event['response'].get('status') != 'completed':
                    raise CloudError('tts_response_incomplete')
                response_done = True
                usage = event['response'].get('usage', {}).get('characters')
                if type(usage) is int and 0 <= usage <= 1000000:
                    reported = usage
                await send('session.finish')
        elif kind == 'session.finished':
            if not response_done or not pcm or len(pcm) % 2:
                raise CloudError('tts_invalid_pcm')
            return bytes(pcm), reported
        elif kind not in {'input_text_buffer.committed', 'response.output_item.added',
                          'response.content_part.added', 'response.content_part.done',
                          'response.output_item.done'}:
            raise CloudError('tts_unexpected_event')


class CloudClient:
    def __init__(self, *, enabled: bool = False):
        if enabled is not True:
            raise CloudError('cloud_disabled')
        _gate()
        self.plan = os.environ.get('COMPANION_PROVIDER') == 'token-plan'
        self.chat_model = os.environ.get('COMPANION_CHAT_MODEL', 'qwen3.8-flash') if self.plan else CHAT_MODEL
        if self.plan and self.chat_model not in {'auto','qwen3.8-flash','qwen3.8-max','qwen3.7-plus','qwen3.7-max','qwen3.6-flash'}:
            raise CloudError('invalid_chat_model')
        # Read credentials only at an explicitly authorized call, never store them.

    async def stream_reply(self, messages: Sequence[dict[str, str]], *,
                           max_tokens: int = MAX_TOKENS) -> AsyncIterator[str]:
        if type(max_tokens) is not int or not 1 <= max_tokens <= MAX_TOKENS:
            raise CloudError('invalid_max_tokens')
        context = _messages(messages)
        routes = getattr(self, 'routes', None)
        protocol = routes.route('conversation')['protocol'] if routes else 'chat-completions'
        key = '' if routes else _key()
        if routes:
            _gate()
        _reserve('chat')
        payload = dict(model=self.chat_model, messages=context, stream=True,
                       max_tokens=max_tokens, enable_thinking=False)
        if routes:
            payload = routes.chat_payload('conversation', payload)
        try:
            async with asyncio.timeout(TOTAL_TIMEOUT):
                async with httpx.AsyncClient(
                    trust_env=False, follow_redirects=False,
                    timeout=httpx.Timeout(IDLE_TIMEOUT, connect=CONNECT_TIMEOUT),
                    transport=getattr(self, 'transport', None) or httpx.AsyncHTTPTransport(retries=0, trust_env=False),
                ) as client:
                    async with client.stream('POST', routes.endpoint('conversation') if routes else ('https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions' if self.plan else CHAT_URL),
                        headers=routes.headers('conversation') if routes else {'Authorization': 'Bearer ' + key},
                        json=payload,
                    ) as response:
                        if response.status_code != 200:
                            # Bound the error body too; do not log or surface it.
                            body = bytearray()
                            async for part in response.aiter_bytes(chunk_size=4096):
                                body.extend(part)
                                if len(body) > 65536:
                                    raise CloudError('chat_upstream_error')
                            try:
                                detail = json.loads(body)
                            except (ValueError, UnicodeError):
                                detail = {}
                            if isinstance(detail, dict):
                                detail = detail.get('error', detail)
                            raise _upstream_error(detail, 'chat_upstream_error')
                        if response.headers.get('content-type', '').split(';')[0].strip().lower() != 'text/event-stream':
                            raise CloudError('chat_invalid_stream')
                        length = 0
                        async for data in _sse(response):
                            if data == '[DONE]':
                                return
                            event = json.loads(data)
                            if not isinstance(event, dict):
                                raise CloudError('chat_upstream_error')
                            if 'error' in event:
                                raise _upstream_error(event['error'], 'chat_upstream_error')
                            delta = None
                            finished = False
                            if protocol in ('chat-completions', 'token-plan'):
                                choices = event.get('choices')
                                if not isinstance(choices, list):
                                    raise CloudError('chat_invalid_event')
                                if not choices:  # optional usage-only frame
                                    continue
                                if len(choices) != 1 or choices[0].get('index', 0) != 0:
                                    raise CloudError('chat_invalid_event')
                                delta = choices[0]['delta'].get('content')
                            elif protocol == 'anthropic-messages':
                                kind = event.get('type')
                                if kind == 'content_block_delta':
                                    block = event.get('delta')
                                    delta = block.get('text') if isinstance(block, dict) else None
                                elif kind == 'message_stop':
                                    return
                                elif kind in ('message_start', 'content_block_start', 'content_block_stop', 'ping'):
                                    continue
                                else:
                                    raise CloudError('chat_invalid_event')
                            elif protocol == 'responses':
                                kind = event.get('type')
                                if kind == 'response.output_text.delta':
                                    delta = event.get('delta')
                                elif kind in ('response.completed', 'response.done'):
                                    return
                                elif kind == 'response.failed':
                                    raise CloudError('chat_upstream_error')
                                elif kind in ('response.created', 'response.in_progress', 'response.output_item.added',
                                              'response.content_part.added', 'response.content_part.done',
                                              'response.output_item.done', 'response.output_text.done',
                                              'response.incomplete'):
                                    continue
                                else:
                                    raise CloudError('chat_invalid_event')
                            elif protocol == 'gemini-content':
                                candidates = event.get('candidates')
                                if not isinstance(candidates, list) or len(candidates) > 1:
                                    raise CloudError('chat_invalid_event')
                                if not candidates:
                                    continue
                                candidate = candidates[0]
                                content = candidate.get('content', {})
                                parts = content.get('parts', []) if isinstance(content, dict) else []
                                if not isinstance(parts, list):
                                    raise CloudError('chat_invalid_event')
                                delta = ''.join(part.get('text', '') for part in parts
                                                 if isinstance(part, dict) and isinstance(part.get('text'), str))
                                finished = bool(candidate.get('finishReason'))
                            else:
                                raise CloudError('chat_invalid_event')
                            if delta is None:
                                if finished:
                                    return
                                continue
                            if not isinstance(delta, str):
                                raise CloudError('chat_invalid_event')
                            delta = delta[:MAX_TEXT - length]
                            if delta:
                                length += len(delta)
                                yield delta
                            if length >= MAX_TEXT:
                                return
                            if finished:
                                return
                        raise CloudError('chat_incomplete')
        except CloudError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise CloudError('chat_timeout') from None
        except Exception:
            raise CloudError('chat_transport_or_protocol_error') from None

    async def synthesize(self, text: str, *, voice_settings=None) -> dict:
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise CloudError('invalid_tts_text')
        # Same conservative ordinary-text estimate as the verified audition tool.
        estimate = len(text) + sum('\u4e00' <= c <= '\u9fff' for c in text)
        key = _key()
        _reserve('tts', estimate)
        if self.plan:
            try:
                settings = voice_settings or (self.settings_provider() if getattr(self, 'settings_provider', None) else None)
                return await asyncio.wait_for(self._plan_tts(text, key, estimate, settings), TOTAL_TIMEOUT)
            except TimeoutError:
                raise CloudError('tts_timeout') from None
        try:
            async with asyncio.timeout(TOTAL_TIMEOUT):
                async with connect(TTS_URL, additional_headers={'Authorization': 'Bearer ' + key},
                                   proxy=None, open_timeout=CONNECT_TIMEOUT, close_timeout=2,
                                   max_size=MAX_MESSAGE, max_queue=8, logger=_logger) as ws:
                    pcm, reported = await _tts_protocol(ws, text)
            audio = io.BytesIO()
            with wave.open(audio, 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes(pcm)
            return dict(audio=base64.b64encode(audio.getvalue()).decode('ascii'), mime='audio/wav',
                        durationMs=len(pcm) * 1000 / 48000,
                        usage=dict(estimatedCharacters=estimate, reportedCharacters=reported,
                                   freeBalanceVerified=False, finalBillKnown=False))
        except CloudError:
            raise
        except TimeoutError:
            raise CloudError('tts_timeout') from None
        except Exception:
            raise CloudError('tts_transport_or_protocol_error') from None

    async def _plan_tts(self, text, key, estimate, settings=None, route=None, transport=None):
        settings = settings or {'id': 'longanhuan_v3.6', 'gain': 1.0}
        try:
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=TOTAL_TIMEOUT, **({'transport': transport} if transport is not None else {})) as client:
                response = await client.post(route['endpoint'] if route else 'https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer',
                    headers=route['headers'] if route else {'Authorization':'Bearer '+key}, json={'model':route['model'] if route else 'qwen-audio-3.0-tts-plus',
                    'input':{'text':text,'voice':settings['id'],'format':'wav','sample_rate':24000}})
                if len(response.content) > 6*1024*1024:
                    raise CloudError('tts_audio_limit')
                body = response.json()
                if response.status_code != 200:
                    raise _upstream_error(body.get('error',body), 'tts_upstream_error')
                encoded = body['output']['audio']['data']
                if encoded:
                    audio = base64.b64decode(encoded, validate=True)
                else:
                    from urllib.parse import urlsplit
                    url = body['output']['audio']['url']
                    parts = urlsplit(url)
                    import re
                    if parts.scheme not in ('http','https') or not re.fullmatch(r'dashscope-result-[a-z0-9-]+\.oss-cn-beijing\.aliyuncs\.com', parts.hostname or '') or parts.port not in (None,443) or parts.username:
                        raise CloudError('tts_invalid_audio_url')
                    url = parts._replace(scheme='https').geturl()
                    audio = bytearray()
                    async with client.stream('GET',url) as download:
                        if download.status_code != 200:
                            raise CloudError('tts_upstream_error')
                        async for chunk in download.aiter_bytes():
                            audio.extend(chunk)
                            if len(audio)>MAX_PCM_BYTES+65536:
                                raise CloudError('tts_audio_limit')
                    audio = bytes(audio)
                with wave.open(io.BytesIO(audio)) as wav:
                    if wav.getnchannels()!=1 or wav.getsampwidth()!=2 or wav.getframerate()!=24000:
                        raise CloudError('tts_invalid_pcm')
                    pcm = wav.readframes(30*24000+1)
                    if not pcm or len(pcm)%2 or len(pcm)>MAX_PCM_BYTES:
                        raise CloudError('tts_invalid_pcm')
                    duration = len(pcm)*1000/48000
                if settings['gain'] != 1:
                    samples = array.array('h')
                    samples.frombytes(pcm)
                    scale = settings['gain']
                    for index, sample in enumerate(samples):
                        samples[index] = max(-32768, min(32767, round(sample * scale)))
                    pcm = samples.tobytes()
                normalized = io.BytesIO()
                with wave.open(normalized,'wb') as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(24000)
                    wav.writeframes(pcm)
                audio = normalized.getvalue()
                return dict(audio=base64.b64encode(audio).decode('ascii'), mime='audio/wav',durationMs=duration,
                    usage=dict(estimatedCharacters=estimate,reportedCharacters=body.get('usage',{}).get('characters'),freeBalanceVerified=False,finalBillKnown=False))
        except CloudError:
            raise
        except Exception:
            raise CloudError('tts_transport_or_protocol_error') from None

    def counters(self) -> dict[str, int]:
        return counters()

    def usage_status(self) -> dict:
        return usage_status()


def create_cloud_client(*, enabled: bool = False) -> CloudClient | None:
    return CloudClient(enabled=True) if enabled is True else None


def configure(*, enabled: bool = False) -> CloudClient | None:
    global _default_client
    _default_client = create_cloud_client(enabled=enabled)
    return _default_client


async def stream_reply(messages: Sequence[dict[str, str]], *,
                       max_tokens: int = MAX_TOKENS) -> AsyncIterator[str]:
    if _default_client is None:
        raise CloudError('cloud_disabled')
    # Forwarding generators need an explicit finally to propagate aclose().
    stream = _default_client.stream_reply(messages, max_tokens=max_tokens)
    try:
        async for delta in stream:
            yield delta
    finally:
        await stream.aclose()


async def synthesize(text: str) -> dict:
    if _default_client is None:
        raise CloudError('cloud_disabled')
    return await _default_client.synthesize(text)

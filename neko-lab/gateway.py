"""Bounded native OpenAI gateway; S1 and daily integration supply separate ledgers."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

MODEL = 'qwen3.8-flash'
ENDPOINT = 'https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions'
LOCAL_TOKEN = 'neko-isolated-s1-loopback'  # Not a provider credential.
POLICY = dict(version=1, foreground=12, background=20, total=32, concurrency=1,
              timeout=60, max_tokens=3000, max_body_bytes=196608,
              conservative_input_tokens=60000, retries=0)
TASKS = {'conversation': 'foreground', 'summary': 'background',
         'correction': 'background', 'emotion': 'background'}


class Denied(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status


class Ledger:
    def __init__(self, path: Path, policy=None):
        self.path, self.policy = path, dict(policy or POLICY)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS policy (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (
                  request_id TEXT PRIMARY KEY, category TEXT NOT NULL, task TEXT NOT NULL,
                  body_sha256 TEXT NOT NULL, started REAL NOT NULL, finished REAL,
                  status TEXT NOT NULL, model TEXT NOT NULL, usage TEXT);
            ''')
            encoded = json.dumps(self.policy, sort_keys=True)
            db.execute('INSERT OR IGNORE INTO policy VALUES(1,?)', (encoded,))
            if db.execute('SELECT value FROM policy WHERE id=1').fetchone()[0] != encoded:
                raise RuntimeError('Persistent experiment policy differs; refusing to reset limits')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        try:
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    def reserve(self, request_id, category, task, digest):
        try:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute('SELECT 1 FROM attempts WHERE request_id=? OR body_sha256=?',
                              (request_id, digest)).fetchone():
                    raise Denied('DUPLICATE_ATTEMPT', 409)
                counts = dict(db.execute('SELECT category,COUNT(*) FROM attempts GROUP BY category'))
                if counts.get(category, 0) >= self.policy[category] or sum(counts.values()) >= self.policy['total']:
                    raise Denied('EXPERIMENT_LIMIT', 429)
                db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,NULL,?,?,NULL)',
                           (request_id, category, task, digest, time.time(), 'reserved', MODEL))
        except sqlite3.Error:
            raise Denied('LEDGER_UNAVAILABLE', 503) from None

    def finish(self, request_id, status, usage=None):
        try:
            with self.connect() as db:
                db.execute('UPDATE attempts SET finished=?,status=?,usage=? WHERE request_id=?',
                           (time.time(), status, json.dumps(usage) if usage else None, request_id))
        except sqlite3.Error:
            raise Denied('LEDGER_UNAVAILABLE', 503) from None

    def snapshot(self):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            return {'policy': self.policy, 'attempts': [dict(r) for r in
                    db.execute('SELECT * FROM attempts ORDER BY started')]}


def normalize(task, body):
    if task not in TASKS:
        raise Denied('TASK_DISABLED', 403)
    if not isinstance(body, dict) or body.get('model') != MODEL:
        raise Denied('MODEL_DISABLED')
    messages = body.get('messages')
    if not isinstance(messages, list) or not 1 <= len(messages) <= 200:
        raise Denied('INVALID_MESSAGES')
    for msg in messages:
        if not isinstance(msg, dict) or msg.get('role') not in ('system', 'user', 'assistant', 'tool', 'developer'):
            raise Denied('INVALID_MESSAGE')
        content = msg.get('content')
        if content is not None and not isinstance(content, (str, list)):
            raise Denied('INVALID_CONTENT')
        if isinstance(content, list) and any(not isinstance(p, dict) or p.get('type') != 'text'
                                              or not isinstance(p.get('text'), str) for p in content):
            raise Denied('MULTIMODAL_DISABLED', 403)
        if any(k in msg for k in ('audio', 'image', 'images')):
            raise Denied('MULTIMODAL_DISABLED', 403)
    # Byte-level BPE uses at most one token per UTF-8 byte. JSON and per-message
    # framing overhead make this deliberately conservative, without downloading a tokenizer.
    token_ceiling = len(json.dumps(messages, ensure_ascii=False).encode('utf-8')) + 32 * len(messages)
    token_ceiling += len(json.dumps(body.get('tools', []), ensure_ascii=False).encode('utf-8'))
    if token_ceiling > POLICY['conservative_input_tokens']:
        raise Denied('INPUT_LIMIT', 413)
    limit = body.get('max_completion_tokens', body.get('max_tokens', 3000))
    if type(limit) is not int or limit <= 0:
        raise Denied('INVALID_OUTPUT_LIMIT')
    if type(body.get('stream', False)) is not bool:
        raise Denied('INVALID_STREAM')
    payload = {'model': MODEL, 'messages': messages, 'stream': body.get('stream', False),
               'max_tokens': min(limit, POLICY['max_tokens']), 'enable_thinking': False}
    for name in ('tools', 'tool_choice', 'response_format', 'parallel_tool_calls'):
        if name in body:
            payload[name] = body[name]
    if payload['stream']:
        payload['stream_options'] = {'include_usage': True}
    return payload


def create_app(ledger: Ledger, key: str, transport=None, total_timeout=None,
               request_ids=None, app_id='NEKO-S1-GATEWAY', queue_timeout=None, routes=None):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    lock = asyncio.Lock()
    app.state.halted = False
    timeout = POLICY['timeout'] if total_timeout is None else total_timeout

    def error(code, status):
        return JSONResponse({'error': {'message': code, 'type': 'neko_lab_gate', 'code': code}}, status_code=status)

    @app.get('/health')
    async def health():
        try:
            state = ledger.snapshot()
            return {'app': app_id, 'status': 'halted' if app.state.halted else 'ok',
                    'counts': {k: sum(a['category'] == k for a in state['attempts'])
                               for k in ('foreground', 'background')}, 'policy': state['policy']}
        except Exception:
            return error('LEDGER_UNAVAILABLE', 503)

    @app.post('/{task}/v1/chat/completions')
    async def completions(task: str, request: Request):
        if request.headers.get('authorization') != 'Bearer ' + LOCAL_TOKEN:
            return error('LOCAL_AUTH_REQUIRED', 401)
        if app.state.halted:
            return error('GATEWAY_HALTED', 503)
        try:
            raw = bytearray()
            async for part in request.stream():
                raw.extend(part)
                if len(raw) > POLICY['max_body_bytes']:
                    raise Denied('BODY_LIMIT', 413)
            payload = normalize(task, json.loads(raw))
            if routes is not None:
                payload = routes.chat_payload(task, payload)
            digest = hashlib.sha256(task.encode() + b'\0' +
                      json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            request_id = request.headers.get('x-neko-request-id') or (request_ids() if request_ids else digest)
            if not 1 <= len(request_id) <= 160 or not request_id.isascii():
                raise Denied('INVALID_REQUEST_ID')
            if queue_timeout is None:
                if lock.locked():
                    raise Denied('UPSTREAM_BUSY', 409)
                await lock.acquire()
            else:
                try:
                    await asyncio.wait_for(lock.acquire(), queue_timeout)
                except TimeoutError:
                    raise Denied('UPSTREAM_BUSY', 409) from None
            try:
                ledger.reserve(request_id, TASKS[task], task, digest)
                if routes is not None:
                    with ledger.connect() as db:
                        db.execute('UPDATE attempts SET model=? WHERE request_id=?',
                                   (payload['model'], request_id))
            except sqlite3.Error:
                app.state.halted = True
                lock.release()
                raise Denied('LEDGER_UNAVAILABLE', 503) from None
            except BaseException:
                lock.release()
                raise
        except Denied as exc:
            return error(exc.code, exc.status)
        except (ValueError, UnicodeError):
            return error('INVALID_JSON', 400)

        async def forward():
            status, usage, response = 'failed', None, None
            client = httpx.AsyncClient(transport=transport or httpx.AsyncHTTPTransport(retries=0),
                                       timeout=timeout, trust_env=False, follow_redirects=False)
            try:
                async with asyncio.timeout(timeout):
                    headers = routes.headers(task) if routes is not None else {'Authorization': 'Bearer ' + key}
                    headers['Accept'] = 'text/event-stream' if payload['stream'] else 'application/json'
                    upstream_request = client.build_request('POST', routes.endpoint(task) if routes is not None else ENDPOINT,
                                                           json=payload, headers=headers)
                    response = await client.send(upstream_request, stream=True)
                    if response.status_code != 200:
                        status = 'upstream_http_' + str(response.status_code)
                        raise Denied('UPSTREAM_REJECTED', 502)
                    if not payload['stream']:
                        chunks, size = [], 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > 2_000_000:
                                raise Denied('RESPONSE_LIMIT', 502)
                            chunks.append(chunk)
                        data = b''.join(chunks)
                        value = json.loads(data)
                        usage = safe_usage(value.get('usage'))
                        status = 'ok'
                        yield data
                    else:
                        size, done = 0, False
                        async for line in response.aiter_lines():
                            size += len(line.encode('utf-8'))
                            if size > 2_000_000:
                                raise Denied('RESPONSE_LIMIT', 502)
                            if line.startswith('data:'):
                                data = line[5:].strip()
                                if data == '[DONE]':
                                    done = True
                                elif data:
                                    parsed = json.loads(data)
                                    usage = safe_usage(parsed.get('usage')) or usage
                            yield (line + '\n').encode('utf-8')
                        if not done:
                            raise Denied('INCOMPLETE_STREAM', 502)
                        status = 'ok'
            except asyncio.CancelledError:
                status = 'cancelled'
                raise
            except (TimeoutError, httpx.TimeoutException):
                status = 'timeout'
                raise Denied('UPSTREAM_TIMEOUT', 504) from None
            except Denied:
                raise
            except Exception:
                raise Denied('UPSTREAM_FAILURE', 502) from None
            finally:
                try:
                    if response is not None:
                        await response.aclose()
                    await client.aclose()
                finally:
                    # Cleanup can itself be cancelled or raise. Reservations
                    # and the single-flight lock still have to settle.
                    try:
                        ledger.finish(request_id, status, usage)
                    except Denied:
                        app.state.halted = True
                    finally:
                        lock.release()

        if not payload['stream']:
            try:
                parts = [part async for part in forward()]
                return Response(b''.join(parts), media_type='application/json')
            except Denied as exc:
                return error(exc.code, exc.status)

        async def safe_stream():
            try:
                async for part in forward():
                    yield part
            except Denied as exc:
                # Streaming headers may already have been sent. Omit [DONE] on
                # failure and return a sanitized OpenAI-compatible error event.
                yield ('data: ' + json.dumps({'error': {'message': exc.code, 'code': exc.code}}) + '\n\n').encode()
        return StreamingResponse(safe_stream(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
    return app


def safe_usage(value):
    if not isinstance(value, dict):
        return None
    return {k: v for k, v in value.items() if k in ('prompt_tokens', 'completion_tokens', 'total_tokens')
            and type(v) is int and 0 <= v <= 10_000_000} or None

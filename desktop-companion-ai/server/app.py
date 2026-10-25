"""Loopback companion service with explicit simulation and cloud modes."""
import asyncio
import base64
import io
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import struct
import time
import uuid
import wave
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get('COMPANION_DATA_DIR', str(ROOT / 'data')))
DATA.mkdir(parents=True, exist_ok=True)
DB = sqlite3.connect(DATA / 'sessions.sqlite3', check_same_thread=False)
DB.row_factory = sqlite3.Row
DB.executescript('''
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, seq INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS turns(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL, user_text TEXT NOT NULL,
 generated_text TEXT NOT NULL DEFAULT '', displayed_text TEXT NOT NULL DEFAULT '',
 played_text TEXT NOT NULL DEFAULT '', play_started_text TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL, origin_turn_id TEXT, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS wallpaper_pairs(
 digest TEXT PRIMARY KEY, session_id TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS wallpaper_chat_pairs(
 digest TEXT PRIMARY KEY, session_id TEXT NOT NULL, expires REAL NOT NULL);
''')
# A stopped server cannot still be speaking. Never replay persisted audio.
DB.execute("UPDATE turns SET status='interrupted' WHERE status IN ('generating','buffering','speaking')")
DB.commit()
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
ORIGIN = 'http://127.0.0.1:8765'
COOKIE = 'companion_session'
runtimes = {}
WALLPAPER_DIR = ROOT.parent / 'PURPLE-Enhanced'
PAIR_LIFETIME = 30 * 86400
from server.native_input import NativeInputBroker, checked_layout
NATIVE_INPUT = NativeInputBroker(ROOT)
from server.voice import VoiceBroker
VOICE = VoiceBroker()
from server.persona import build_system_prompt
CLOUD = None
LAST_CLOUD_ERROR = None
from server.provider_routes import RuntimeRoutes
ROUTES = RuntimeRoutes.load(ROOT) if os.environ.get('COMPANION_CLOUD_ENABLED') == '1' else None
if os.environ.get("COMPANION_CLOUD_ENABLED") == "1":
    from server.cloud import CloudClient
    if ROUTES:
        from server.provider_audio import RoutedVoice, RoutedRecognizer
        CLOUD = RoutedVoice(ROUTES)
        VOICE.recognizer = RoutedRecognizer(ROUTES)
    else:
        CLOUD = CloudClient(enabled=True)
    if os.environ.get('COMPANION_DIALOGUE_BACKEND') == 'neko':
        from server.neko import NekoClient
        CLOUD = NekoClient(voice=CLOUD)
        DB.execute('CREATE TABLE IF NOT EXISTS backend_sessions(backend TEXT PRIMARY KEY, session_id TEXT NOT NULL)')
        row = DB.execute('SELECT session_id FROM backend_sessions WHERE backend=?', ('neko',)).fetchone()
        if row is None:
            sid = secrets.token_urlsafe(32)
            DB.execute('INSERT INTO sessions(id) VALUES(?)', (sid,))
            DB.execute('INSERT INTO backend_sessions VALUES(?,?)', ('neko', sid))
        else:
            sid = row[0]
        # Existing local wallpaper capabilities join the new shared conversation.
        # Old display turns remain in their original sessions, never imported.
        DB.execute('UPDATE wallpaper_pairs SET session_id=?', (sid,))
        DB.execute('UPDATE wallpaper_chat_pairs SET session_id=?', (sid,))
        DB.commit()


from server.admin import Settings, create_router as create_admin_router
SETTINGS = Settings(ROOT, DATA, CLOUD, runtimes, VOICE)
if getattr(CLOUD, 'backend', None) == 'neko':
    CLOUD.voice.settings_provider = SETTINGS.voice_settings
elif ROUTES:
    CLOUD.settings_provider = SETTINGS.voice_settings


@app.on_event('shutdown')
async def close_dialogue():
    if SETTINGS.task and not SETTINGS.task.done():
        SETTINGS.task.cancel()
        try:
            await SETTINGS.task
        except asyncio.CancelledError:
            pass
    await VOICE.close(VOICE.owner)
    if getattr(CLOUD, 'backend', None) == 'neko':
        await CLOUD.close()


def usage_status():
    if CLOUD is None:
        return {'mode': 'simulation'}
    getter = getattr(CLOUD, 'usage_status', None)
    try:
        return getter() if callable(getter) else {'mode': 'unavailable'}
    except Exception:
        # Status failures must not take down playback or expose storage details.
        return {'mode': 'unavailable'}


def voice_available():
    return (getattr(CLOUD, 'backend', None) == 'neko'
            and os.environ.get('COMPANION_PROVIDER') == 'token-plan' and os.name == 'nt')


def context_messages(sid, current_turn):
    messages = [{"role": "system", "content": build_system_prompt(DATA)}]
    rows = DB.execute("SELECT * FROM (SELECT rowid,* FROM turns WHERE session_id=? AND id<>? AND origin_turn_id IS NULL ORDER BY rowid DESC LIMIT 8) ORDER BY rowid", (sid,current_turn)).fetchall()
    for row in rows:
        # Legacy demo replies (including copies echoed by a cloud model) are
        # fixtures, not conversation context. Preserve them in visible history.
        if row["generated_text"].startswith('这是本地模拟回复，收到了：'):
            continue
        messages.append({"role":"user", "content":row["user_text"]})
        text = row["generated_text"] if row["status"] == "completed" else row["played_text"]
        if text:
            messages.append({"role":"assistant", "content":text + (" [此前回答被中断]" if row["status"] != "completed" else "")})
    return messages


def spoken_sentences(text):
    """Split a reply into a few natural TTS units without rewriting its words."""
    if not isinstance(text, str):
        return []
    text = text.strip()
    if not text:
        return []
    # Keep closing punctuation with the sentence.  The response contract is
    # short, so a bounded list is enough and avoids a request per clause.
    parts = []
    start = 0
    for index, char in enumerate(text):
        if char in '。！？!?；;':
            part = text[start:index + 1].strip()
            if part:
                parts.append(part)
            start = index + 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    if not parts:
        parts = [text]
    # Very short fragments sound worse as isolated requests. Join them while
    # retaining the original punctuation and cap the request count.
    merged = []
    for part in parts:
        if merged and len(part) < 4:
            merged[-1] += part
        else:
            merged.append(part)
    if len(merged) <= 3:
        return merged
    return merged[:2] + [''.join(merged[2:])]


def valid_session(sid):
    return bool(sid and DB.execute('SELECT 1 FROM sessions WHERE id=?', (sid,)).fetchone())


@app.middleware('http')
async def guard(request: Request, call_next):
    if request.headers.get('host') != '127.0.0.1:8765':
        return JSONResponse({'detail': '仅允许本机指定地址'}, status_code=403)
    if request.method not in ('GET', 'HEAD') and request.headers.get('origin') != ORIGIN:
        return JSONResponse({'detail': '来源不允许'}, status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; connect-src 'self' ws://127.0.0.1:8765; media-src 'self' blob:; style-src 'self'; script-src 'self'; frame-ancestors 'none'"
    if request.url.path.startswith('/api'):
        response.headers['Cache-Control'] = 'no-store'
    return response


class Runtime:
    def __init__(self, sid):
        self.sid = sid
        self.owner = None
        self.viewers = set()
        self.peers = set()
        self.chat_digests = {}
        self.control_lock = asyncio.Lock()
        self.task = None
        self.audio_tasks = set()
        self.active = None
        self.state = 'idle'
        self.subtitle = ''
        self.segments = {}
        self.started = set()
        self.ended = set()
        self.synthesis_done = False
        self.lock = asyncio.Lock()
        self.voice_state = 'off'

    async def stop_voice(self, owner=None):
        if VOICE.session is self and (owner is None or VOICE.owner is owner):
            await VOICE.close(VOICE.owner)
            if self.voice_state != 'off':
                self.voice_state = 'off'
                await self.emit('voice_state', target=owner or self.owner, voiceState='off')

    def start_voice(self, owner):
        def authorized():
            digest = self.chat_digests.get(owner)
            return (self.owner is owner and VOICE.owner is owner and VOICE.session is self
                    and (digest is None or DB.execute(
                        'SELECT 1 FROM wallpaper_chat_pairs WHERE digest=? AND expires>?',
                        (digest, time.time())).fetchone()))

        async def state(value, code=None):
            self.voice_state = value
            if value == 'error':
                async with self.control_lock:
                    if self.owner is owner:
                        await self.cancel()
            await self.emit('voice_state', target=owner, voiceState=value, code=code)

        current = {}
        async def submit(text):
            async with self.control_lock:
                if not authorized():
                    raise asyncio.CancelledError()
                turn = str(uuid.uuid4())
                current['turn'] = turn
                # Prepare the owner's player before generation can emit audio.
                await self.emit('voice_turn', turn, target=owner, userText=text)
                await self.command(dict(v=1, type='send', turnId=turn, text=text))

        async def wait_reply():
            from server.cloud import CloudError
            async with asyncio.timeout(150):
                while authorized():
                    row = DB.execute('SELECT status FROM turns WHERE id=?', (current['turn'],)).fetchone()
                    if row is None or row[0] in ('error', 'interrupted'):
                        raise CloudError('voice_reply_failed')
                    if row[0] == 'completed':
                        return
                    await asyncio.sleep(.05)
                raise asyncio.CancelledError()

        return VOICE.open(owner, self, state, submit, wait_reply)

    def history(self):
        rows = DB.execute('SELECT * FROM (SELECT rowid,* FROM turns WHERE session_id=? ORDER BY rowid DESC LIMIT 100) ORDER BY rowid', (self.sid,)).fetchall()
        return [dict(turnId=r['id'], userText=r['user_text'], generatedText=r['generated_text'],
                     displayedText=r['displayed_text'], playedText=r['played_text'],
                     playStartedText=r['play_started_text'], status=r['status'],
                     originTurnId=r['origin_turn_id']) for r in rows]

    async def emit(self, kind, turn=None, segment=None, viewers=False, target=None, **payload):
        # Serialize seq allocation AND sends to prevent reordering across tasks.
        async with self.lock:
            DB.execute('UPDATE sessions SET seq=seq+1 WHERE id=?', (self.sid,))
            seq = DB.execute('SELECT seq FROM sessions WHERE id=?', (self.sid,)).fetchone()[0]
            DB.commit()
            event = dict(v=1, sessionId=self.sid, turnId=turn, segmentId=segment, seq=seq, type=kind, **payload)
            targets = list(self.viewers) if viewers else ([self.owner] if self.owner else [])
            if target is not None:
                targets = [target]
            elif not viewers and kind != 'audio_segment':
                targets += [peer for peer in self.peers if peer is not self.owner]
            for ws in targets:
                try:
                    outgoing = dict(event, ownsPlayback=ws is self.owner) if kind == 'snapshot' else event
                    await asyncio.wait_for(ws.send_json(outgoing), 2)
                except (Exception, asyncio.TimeoutError):
                    if ws in self.viewers:
                        self.viewers.discard(ws)
            return event

    async def snapshot(self, viewer=None, request_revision=None, target=None):
        if viewer:
            # Snapshot carries no audio and never tells a reconnecting viewer to replay.
            await self.emit('state', self.active, viewers=True, state=self.state, subtitle=self.subtitle)
        else:
            await self.emit('snapshot', self.active, target=target, history=self.history(), activeTurnId=self.active,
                            state=self.state, subtitle=self.subtitle, testMode=CLOUD is None, usage=usage_status(),
                            ownsPlayback=(target is self.owner) if target is not None else True,
                            dialogueBackend=getattr(CLOUD, 'backend', None),
                            voiceAvailable=voice_available(),
                            voiceState=self.voice_state if VOICE.owner is (target or self.owner) else 'off',
                            **({'requestRevision': request_revision} if request_revision is not None else {}))

    async def state_to(self, state, subtitle=''):
        self.state, self.subtitle = state, subtitle
        await self.emit('state', self.active, state=state, subtitle=subtitle)
        await self.emit('state', self.active, viewers=True, state=state, subtitle=subtitle)

    async def cancel(self):
        old = self.active
        self.active = None  # invalidate before task cancellation
        # Native stream teardown can wait for an acknowledgement. Stop parallel
        # synthesis and delivery now, before that teardown is allowed to block.
        for job in tuple(self.audio_tasks):
            if not job.done():
                job.cancel()
        if self.task and not self.task.done():
            self.task.cancel()
        self.task = None
        if old:
            DB.execute("UPDATE turns SET status='interrupted' WHERE id=? AND status NOT IN ('completed','error')", (old,))
            DB.commit()
            await self.emit('cancelled', old)
        self.segments, self.started, self.ended = {}, set(), set()
        self.synthesis_done = False
        await self.state_to('cancelled' if old else 'idle')

    def current(self, turn):
        return self.active == turn

    async def generate(self, turn, user_text, fault, retry_text=None):
        if CLOUD is not None:
            await self.generate_cloud(turn,user_text,fault,retry_text)
            return
        try:
            await self.emit('turn_started', turn, testMode=True, userText=user_text)
            await self.state_to('generating' if retry_text is None else 'buffering')
            text = retry_text if retry_text is not None else mock_reply(user_text)
            if retry_text is None:
                for i in range(0, len(text), 3):
                    await asyncio.sleep(.035)
                    if not self.current(turn):
                        return
                    full = text[:i+3]
                    DB.execute('UPDATE turns SET generated_text=?,displayed_text=? WHERE id=?', (full, full, turn))
                    DB.commit()
                    await self.emit('text_delta', turn, text=full, delta=text[i:i+3])
            if not self.current(turn):
                return
            await self.emit('text_done', turn, text=text)
            if fault == 'tts':
                DB.execute("UPDATE turns SET status='error' WHERE id=?", (turn,))
                DB.commit()
                await self.state_to('error')
                await self.emit('error', turn, scope='tts', message='模拟语音故障：文字已保留，可只重试测试音。', retryable=True)
                return
            await self.state_to('buffering')
            # S1 bounded deterministic signals, not speech.
            parts = [s+'。' for s in text.rstrip('。').split('。') if s][:3]
            for index, sentence in enumerate(parts):
                await asyncio.sleep(.08)
                if not self.current(turn):
                    return
                self.segments[index] = sentence
                await self.emit('audio_segment', turn, index, text=sentence, audio=tone(index),
                                mime='audio/wav', durationMs=420, testAudio=True)
            if self.current(turn):
                self.synthesis_done = True
                await self.emit('synthesis_done', turn)
                await self.finish_if_played(turn)
        except asyncio.CancelledError:
            pass
        except Exception:
            if self.current(turn):
                DB.execute("UPDATE turns SET status='error' WHERE id=?", (turn,))
                DB.commit()
                await self.state_to('error')
                await self.emit('error', turn, scope='server', message='本地原型处理失败，已保留生成文字。', retryable=True)

    async def generate_cloud(self, turn, user_text, fault, retry_text=None):
        global LAST_CLOUD_ERROR
        scope = "llm"
        jobs = []
        sender = None
        queue = asyncio.Queue()
        queued_end = 0

        def audio_task(coroutine):
            job = asyncio.create_task(coroutine)
            self.audio_tasks.add(job)
            job.add_done_callback(self.audio_tasks.discard)
            return job

        async def send_audio():
            while True:
                item = await queue.get()
                if item is None:
                    return
                index, sentence, job = item
                result = await job
                if not self.current(turn):
                    return
                self.segments[index] = sentence
                await self.emit('audio_segment', turn, index, text=sentence, audio=result['audio'],
                                mime=result['mime'], durationMs=result['durationMs'], testAudio=False)

        def submit(sentence):
            # Bounded to three requests; delivery and playback retain FIFO order.
            job = audio_task(CLOUD.synthesize(sentence))
            jobs.append(job)
            queue.put_nowait((len(jobs)-1, sentence, job))

        try:
            await self.emit("turn_started",turn,testMode=False,userText=user_text)
            await self.state_to("generating" if retry_text is None else "buffering")
            text = retry_text or ""
            sender = audio_task(send_audio())
            if retry_text is None:
                if getattr(CLOUD, 'backend', None) == 'neko':
                    replies = CLOUD.stream_turn(self.sid, turn, user_text)
                else:
                    messages = context_messages(self.sid,turn)
                    messages.append({"role":"user","content":user_text})
                    replies = CLOUD.stream_reply(messages)
                async for delta in replies:
                    if not self.current(turn):
                        return
                    text += delta
                    DB.execute("UPDATE turns SET generated_text=?,displayed_text=? WHERE id=?", (text,text,turn))
                    DB.commit()
                    await self.emit("text_delta",turn,text=text,delta=delta)
                    # Commit completed units while native text is still arriving.
                    # Keep a final slot for the tail, including any short fragments.
                    if fault != 'tts' and len(jobs) < 2:
                        boundary = queued_end
                        for pos in range(queued_end, len(text)):
                            if text[pos] in '。！？!?；;' and len(text[boundary:pos+1].strip()) >= 4:
                                submit(text[boundary:pos+1].strip())
                                boundary = pos+1
                                queued_end = boundary
                                if len(jobs) == 2:
                                    break
            if not self.current(turn):
                return
            await self.emit("text_done",turn,text=text)
            scope = "tts"
            if fault == "tts":
                raise RuntimeError("injected_tts_fault")
            if self.state != 'speaking':
                await self.state_to("buffering")
            tail = text[queued_end:].strip()
            if not jobs and not tail:
                raise RuntimeError("empty_tts_text")
            for sentence in (spoken_sentences(tail) if not jobs else ([tail] if tail else [])):
                submit(sentence)
            queue.put_nowait(None)
            await sender
            self.synthesis_done = True
            await self.emit("synthesis_done",turn)
            await self.finish_if_played(turn)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if self.current(turn):
                from server.cloud import CloudError
                safe_codes = {"free_tier_exhausted", "account_arrearage", "provider_rate_limited", "chat_timeout", "tts_timeout",
                              "provider_upstream_rejected", "provider_response_limit",
                              "chat_transport_or_protocol_error", "tts_transport_or_protocol_error",
                              "chat_upstream_error", "tts_upstream_error", "chat_call_limit",
                              "tts_call_limit", "tts_character_limit", "usage_config_invalid", "usage_storage_unavailable",
                              'neko_unavailable', 'neko_timeout', 'neko_protocol_error', 'neko_restart_required',
                              'neko_session_failed', 'neko_response_discarded', 'neko_empty_reply', 'neko_reply_too_long'}
                code = str(exc) if isinstance(exc, CloudError) and str(exc) in safe_codes else "cloud_request_failed"
                LAST_CLOUD_ERROR = {'scope': scope, 'code': code, 'time': int(time.time())}
                DB.execute("UPDATE turns SET status='error' WHERE id=?", (turn,))
                DB.commit()
                await self.state_to("error")
                message = "文字接口暂不可用，请稍后重试。" if scope == "llm" else "语音暂不可用，生成文字已保留，可仅重试语音。"
                if code == "free_tier_exhausted":
                    message = "免费额度已耗尽，本次服务已停止云调用；已有文字保留。"
                elif code == "provider_rate_limited":
                    message = "云服务限流，已停止本轮，未自动重试。"
                elif code == "account_arrearage":
                    message = "百炼账号欠费或余额状态受限，已停止云调用；请核对账号状态。"
                elif code in {"chat_call_limit", "tts_call_limit", "tts_character_limit"}:
                    message = "已达到本机今日的" + {"chat_call_limit": "文字请求上限", "tts_call_limit": "语音请求上限", "tts_character_limit": "估计语音字数上限"}[code] + "。北京时间次日恢复；也可调整 config/usage-limits.json。已有文字保留。"
                elif code == "usage_config_invalid":
                    message = "本地用量配置无效，已暂停云调用。请检查 config/usage-limits.json。"
                elif code == "usage_storage_unavailable":
                    message = "本地用量记录暂不可写，已暂停云调用。请检查数据目录权限和磁盘空间。"
                elif code.startswith('neko_'):
                    message = ('回答超过当前语音长度限制，完整文字已保留。' if code == 'neko_reply_too_long'
                               else 'N.E.K.O. 本轮未能完成，已停止输出；请检查融合服务，未自动重发消息。')
                    if code == 'neko_restart_required':
                        message = 'N.E.K.O. 停止确认未完成，请停止并重新启动融合服务后继续。'
                await self.emit("error",turn,scope=scope,code=code,message=message,retryable=scope == "tts" and code not in {"free_tier_exhausted", "account_arrearage"})
        finally:
            for job in [sender, *jobs]:
                if job is not None and not job.done():
                    job.cancel()
            await asyncio.gather(*(job for job in [sender, *jobs] if job is not None), return_exceptions=True)
            if self.current(turn):
                await self.emit('usage', turn, usage=usage_status())

    async def finish_if_played(self, turn):
        if self.current(turn) and self.synthesis_done and self.ended == set(self.segments):
            DB.execute("UPDATE turns SET status='completed' WHERE id=?", (turn,))
            DB.commit()
            await self.state_to('idle')
            await self.emit('turn_done', turn)
            self.active = None

    def prepare_turn(self, msg):
        if msg.get('v') != 1:
            raise ValueError('协议版本必须为 1')
        kind = msg.get('type')
        if kind in ('send', 'retry_audio'):
            turn = msg.get('turnId')
            try:
                uuid.UUID(turn)
            except (ValueError, TypeError, AttributeError):
                raise ValueError('turnId 必须为 UUID')
            if DB.execute('SELECT 1 FROM turns WHERE id=?', (turn,)).fetchone():
                raise ValueError('turnId 已使用')
            retry_text, source = None, None
            if kind == 'retry_audio':
                source = msg.get('sourceTurnId')
                row = DB.execute('SELECT * FROM turns WHERE id=? AND session_id=?', (source, self.sid)).fetchone()
                if not row or not row['generated_text'] or row['status'] not in ('error','completed','interrupted'):
                    raise ValueError('该轮没有可重试的文字')
                retry_text = row['generated_text']
                user = '[仅重试语音]' if CLOUD else '[仅重试测试音]'
            else:
                user = msg.get('text')
                if not isinstance(user, str) or not 1 <= len(user.strip()) <= 2000:
                    raise ValueError('请输入 1–2000 字')
                user = user.strip()
            fault = msg.get('fault', 'none')
            if fault not in ('none','tts'):
                raise ValueError('无效模拟故障选项')
            return turn, user, fault, retry_text, source
        return None

    async def command(self, msg):
        if (SETTINGS.applying or SETTINGS.previewing) and msg.get('type') in {'send', 'retry_audio'}:
            await self.emit('error', scope='settings', message='正在应用人物设置，请稍后发送。', retryable=True)
            return
        prepared = self.prepare_turn(msg)
        kind = msg.get('type')
        if prepared:
            turn, user, fault, retry_text, source = prepared
            await self.cancel()
            self.active = turn
            DB.execute('INSERT INTO turns(id,session_id,user_text,generated_text,displayed_text,status,origin_turn_id,created) VALUES(?,?,?,?,?,?,?,?)',
                       (turn,self.sid,user,retry_text or '',retry_text or '','generating',source,time.time()))
            DB.commit()
            self.task = asyncio.create_task(self.generate(turn,user,fault,retry_text))
        elif kind == 'cancel':
            if msg.get('turnId') == self.active:
                await self.cancel()
        elif kind == 'refresh':
            await self.snapshot(request_revision=msg.get('requestRevision'))
        elif kind == 'played':
            turn, segment, phase = msg.get('turnId'), msg.get('segmentId'), msg.get('phase')
            if not self.current(turn) or type(segment) is not int or segment not in self.segments:
                return
            text = self.segments[segment]
            if phase == 'started' and segment not in self.started:
                self.started.add(segment)
                DB.execute('UPDATE turns SET play_started_text=play_started_text||?,status=? WHERE id=?', (text,'speaking',turn))
                DB.commit()
                await self.state_to('speaking', text)
            elif phase == 'ended' and segment in self.started and segment not in self.ended:
                self.ended.add(segment)
                DB.execute('UPDATE turns SET played_text=played_text||? WHERE id=?', (text,turn))
                DB.commit()
                await self.state_to('buffering')
                await self.finish_if_played(turn)
        else:
            raise ValueError('未知命令')


def mock_reply(user):
    safe = user[:60].replace('。', '，')
    return f'这是本地模拟回复，收到了：{safe}。接下来播放的是测试提示音，尚未接入真人女声。'


def tone(index=0):
    rate, duration = 16000, .42
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        samples = [int(2200 * min(i/160, (int(rate*duration)-i)/160, 1) * math.sin(2*math.pi*(440+index*110)*i/rate)) for i in range(int(rate*duration))]
        wav.writeframes(struct.pack('<'+'h'*len(samples), *samples))
    return base64.b64encode(output.getvalue()).decode('ascii')


@app.get('/health')
async def health():
    usage = usage_status()
    return {'ok': True, 'mode': 'cloud' if CLOUD else 'simulation', 'protocol': 1,
            'cloudCalls': usage.get('reserved', {}) if CLOUD else 0,
            'usageAvailable': usage.get('mode') != 'unavailable',
            'lastCloudError': LAST_CLOUD_ERROR,
            'dialogueBackend': getattr(CLOUD, 'backend', 'direct' if CLOUD else 'simulation')}


@app.get('/api/usage')
async def get_usage(request: Request):
    if not valid_session(request.cookies.get(COOKIE)):
        raise HTTPException(401)
    return usage_status()


@app.get('/api/bootstrap')
async def bootstrap(request: Request):
    sid = request.cookies.get(COOKIE)
    if getattr(CLOUD, 'backend', None) == 'neko':
        # A native character has one authoritative session. All local clients
        # use the same new display conversation, without importing old chats.
        DB.execute('CREATE TABLE IF NOT EXISTS backend_sessions(backend TEXT PRIMARY KEY, session_id TEXT NOT NULL)')
        row = DB.execute('SELECT session_id FROM backend_sessions WHERE backend=?', ('neko',)).fetchone()
        if row:
            sid = row[0]
        else:
            sid = secrets.token_urlsafe(32)
            DB.execute('INSERT INTO sessions(id) VALUES(?)', (sid,))
            DB.execute('INSERT INTO backend_sessions VALUES(?,?)', ('neko', sid))
            DB.commit()
    if not valid_session(sid):
        sid = secrets.token_urlsafe(32)
        DB.execute('INSERT INTO sessions(id) VALUES(?)', (sid,))
        DB.commit()
    from server.cloud import blocked_reason
    response = JSONResponse({'protocol': 1, 'mode': 'cloud' if CLOUD else 'simulation',
                             'cloudBlock': blocked_reason() if CLOUD else None,
                             'dialogueBackend': getattr(CLOUD, 'backend', None)})
    response.set_cookie(COOKIE, sid, httponly=True, samesite='strict', max_age=PAIR_LIFETIME)
    return response


@app.delete('/api/history')
async def delete_history(request: Request):
    sid = request.cookies.get(COOKIE)
    if not valid_session(sid):
        raise HTTPException(401)
    rt = runtimes.get(sid)
    if rt:
        async with rt.control_lock:
            await rt.stop_voice()
            await rt.cancel()
            DB.execute('DELETE FROM turns WHERE session_id=?', (sid,))
            DB.commit()
            await rt.emit('history_cleared')
            await rt.snapshot()
    else:
        DB.execute('DELETE FROM turns WHERE session_id=?', (sid,))
        DB.commit()
    return {'ok': True}


@app.post('/api/wallpaper-pair')
async def pair_wallpaper(request: Request):
    sid = request.cookies.get(COOKIE)
    if not valid_session(sid):
        raise HTTPException(401)
    await revoke_wallpaper(request)
    token = secrets.token_urlsafe(32)
    expires = time.time() + PAIR_LIFETIME
    DB.execute('INSERT INTO wallpaper_pairs VALUES(?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), sid, expires))
    DB.commit()
    return {'token': token, 'scope': 'state-only', 'expires': int(expires)}


@app.get('/api/wallpaper-pair')
async def wallpaper_status(request: Request):
    sid = request.cookies.get(COOKIE)
    if not valid_session(sid):
        raise HTTPException(401)
    pair = DB.execute('SELECT MAX(expires) FROM wallpaper_pairs WHERE session_id=? AND expires>?', (sid, time.time())).fetchone()[0]
    rt = runtimes.get(sid)
    connected = sum(isinstance(v, PairedViewer) for v in rt.viewers) if rt else 0
    return {'paired': pair is not None, 'expires': int(pair) if pair else None, 'connected': connected}


@app.post('/api/wallpaper-pair/install')
async def install_wallpaper_pair(request: Request):
    sid = request.cookies.get(COOKIE)
    if not valid_session(sid):
        raise HTTPException(401)
    from server.wallpaper import install_pair, atomic_write
    token = secrets.token_urlsafe(32)
    chat_token = secrets.token_urlsafe(32)
    try:
        target, original = install_pair(WALLPAPER_DIR, DATA / 'wallpaper-backup', token, chat_token)
    except OSError:
        raise HTTPException(409, detail='增强壁纸配置不可写，请检查目录是否存在且不是目录联接。')
    expires = time.time() + PAIR_LIFETIME
    try:
        DB.execute('DELETE FROM wallpaper_pairs WHERE session_id=?', (sid,))
        DB.execute('INSERT INTO wallpaper_pairs VALUES(?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), sid, expires))
        DB.execute('DELETE FROM wallpaper_chat_pairs WHERE session_id=?', (sid,))
        DB.execute('INSERT INTO wallpaper_chat_pairs VALUES(?,?,?)', (hashlib.sha256(chat_token.encode()).hexdigest(), sid, expires))
        DB.commit()
    except sqlite3.Error:
        DB.rollback()
        try:
            atomic_write(target, original)
        except OSError:
            raise HTTPException(503, detail='配对保存失败，配置恢复未完成；请检查增强目录权限后重新连接。')
        raise HTTPException(503, detail='配对保存失败，已恢复壁纸原配置。')
    await close_paired_viewers(sid)
    await close_chat_peers(sid)
    return {'ok': True, 'scope': 'state-and-chat', 'expires': int(expires), 'reloadRequired': True}


async def close_paired_viewers(sid):
    rt = runtimes.get(sid)
    if rt:
        for viewer in list(rt.viewers):
            if isinstance(viewer, PairedViewer):
                rt.viewers.discard(viewer)
                await viewer.ws.close(code=1008)


@app.delete('/api/wallpaper-pair')
async def revoke_wallpaper(request: Request):
    sid = request.cookies.get(COOKIE)
    if not valid_session(sid):
        raise HTTPException(401)
    DB.execute('DELETE FROM wallpaper_pairs WHERE session_id=?', (sid,))
    DB.execute('DELETE FROM wallpaper_chat_pairs WHERE session_id=?', (sid,))
    DB.commit()
    await close_paired_viewers(sid)
    await close_chat_peers(sid)
    return {'ok': True}


async def close_chat_peers(sid):
    rt = runtimes.get(sid)
    if rt:
        async with rt.control_lock:
            for peer in list(rt.chat_digests):
                await rt.stop_voice(peer)
                await NATIVE_INPUT.close(peer)
                rt.chat_digests.pop(peer, None)
                rt.peers.discard(peer)
                if rt.owner is peer:
                    await rt.cancel()
                    rt.owner = None
                await peer.close(code=1008)


class PairedViewer:
    def __init__(self, ws):
        self.ws = ws

    async def send_json(self, event):
        # The wallpaper capability cannot obtain chat cookies, history or audio.
        if event['type'] in {'state', 'heartbeat'}:
            await self.ws.send_json({k: v for k, v in event.items() if k != 'sessionId'})


@app.websocket('/ws/paired-wallpaper')
async def paired_wallpaper(ws: WebSocket):
    token = ws.query_params.get('token', '')
    digest = hashlib.sha256(token.encode()).hexdigest()
    pair = DB.execute('SELECT session_id FROM wallpaper_pairs WHERE digest=? AND expires>?', (digest, time.time())).fetchone() if len(token) == 43 else None
    sid = pair[0] if pair else None
    if (ws.headers.get('host') != '127.0.0.1:8765' or
            ws.headers.get('origin') not in {ORIGIN, 'null'} or not valid_session(sid)):
        await ws.close(code=1008)
        return
    await ws.accept()
    rt = runtimes.setdefault(sid, Runtime(sid))
    viewer = PairedViewer(ws)
    if sum(isinstance(v, PairedViewer) for v in rt.viewers) >= 2:
        await ws.close(code=4009)
        return
    rt.viewers.add(viewer)
    async def heartbeat():
        while True:
            await asyncio.sleep(5)
            if not DB.execute('SELECT 1 FROM wallpaper_pairs WHERE digest=? AND expires>?', (digest, time.time())).fetchone():
                await ws.close(code=1008)
                return
            await rt.emit('heartbeat', viewers=True)
    task = None
    try:
        await rt.snapshot(viewer)
        task = asyncio.create_task(heartbeat())
        # No commands are accepted from a state-only capability.
        incoming = await ws.receive()
        if incoming['type'] != 'websocket.disconnect':
            await ws.close(code=1008)
    except WebSocketDisconnect:
        pass
    finally:
        if task:
            task.cancel()
        rt.viewers.discard(viewer)


async def connect(ws, viewer=False, chat=False):
    digest = None
    if chat:
        token = ws.query_params.get('token', '')
        digest = hashlib.sha256(token.encode()).hexdigest()
        pair = DB.execute('SELECT session_id FROM wallpaper_chat_pairs WHERE digest=? AND expires>?',
                          (digest, time.time())).fetchone() if len(token) == 43 else None
        sid = pair[0] if pair else None
    else:
        sid = ws.cookies.get(COOKIE)
    shared = chat or (not viewer and ws.query_params.get('shared') == '1')
    if getattr(CLOUD, 'backend', None) == 'neko' and not (chat or viewer):
        row = DB.execute('SELECT session_id FROM backend_sessions WHERE backend=?', ('neko',)).fetchone()
        if not row or sid != row[0]:
            sid = None
    origins = {ORIGIN, 'null'} if chat else {ORIGIN}
    if ws.headers.get('host') != '127.0.0.1:8765' or ws.headers.get('origin') not in origins or not valid_session(sid):
        await ws.close(code=1008)
        return
    rt = runtimes.setdefault(sid, Runtime(sid))
    await ws.accept()
    # Registration and the first snapshot share the takeover/disconnect lock.
    # A reconnect must observe the old turn after cancellation has finished.
    async with rt.control_lock:
        if not viewer and not shared and rt.owner:
            await ws.close(code=4009, reason='此会话已有播放窗口，请先关闭旧窗口')
            return
        if viewer:
            rt.viewers.add(ws)
        elif shared:
            if len(rt.peers) >= 4:
                await ws.close(code=4009)
                return
            rt.peers.add(ws)
            if chat:
                rt.chat_digests[ws] = digest
        else:
            rt.owner = ws
        await rt.snapshot(ws if viewer else None, target=ws if shared else None)
    async def check_expiry():
        while True:
            await asyncio.sleep(5)
            if not DB.execute('SELECT 1 FROM wallpaper_chat_pairs WHERE digest=? AND expires>?',
                              (digest, time.time())).fetchone():
                await ws.close(code=1008)
                return
    lease = asyncio.create_task(check_expiry()) if chat else None
    try:
        while True:
            raw = await ws.receive_text()
            if viewer:
                continue
            if len(raw) > 12000:
                await ws.close(code=1009)
                break
            try:
                msg = json.loads(raw)
                if not isinstance(msg, dict):
                    raise ValueError('消息格式错误')
                async with rt.control_lock:
                    if chat and not DB.execute('SELECT 1 FROM wallpaper_chat_pairs WHERE digest=? AND expires>?',
                                               (digest,time.time())).fetchone():
                        await ws.close(code=1008)
                        break
                    kind = msg.get('type')
                    # Validate before a shared client can interrupt playback.
                    rt.prepare_turn(msg)
                    if kind in {'open_input', 'close_input'}:
                        if not chat:
                            raise ValueError('需要壁纸聊天连接')
                        if kind == 'close_input':
                            await NATIVE_INPUT.close(ws)
                            continue
                        request_id = msg.get('requestId')
                        try:
                            uuid.UUID(request_id)
                        except (ValueError, TypeError, AttributeError):
                            raise ValueError('invalid_input_request')
                        draft = msg.get('text', '')
                        if not isinstance(draft, str) or len(draft) > 2000:
                            raise ValueError('invalid_input_draft')
                        layout = checked_layout(msg.get('layout'))

                        async def deliver(result, request_id=request_id):
                            async with rt.control_lock:
                                if (ws in rt.peers and rt.chat_digests.get(ws) == digest
                                        and DB.execute('SELECT 1 FROM wallpaper_chat_pairs WHERE digest=? AND expires>?',
                                                       (digest, time.time())).fetchone()):
                                    await rt.emit('native_input_result', target=ws, requestId=request_id, **result)

                        async def progress(activity, request_id=request_id):
                            if ws in rt.peers and rt.chat_digests.get(ws) == digest:
                                await rt.emit('native_input_activity', target=ws, requestId=request_id, **activity)
                        if os.name != 'nt' or not NATIVE_INPUT.open(ws, draft, deliver, layout, progress):
                            await rt.emit('native_input_result', target=ws, requestId=request_id, action='busy')
                        continue
                    if shared and kind == 'refresh':
                        await rt.snapshot(request_revision=msg.get('requestRevision'), target=ws)
                        continue
                    if kind == 'voice_start':
                        if SETTINGS.applying or SETTINGS.previewing:
                            await rt.emit('voice_state', target=ws, voiceState='error', code='voice_busy')
                            continue
                        if not voice_available():
                            await rt.emit('voice_state', target=ws, voiceState='error', code='voice_unavailable')
                            continue
                        if VOICE.task is not None:
                            if VOICE.owner is not ws:
                                await rt.emit('voice_state', target=ws, voiceState='error', code='voice_busy')
                            continue
                    if shared and kind in {'send','retry_audio','voice_start'} and rt.owner is not ws:
                        previous = rt.owner
                        if previous:
                            await rt.stop_voice(previous)
                            await rt.emit('control_lost', target=previous)
                        await rt.cancel()
                        rt.owner = ws
                        await rt.emit('control_acquired', target=ws)
                    if shared and kind == 'clear_history':
                        await rt.stop_voice()
                        await rt.cancel()
                        DB.execute('DELETE FROM turns WHERE session_id=?', (sid,))
                        DB.commit()
                        await rt.emit('history_cleared')
                        continue
                    if rt.owner is ws:
                        if kind in {'voice_start', 'voice_stop'}:
                            await NATIVE_INPUT.close(ws)
                            if kind == 'voice_stop':
                                await rt.stop_voice(ws)
                            await rt.cancel()
                            if kind == 'voice_start':
                                rt.start_voice(ws)
                            continue
                        if kind in {'send', 'retry_audio'} or (
                                kind == 'cancel' and msg.get('turnId') == rt.active and rt.active is not None):
                            await rt.stop_voice(ws)
                        await rt.command(msg)
            except (ValueError, TypeError, KeyError):
                if isinstance(msg, dict) and msg.get('type') == 'open_input':
                    await rt.emit('native_input_result', target=ws, requestId=msg.get('requestId'), action='error')
                await rt.emit('error', target=ws, scope='protocol', message='命令无效，请检查输入或刷新。', retryable=False)
    except WebSocketDisconnect:
        pass
    finally:
        if lease:
            lease.cancel()
        await NATIVE_INPUT.close(ws)
        if viewer:
            rt.viewers.discard(ws)
        else:
            async with rt.control_lock:
                await rt.stop_voice(ws)
                rt.peers.discard(ws)
                rt.chat_digests.pop(ws, None)
                if rt.owner is ws:
                    rt.owner = None
                    await rt.cancel()


@app.websocket('/ws')
async def owner_socket(ws: WebSocket):
    await connect(ws)


@app.websocket('/ws/wallpaper')
async def wallpaper_socket(ws: WebSocket):
    await connect(ws, viewer=True)


@app.websocket('/ws/wallpaper-chat')
async def wallpaper_chat_socket(ws: WebSocket):
    await connect(ws, chat=True)


@app.get('/')
async def index():
    return FileResponse(ROOT / 'web' / 'index.html')


@app.get('/wallpaper-preview')
async def wallpaper_preview():
    return FileResponse(ROOT / 'wallpaper' / 'preview.html')


app.mount('/web', StaticFiles(directory=ROOT / 'web', check_dir=False), name='web')
app.mount('/wallpaper', StaticFiles(directory=ROOT / 'wallpaper', check_dir=False), name='wallpaper')
from server.standalone import create_router as create_standalone_router
app.include_router(create_standalone_router())
from server.wallpaper_assets import create_asset_router
app.include_router(create_asset_router(WALLPAPER_DIR))
from server.audition import create_router
app.include_router(create_router(DATA, ROOT, valid_session, COOKIE))
app.include_router(create_admin_router(SETTINGS, valid_session, COOKIE))

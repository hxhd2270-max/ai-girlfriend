"""Click-to-open local microphone, bounded utterances and token-plan ASR.

Audio stays in RAM. Only a completed speech segment is sent for recognition.
The caller owns authentication, single-client ownership and NEKO dispatch.
"""
import asyncio
from collections import deque
import ctypes
import io
import math
import os
import queue
import struct
import threading
import wave

import httpx

from server.cloud import CloudError, _key, _upstream_error

RATE = 16000
BLOCK_BYTES = 3200  # 100 ms, signed 16-bit mono
MAX_SECONDS = 20


def wav_bytes(pcm):
    out = io.BytesIO()
    with wave.open(out, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(pcm)
    return out.getvalue()


class SpeechSegments:
    """Local energy VAD; never sends silence, and bounds every utterance."""
    def __init__(self):
        self.reset()

    def reset(self):
        self.pre = deque(maxlen=3)
        self.frames = []
        self.silent = self.voiced = 0

    def feed(self, pcm):
        if len(pcm) != BLOCK_BYTES:
            raise CloudError('voice_capture_format')
        values = struct.unpack('<1600h', pcm)
        loud = math.sqrt(sum(x*x for x in values) / len(values)) >= 450
        if not self.frames:
            if not loud:
                self.pre.append(pcm)
                return None
            self.frames = list(self.pre)
            self.pre.clear()
        self.frames.append(pcm)
        self.voiced += int(loud)
        self.silent = 0 if loud else self.silent + 1
        if self.silent >= 9 or len(self.frames) >= MAX_SECONDS * 10:
            data = b''.join(self.frames) if self.voiced >= 3 else None
            self.reset()
            return data
        return None


class WindowsMicrophone:
    """WinMM capture with explicit lifetime; construction never opens a device."""
    def __init__(self):
        self.events = queue.Queue(maxsize=40)
        self.stopped = threading.Event()
        self.thread = None
        self.overrun = False

    def _publish(self, value):
        try:
            self.events.put_nowait(value)
        except queue.Full:
            self.overrun = True
            self.stopped.set()  # Do not recognize a silently gapped recording.

    def _run(self):
        from ctypes import wintypes as w

        class Format(ctypes.Structure):
            _pack_ = 1
            _fields_ = [('tag', w.WORD), ('channels', w.WORD), ('rate', w.DWORD),
                        ('avg', w.DWORD), ('align', w.WORD), ('bits', w.WORD), ('extra', w.WORD)]

        class Header(ctypes.Structure):
            _fields_ = [('data', ctypes.c_void_p), ('length', w.DWORD), ('recorded', w.DWORD),
                        ('user', ctypes.c_size_t), ('flags', w.DWORD), ('loops', w.DWORD),
                        ('next', ctypes.c_void_p), ('reserved', ctypes.c_size_t)]

        api = ctypes.WinDLL('winmm')
        handle = w.HANDLE()
        api.waveInOpen.argtypes = [ctypes.POINTER(w.HANDLE), w.UINT, ctypes.POINTER(Format),
                                   ctypes.c_size_t, ctypes.c_size_t, w.DWORD]
        for name in ('waveInPrepareHeader', 'waveInUnprepareHeader', 'waveInAddBuffer'):
            getattr(api, name).argtypes = [w.HANDLE, ctypes.POINTER(Header), w.UINT]
        for name in ('waveInStart', 'waveInStop', 'waveInReset', 'waveInClose'):
            getattr(api, name).argtypes = [w.HANDLE]
        buffers, prepared = [], []
        try:
            fmt = Format(1, 1, RATE, RATE*2, 2, 16, 0)
            if api.waveInOpen(ctypes.byref(handle), 0xffffffff, ctypes.byref(fmt), 0, 0, 0):
                raise CloudError('voice_microphone_unavailable')
            for _ in range(8):
                data = ctypes.create_string_buffer(BLOCK_BYTES)
                header = Header(ctypes.addressof(data), BLOCK_BYTES, 0, 0, 0, 0, None, 0)
                buffers.append((data, header))
                if api.waveInPrepareHeader(handle, ctypes.byref(header), ctypes.sizeof(header)):
                    raise CloudError('voice_microphone_unavailable')
                prepared.append(header)
                if api.waveInAddBuffer(handle, ctypes.byref(header), ctypes.sizeof(header)):
                    raise CloudError('voice_microphone_unavailable')
            if api.waveInStart(handle):
                raise CloudError('voice_microphone_unavailable')
            self._publish('ready')
            next_buffer = 0
            while not self.stopped.wait(.015):
                for _ in buffers:
                    data, header = buffers[next_buffer]
                    if header.flags & 1:
                        if header.recorded != BLOCK_BYTES:
                            raise CloudError('voice_capture_format')
                        self._publish(bytes(data.raw[:header.recorded]))
                        header.recorded = 0
                        if api.waveInAddBuffer(handle, ctypes.byref(header), ctypes.sizeof(header)):
                            raise CloudError('voice_microphone_unavailable')
                        next_buffer = (next_buffer + 1) % len(buffers)
                    else:
                        break
            # Queue exhaustion is an error, never a partial utterance.
            if self.overrun:
                while not self.events.empty():
                    self.events.get_nowait()
                self._publish(CloudError('voice_capture_overrun'))
        except Exception as exc:
            self._publish(exc if isinstance(exc, CloudError) else CloudError('voice_microphone_unavailable'))
        finally:
            if handle.value:
                api.waveInStop(handle)
                api.waveInReset(handle)
                for header in prepared:
                    api.waveInUnprepareHeader(handle, ctypes.byref(header), ctypes.sizeof(header))
                api.waveInClose(handle)

    async def __aenter__(self):
        if os.name != 'nt':
            raise CloudError('voice_microphone_unavailable')
        self.thread = threading.Thread(target=self._run, daemon=True, name='companion-microphone')
        self.thread.start()
        try:
            async with asyncio.timeout(8):
                if await self.read() != 'ready':
                    raise CloudError('voice_microphone_unavailable')
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def read(self):
        while True:
            try:
                value = self.events.get_nowait()
                if isinstance(value, Exception):
                    raise value
                return value
            except queue.Empty:
                if self.thread and not self.thread.is_alive():
                    raise CloudError('voice_microphone_unavailable')
                await asyncio.sleep(.02)

    def discard(self):
        while True:
            try:
                value = self.events.get_nowait()
                if isinstance(value, Exception):
                    raise value
            except queue.Empty:
                return

    async def __aexit__(self, *args):
        self.stopped.set()
        if self.thread:
            await asyncio.to_thread(self.thread.join, 3)
        while not self.events.empty():
            self.events.get_nowait()


class PlanRecognizer:
    async def transcribe(self, pcm):
        import base64
        if not BLOCK_BYTES * 3 <= len(pcm) <= RATE * 2 * MAX_SECONDS or len(pcm) % 2:
            raise CloudError('voice_audio_limit')
        key = _key()
        from server.usage import reserve_asr, UsageError
        try:
            reserve_asr(len(pcm) * 1000 // (RATE * 2))
        except UsageError as exc:
            raise CloudError(str(exc)) from None
        payload = {'model': 'qwen-audio-3.0-asr-flash',
                   'input': {'messages': [{'role': 'user', 'content': [{'type': 'input_audio',
                       'input_audio': {'data': 'data:audio/wav;base64,' +
                                      base64.b64encode(wav_bytes(pcm)).decode('ascii')}}]}]},
                   'parameters': {'format': 'wav', 'sample_rate': str(RATE), 'language_hints': ['zh']}}
        try:
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=30) as client:
                async with client.stream('POST',
                    'https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation',
                    headers={'Authorization': 'Bearer ' + key}, json=payload) as response:
                    body = bytearray()
                    async for part in response.aiter_bytes(chunk_size=4096):
                        body.extend(part)
                        if len(body) > 65536:
                            raise CloudError('voice_asr_protocol')
                    import json
                    result = json.loads(body)
                    if response.status_code != 200:
                        raise _upstream_error(result.get('error', result), 'voice_asr_upstream')
            text = result['output']['text']
            if not isinstance(text, str) or len(text) > 2000:
                raise CloudError('voice_asr_protocol')
            return text.strip()
        except CloudError:
            raise
        except httpx.TimeoutException:
            raise CloudError('voice_asr_timeout') from None
        except Exception:
            raise CloudError('voice_asr_protocol') from None


class VoiceBroker:
    """One microphone globally. Stop/cancel fences all late ASR results."""
    def __init__(self, microphone=WindowsMicrophone, recognizer=None):
        self.microphone = microphone
        self.recognizer = recognizer or PlanRecognizer()
        self.owner = self.task = self.session = None

    def open(self, owner, session, state, submit, wait_reply):
        if self.task is not None:
            return False
        self.owner, self.session = owner, session

        async def work():
            try:
                async with self.microphone() as mic:
                    vad = SpeechSegments()
                    await state('listening')
                    while True:
                        pcm = vad.feed(await mic.read())
                        if pcm is None:
                            continue
                        await state('recognizing')
                        # Keep draining capture while recognition/playback runs;
                        # this release is half-duplex to avoid speaker feedback.
                        async def reply():
                            try:
                                async with asyncio.timeout(35):
                                    text = await self.recognizer.transcribe(pcm)
                            except TimeoutError:
                                raise CloudError('voice_asr_timeout') from None
                            if not text:
                                return
                            await submit(text)
                            await state('replying')
                            await wait_reply()
                            await asyncio.sleep(.4)
                        job = asyncio.create_task(reply())
                        try:
                            while not job.done():
                                mic.discard()
                                await asyncio.sleep(.05)
                            await job
                        finally:
                            if not job.done():
                                job.cancel()
                            try:
                                await job
                            except asyncio.CancelledError:
                                pass
                        mic.discard()
                        vad.reset()
                        await state('listening')
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                await state('error', 'voice_reply_timeout')
            except Exception as exc:
                await state('error', str(exc) if isinstance(exc, CloudError) else 'voice_microphone_unavailable')
            finally:
                self.owner = self.session = self.task = None
                await state('off')
        self.task = asyncio.create_task(work())
        return True

    async def close(self, owner):
        if self.owner is owner and self.task is not None:
            task = self.task
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            finally:
                # A task cancelled before its first instruction never runs its
                # own finally. Do not leave a permanently occupied microphone.
                if self.task is task:
                    self.owner = self.session = self.task = None
                    

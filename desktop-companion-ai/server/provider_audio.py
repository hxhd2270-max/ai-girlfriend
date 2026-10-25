"""Explicit audio protocols with the same WAV playback and daily counters."""
import asyncio
import array
import base64
import hashlib
import io
import json
import wave

import httpx
from server.cloud import CloudClient, CloudError, _reserve, _gate, MAX_TEXT, TOTAL_TIMEOUT


async def read_response(client, url, *, limit, **kwargs):
    async with client.stream('POST', url, **kwargs) as response:
        if response.status_code != 200:
            raise CloudError('provider_upstream_rejected')
        data = bytearray()
        async for part in response.aiter_bytes(chunk_size=8192):
            data.extend(part)
            if len(data) > limit:
                raise CloudError('provider_response_limit')
        return bytes(data)


def wav_packet(audio, gain, estimate):
    # Browser and action timing use the returned duration, not a hardcoded rate.
    try:
        with wave.open(io.BytesIO(audio), 'rb') as wav:
            channels, width, rate = wav.getnchannels(), wav.getsampwidth(), wav.getframerate()
            count = wav.getnframes()
            if channels not in (1, 2) or width != 2 or not 8000 <= rate <= 48000 or not 0 < count <= rate * 30:
                raise ValueError()
            pcm = wav.readframes(count)
            if len(pcm) != count * width * channels:
                raise ValueError()
        samples = array.array('h')
        samples.frombytes(pcm)
        if gain != 1:
            for i, value in enumerate(samples):
                samples[i] = max(-32768, min(32767, round(value * gain)))
        output = io.BytesIO()
        with wave.open(output, 'wb') as wav:
            wav.setnchannels(channels)
            wav.setsampwidth(width)
            wav.setframerate(rate)
            wav.writeframes(samples.tobytes())
        return dict(audio=base64.b64encode(output.getvalue()).decode('ascii'), mime='audio/wav',
                    durationMs=count * 1000 / rate, usage=dict(estimatedCharacters=estimate,
                    reportedCharacters=None, freeBalanceVerified=False, finalBillKnown=False))
    except (ValueError, EOFError, wave.Error):
        raise CloudError('tts_invalid_pcm') from None


class RoutedVoice(CloudClient):
    supports_voice = True

    def __init__(self, routes, transport=None):
        _gate()
        self.routes, self.transport = routes, transport
        self.plan = routes.route('tts')['protocol'] == 'token-plan'
        self.chat_model = routes.route('conversation')['model']
        route = routes.route('tts')
        self.voice_default = dict(id=route['voice'], label=route['voice'][:40], gain=1.0)
        # A saved voice belongs to this exact connection/model/default voice.
        self.voice_binding = hashlib.sha256(json.dumps({k: route[k] for k in
            ('protocol', 'baseUrl', 'model', 'voice')}, sort_keys=True).encode()).hexdigest()

    async def synthesize(self, text, *, voice_settings=None):
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise CloudError('invalid_tts_text')
        settings = voice_settings or (self.settings_provider() if getattr(self, 'settings_provider', None) else self.voice_default)
        route = self.routes.route('tts')
        estimate = len(text) + sum('\u4e00' <= c <= '\u9fff' for c in text)
        _reserve('tts', estimate)
        try:
            async with asyncio.timeout(TOTAL_TIMEOUT):
                if route['protocol'] == 'token-plan':
                    return await self._plan_tts(text, '', estimate, settings,
                        dict(endpoint=self.routes.endpoint('tts'), headers=self.routes.headers('tts'), model=route['model']), self.transport)
                async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=TOTAL_TIMEOUT, transport=self.transport) as client:
                    data = await read_response(client, self.routes.endpoint('tts'), limit=6*1024*1024,
                        headers=self.routes.headers('tts'), json=dict(model=route['model'], input=text,
                        voice=settings['id'], response_format='wav'))
                return wav_packet(data, settings['gain'], estimate)
        except CloudError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise CloudError('tts_timeout') from None
        except Exception:
            raise CloudError('tts_transport_or_protocol_error') from None


class RoutedRecognizer:
    def __init__(self, routes, transport=None):
        self.routes, self.transport = routes, transport

    async def transcribe(self, pcm):
        from server.voice import BLOCK_BYTES, RATE, MAX_SECONDS, wav_bytes
        from server.usage import reserve_asr, UsageError
        _gate()
        if not BLOCK_BYTES * 3 <= len(pcm) <= RATE * 2 * MAX_SECONDS or len(pcm) % 2:
            raise CloudError('voice_audio_limit')
        try:
            reserve_asr(len(pcm) * 1000 // (RATE * 2))
        except UsageError as exc:
            raise CloudError(str(exc)) from None
        route = self.routes.route('asr')
        audio = wav_bytes(pcm)
        if route['protocol'] == 'token-plan':
            request = {'json': {'model': route['model'], 'input': {'messages': [{'role': 'user',
                'content': [{'type': 'input_audio', 'input_audio': {'data': 'data:audio/wav;base64,' +
                    base64.b64encode(audio).decode('ascii')}}]}]},
                'parameters': {'format': 'wav', 'sample_rate': str(RATE), 'language_hints': ['zh']}}}
        else:
            request = dict(data={'model': route['model'], 'response_format': 'json'},
                           files={'file': ('speech.wav', audio, 'audio/wav')})
        try:
            async with asyncio.timeout(30):
                async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=30, transport=self.transport) as client:
                    data = await read_response(client, self.routes.endpoint('asr'), limit=65536,
                                               headers=self.routes.headers('asr'), **request)
                body = json.loads(data)
                text = body['output']['text'] if route['protocol'] == 'token-plan' else body['text']
                if not isinstance(text, str) or len(text) > 2000:
                    raise ValueError()
                return text.strip()
        except CloudError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise CloudError('voice_asr_timeout') from None
        except Exception:
            raise CloudError('voice_asr_protocol') from None

"""Offline routing contracts: fictional keys, mock upstreams, isolated stores."""
import base64
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from server.provider_routes import defaults, validate, RuntimeRoutes, RouteStore, TASKS
from server.provider_audio import RoutedVoice, RoutedRecognizer, wav_packet
from server.cloud import CloudError
from server.admin import create_router


def mixed():
    config = defaults()
    config['profiles'] = {}
    for task in TASKS:
        config['profiles'][task] = dict(label=task, protocol='openai-audio' if task in ('tts', 'asr') else 'chat-completions',
            baseUrl='https://' + task + '.example/v1', auth='bearer', key='fictional-' + task,
            tokenField='max_completion_tokens', thinking='omit', streamUsage=False)
        config['assignments'][task] = dict(profile=task, model='model-' + task, voice='voice:demo' if task == 'tts' else '')
    return config


def audio(rate=24000, channels=1):
    out = io.BytesIO()
    with wave.open(out, 'wb') as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b'\x10\x00' * rate * channels)
    return out.getvalue()


class RoutingTests(unittest.TestCase):
    def test_task_protocol_options_and_snapshot(self):
        config = mixed()
        runtime = RuntimeRoutes(config, 'snapshot')
        config['profiles']['summary']['key'] = 'changed'
        for task in TASKS:
            self.assertEqual(runtime.headers(task), {'Authorization': 'Bearer fictional-' + task})
            self.assertIn(task + '.example/v1/', runtime.endpoint(task))
        body = runtime.chat_payload('summary', dict(model='legacy', messages=[], max_tokens=12,
            stream=True, enable_thinking=False, stream_options={'include_usage': True}))
        self.assertEqual(body['model'], 'model-summary')
        self.assertEqual(body['max_completion_tokens'], 12)
        self.assertNotIn('max_tokens', body)
        self.assertNotIn('enable_thinking', body)
        self.assertNotIn('stream_options', body)

    def test_key_retention_and_endpoint_boundary(self):
        previous = mixed()
        draft = copy.deepcopy(previous)
        draft['profiles']['conversation']['key'] = ''
        self.assertEqual(validate(draft, previous)['profiles']['conversation']['key'], 'fictional-conversation')
        for field, value in (('baseUrl', 'https://other.example/v1'), ('protocol', 'token-plan')):
            changed = copy.deepcopy(draft)
            changed['profiles']['conversation'][field] = value
            with self.assertRaises(ValueError):
                validate(changed, previous)
        draft['profiles']['conversation'].update(auth='none', baseUrl='http://127.0.0.1:8000/v1')
        self.assertEqual(validate(draft, previous)['profiles']['conversation']['key'], '')

    def test_reject_bad_urls_and_incompatible_assignments(self):
        for url in ('http://external.example/v1', 'https://a.example/v1?key=foo', 'https://user:pass@a.example/v1',
                    'https://a.example/../v1', 'https://a.example/%2e/v1', 'https://a.example:0/v1'):
            config = mixed()
            config['profiles']['conversation']['baseUrl'] = url
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate(config)
        config = mixed()
        config['assignments']['summary']['profile'] = 'tts'
        with self.assertRaises(ValueError):
            validate(config)

    @unittest.skipUnless(os.name == 'nt', 'Windows encryption')
    def test_encrypted_store_revision_redaction_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            store = RouteStore(folder)
            config = mixed()
            store.save('legacy', config)
            self.assertEqual(store.load(), config)
            raw = store.path.read_bytes()
            self.assertNotIn(b'fictional-', raw)
            public = store.public()
            self.assertNotIn('fictional-', json.dumps(public))
            self.assertTrue(public['restartRequired'])
            self.assertFalse(store.public(public['revision'])['restartRequired'])
            with self.assertRaises(HTTPException) as caught:
                store.save('legacy', config)
            self.assertEqual(caught.exception.status_code, 409)
            with patch('server.provider_config.crypt', side_effect=OSError('fictional-secret')):
                with self.assertRaises(HTTPException) as caught:
                    store.save(public['revision'], config)
                self.assertNotIn('fictional-secret', caught.exception.detail)
            self.assertEqual(store.path.read_bytes(), raw)
            self.assertFalse(list(store.path.parent.glob('.routes-*')))
            with patch.dict(os.environ, {'COMPANION_ROUTES_REVISION': 'wrong'}):
                with self.assertRaises(ValueError):
                    RuntimeRoutes.load(folder)

    def test_admin_auth_origin_and_save_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            app = FastAPI()
            app.include_router(create_router(SimpleNamespace(root=Path(folder), cloud=None), lambda v: v == 'fixture', 'session'))
            with TestClient(app, base_url='http://127.0.0.1:8765') as client:
                self.assertEqual(client.get('/api/admin/providers').status_code, 401)
                client.cookies.set('session', 'fixture')
                self.assertEqual(client.get('/api/admin/providers').status_code, 200)
                body = dict(revision='legacy', config=mixed())
                self.assertEqual(client.post('/api/admin/providers', json=body).status_code, 403)
                headers = {'Origin': 'http://127.0.0.1:8765'}
                result = client.post('/api/admin/providers', json=body, headers=headers)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertNotIn('fictional-', result.text)
                self.assertEqual(result.json()['validation'], 'not_tested')
                self.assertEqual(client.post('/api/admin/providers', json=body, headers=headers).status_code, 409)
                self.assertEqual(client.post('/api/admin/providers', content='x'*65537, headers=headers).status_code, 413)


class AudioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        limits = Path(self.tmp.name) / 'limits.json'
        limits.write_text(json.dumps(dict(chat_calls=30, tts_calls=30, tts_estimated_characters=5000)))
        self.enterContext(patch.dict(os.environ, {'COMPANION_PAID_TEST_AUTHORIZED':'1',
            'COMPANION_FREE_ONLY_CONFIRMED':'0', 'COMPANION_DATA_DIR':self.tmp.name,
            'COMPANION_USAGE_LIMITS_PATH':str(limits)}))
        self.routes = RuntimeRoutes(mixed(), 'fixture')

    async def test_direct_voice_uses_configured_default_after_route_change(self):
        from server.admin import Settings, FIELDS, validate as validate_settings
        config = mixed()
        config['assignments']['tts']['voice'] = 'v' * 100
        provider = RoutedVoice(RuntimeRoutes(config, 'fixture'))
        settings = Settings(Path(self.tmp.name), Path(self.tmp.name), provider, {}, SimpleNamespace(task=None))
        settings.state = {'active': {'voice': {'id': 'old-provider', 'label': 'old', 'gain': 1.0}},
                          'voiceBinding': 'old-binding'}
        current = settings.voice_settings()
        self.assertEqual(current['id'], 'v' * 100)
        persona = dict.fromkeys(FIELDS, '')
        persona.update({'昵称': '知夏', '核心特质': '尊重事实'})
        validate_settings({'persona': persona, 'voice': current})

    async def test_tts_wav_gain_and_asr_multipart_independent_keys(self):
        sent = []
        def upstream(request):
            sent.append(request)
            if request.url.host == 'tts.example':
                self.assertEqual(json.loads(request.content), dict(model='model-tts', input='你好', voice='voice:demo', response_format='wav'))
                return httpx.Response(200, content=audio(48000, 2))
            self.assertIn(b'name="model"', request.content)
            self.assertIn(b'model-asr', request.content)
            self.assertIn(b'filename="speech.wav"', request.content)
            return httpx.Response(200, json={'text':'  测试识别  '})
        transport = httpx.MockTransport(upstream)
        result = await RoutedVoice(self.routes, transport).synthesize('你好', voice_settings={'id':'voice:demo','gain':.5})
        self.assertEqual(result['durationMs'], 1000)
        with wave.open(io.BytesIO(base64.b64decode(result['audio']))) as wav:
            self.assertEqual(wav.getnchannels(), 2)
            self.assertEqual(wav.getframerate(), 48000)
            self.assertEqual(wav.readframes(1), b'\x08\x00'*2)
        self.assertEqual(await RoutedRecognizer(self.routes, transport).transcribe(b'\0\0'*4800), '测试识别')
        self.assertEqual([r.headers['authorization'] for r in sent], ['Bearer fictional-tts','Bearer fictional-asr'])

    async def test_rejection_sanitized_and_still_counted(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(403, text='fictional-private-provider-error'))
        voice = RoutedVoice(self.routes, transport)
        with self.assertRaises(CloudError) as error:
            await voice.synthesize('失败')
        self.assertEqual(str(error.exception), 'provider_upstream_rejected')
        self.assertEqual(voice.counters()['tts_calls'], 1)
        for bad in (b'invalid', audio()[:-100]):
            with self.assertRaises(CloudError):
                wav_packet(bad, 1, 2)

    async def test_token_plan_audio_protocol_preserved(self):
        config = defaults()
        config['profiles']['qwen']['key'] = 'fictional-token-plan'
        routes = RuntimeRoutes(config, 'fixture')
        sent=[]
        def upstream(request):
            sent.append(request)
            if 'SpeechSynthesizer' in request.url.path:
                return httpx.Response(200, json={'output': {'audio': {'data':base64.b64encode(audio()).decode()}}})
            return httpx.Response(200, json={'output': {'text': '原链路'}})
        transport = httpx.MockTransport(upstream)
        self.assertEqual((await RoutedVoice(routes, transport).synthesize('你好'))['durationMs'], 1000)
        self.assertEqual(await RoutedRecognizer(routes, transport).transcribe(b'\0\0'*4800), '原链路')
        self.assertEqual(json.loads(sent[0].content)['input']['voice'], 'longanhuan_v3.6')
        self.assertIn('input_audio', json.dumps(json.loads(sent[1].content)))


class GatewayRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_four_tasks_reach_distinct_routes_and_models(self):
        lab = Path(__file__).resolve().parents[2] / 'neko-lab'
        sys.path.insert(0, str(lab))
        try:
            from gateway import Ledger, MODEL, LOCAL_TOKEN, create_app
        finally:
            sys.path.remove(str(lab))
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder)/'ledger.db')
            sent = []
            def upstream(request):
                sent.append(request)
                body = json.loads(request.content)
                if body['stream']:
                    return httpx.Response(200, content=b'data: {"choices": []}\n\ndata: [DONE]\n\n')
                return httpx.Response(200, json={'choices':[{'message':{'content':'fixture'}}]})
            app = create_app(ledger, '', httpx.MockTransport(upstream), routes=RuntimeRoutes(mixed(),'fixture'))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture',
                    headers={'Authorization':'Bearer '+LOCAL_TOKEN}) as client:
                for task in ('conversation','summary','correction','emotion'):
                    result=await client.post('/'+task+'/v1/chat/completions',json=dict(model=MODEL,
                        messages=[{'role':'user','content':'fixture'}], stream=task=='conversation', response_format={'type':'json_object'}))
                    self.assertEqual(result.status_code,200,result.text)
            for task, request in zip(('conversation','summary','correction','emotion'),sent):
                body=json.loads(request.content)
                self.assertEqual(request.url.host,task+'.example')
                self.assertEqual(request.headers['authorization'],'Bearer fictional-'+task)
                self.assertEqual(body['model'],'model-'+task)
                self.assertNotIn('enable_thinking',body)
                self.assertEqual(body['response_format'],{'type':'json_object'})
            self.assertEqual([a['model'] for a in ledger.snapshot()['attempts']],
                ['model-'+task for task in ('conversation','summary','correction','emotion')])

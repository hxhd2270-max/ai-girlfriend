"""Read-only local audition catalog. No provider requests or credential handling."""
import json
import re
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

def create_router(data: Path, root: Path, valid_session, cookie):
    router = APIRouter()
    samples = data / 'auditions'

    def authorize(request):
        if not valid_session(request.cookies.get(cookie)):
            raise HTTPException(401)

    def catalog():
        path = samples / 'index.json'
        if not path.exists():
            return []
        entries = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(entries, list):
            raise HTTPException(503, '试听索引格式错误')
        return entries

    @router.get('/audition')
    async def page():
        return FileResponse(root / 'web' / 'audition.html')

    @router.get('/api/auditions')
    async def listing(request: Request):
        authorize(request)
        # Explicit public fields: never expose raw provider metadata or file paths.
        return {'samples': [dict(id=e['id'], label=e['label'], text=e['text'],
                                sourceKind=e['sourceKind'], status=e['status'])
                            for e in catalog()], 'cloudCalls': 0}

    @router.get('/api/auditions/{sample_id}/audio')
    async def audio(sample_id: str, request: Request):
        authorize(request)
        if not re.fullmatch('[a-zA-Z0-9_-]{1,64}', sample_id):
            raise HTTPException(404)
        entry = next((e for e in catalog() if e.get('id') == sample_id), None)
        if not entry or entry.get('status') != 'ready':
            raise HTTPException(404)
        base = samples.resolve()
        path = (base / entry['file']).resolve()
        if not path.is_relative_to(base) or path.suffix.lower() != '.wav' or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type='audio/wav', headers={'Cache-Control':'no-store'})

    return router

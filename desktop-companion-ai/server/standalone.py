"""Independent application entry and a closed set of project-owned resources."""
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

ROOT = Path(__file__).resolve().parents[1]
ASSET_ROOT = ROOT / 'assets/companion'
MEDIA = frozenset({f'actions/{name}.webm' for name in (
    'reading', 'to-listening', 'listening', 'thinking',
    'to-speaking', 'speaking', 'to-reading')}) | {'actions/thinking-voice.wav'}


def create_router(asset_root=ASSET_ROOT):
    router = APIRouter()
    asset_root = Path(asset_root)

    @router.get('/standalone')
    async def index():
        return FileResponse(ROOT / 'web/standalone.html', headers={'Cache-Control': 'no-store'})

    @router.api_route('/standalone-assets/{asset:path}', methods=['GET', 'HEAD'])
    async def resource(asset: str):
        if asset not in MEDIA:
            raise HTTPException(404)
        target = asset_root / asset
        # Refuse symlinks/junctions as well as traversal outside the independent bundle.
        if not target.is_file() or target.resolve() != target.absolute():
            raise HTTPException(404)
        return FileResponse(target)

    return router

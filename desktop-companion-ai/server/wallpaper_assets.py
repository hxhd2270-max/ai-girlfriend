"""Read-only, explicitly scoped assets for the local Wallpaper Engine wrapper."""
import os
from pathlib import Path
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

TEXT_ASSETS = frozenset({
    'style.css', 'glass.css', 'companion-plus.css', 'we-bridge.css',
    'catalog.js', 'dialogue-engine.js', 'app.js', 'companion-plus.js', 'we-bridge.js',
    'chat-ui.js', 'chat-ui.css',
})
IMAGE = re.compile(r'images/(?:empty(?:-purple)?|(?:look|thumb|opening)-[a-z0-9-]+)\.jpg\Z')
VIDEO = re.compile(r'media/(?:1080p/|1440p/)?look-[a-z0-9-]+\.webm\Z')
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = Path(os.environ.get(
    'COMPANION_WALLPAPER_ASSET_ROOT', str(PROJECT_ROOT.parent / 'PURPLE-Enhanced')))
ACTION_ROOT = PROJECT_ROOT / 'video-production/v001/deliveries/2026-10-21-first-five'
ACTION_FILES = dict(zip(('reading', 'to-listening', 'listening', 'speaking', 'to-reading'),
    ('01-安静阅读.mp4', '02-阅读转聆听.mp4', '03-安静聆听.mp4', '04-自然说话.mp4', '05-聆听转阅读.mp4')))
DIALOGUE_ROOT = PROJECT_ROOT / 'video-production/v002/2026-10-21-dialogue-three'
ACTION_PATHS = {key: ACTION_ROOT / name for key, name in ACTION_FILES.items()}
ACTION_PATHS.update({'to-speaking': DIALOGUE_ROOT / 'shot01/native.mp4',
                     'speaking': DIALOGUE_ROOT / 'shot02/native.mp4'})
ACTION_WEBM = {key: key + '.webm' for key in ACTION_PATHS}
THINKING_ROOT = PROJECT_ROOT / 'video-production/v001/trials/2026-10-22-thinking-v001'
ACTION_PATHS['thinking'] = THINKING_ROOT / 'thinking-native.mp4'
ACTION_WEBM['thinking'] = 'thinking-v001.webm'
ACTION_WEBM.update({'to-speaking': 'to-speaking-v002.webm', 'speaking': 'speaking-v002.webm'})


def create_asset_router(wallpaper_root, source_root=SOURCE_ROOT):
    router = APIRouter()
    # Portable distribution contains only its own standalone media. Never open
    # a Steam/workshop path, even if an old /purple URL is requested manually.
    if (PROJECT_ROOT / 'portable.json').is_file():
        return router
    wallpaper_root, source_root = Path(wallpaper_root), Path(source_root)

    @router.api_route('/purple/{asset:path}', methods=['GET', 'HEAD'])
    async def wallpaper_asset(asset: str, request: Request):
        if asset == 'action-player.js':
            base = PROJECT_ROOT / 'wallpaper'
        elif asset == 'actions/thinking-voice.wav':
            base, asset = THINKING_ROOT.resolve(), 'thinking-voice-audition.wav'
        elif asset in {f'actions/{key}.webm' for key in ACTION_WEBM}:
            base = (PROJECT_ROOT / 'data/action-webm').resolve()
            asset = ACTION_WEBM[asset[8:-5]]
        elif asset in {f'actions/{key}.mp4' for key in ACTION_PATHS}:
            target = ACTION_PATHS[asset[8:-4]]
            base, asset = target.parent.resolve(), target.name
        elif asset in {'chat-client.js', 'player.js'}:
            base = Path(__file__).resolve().parents[1] / 'web'
            asset = 'app.js' if asset == 'chat-client.js' else asset
        elif asset in TEXT_ASSETS:
            base = wallpaper_root.resolve()
        elif IMAGE.fullmatch(asset) or VIDEO.fullmatch(asset):
            # Bypass WE's file provider/junction handling. Never enumerate,
            # copy, modify or serve arbitrary files from the workshop folder.
            base = source_root.resolve()
        else:
            raise HTTPException(404)
        target = (base / asset).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            raise HTTPException(404)
        response = FileResponse(target)
        response.headers['Vary'] = 'Origin'
        if request.headers.get('origin') == 'null':
            # File-origin WE videos use anonymous CORS so frame capture works.
            response.headers['Access-Control-Allow-Origin'] = 'null'
        return response

    return router

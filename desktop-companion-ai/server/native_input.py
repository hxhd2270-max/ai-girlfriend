"""One local input dialog, owned by a live wallpaper chat connection."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import math
import logging

logger = logging.getLogger(__name__)


def checked_layout(value):
    """Only relative viewport geometry; never accept native handles or paths."""
    if not isinstance(value, dict):
        raise ValueError('invalid_input_layout')
    keys = ('left', 'top', 'width', 'height', 'viewportWidth', 'viewportHeight', 'fontSize')
    if any(type(value.get(k)) not in (int, float) or not math.isfinite(value[k]) for k in keys):
        raise ValueError('invalid_input_layout')
    x, y, w, h, vw, vh, font = (value[k] for k in keys)
    if not (320 <= vw <= 16384 and 200 <= vh <= 16384 and 0 <= x < vw and 0 <= y < vh
            and 160 <= w <= vw and 55 <= h <= vh and x+w <= vw+1 and y+h <= vh+1
            and 8 <= font <= 48):
        raise ValueError('invalid_input_layout')
    return {k: value[k] for k in keys}


async def run_dialog(script, draft, layout=None, progress=None):
    executable = Path(sys.executable)
    if executable.with_name('pythonw.exe').is_file():
        executable = executable.with_name('pythonw.exe')
    # The editor needs no provider credentials or local pairing capabilities.
    allowed = {'systemroot', 'windir', 'path', 'userprofile', 'localappdata',
               'appdata', 'temp', 'tmp', 'systemdrive'}
    environment = {k: v for k, v in os.environ.items() if k.lower() in allowed}
    process = await asyncio.create_subprocess_exec(
        str(executable), '-B', str(script), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=environment, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        limit=32768)
    try:
        payload = json.dumps({'text': draft, 'layout': layout}, ensure_ascii=False).encode('utf-8')
        if progress is None:
            output, _ = await asyncio.wait_for(process.communicate(payload), 900)
            output = output.splitlines()[-1] if output else output
        else:
            process.stdin.write(payload)
            await process.stdin.drain()
            process.stdin.close()
            async def read_result():
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        raise ValueError('input_dialog_failed')
                    item = json.loads(line)
                    if item.get('action') == 'ready':
                        await progress({'opened': True})
                    elif item.get('action') == 'activity':
                        await progress({'hasText': item.get('hasText') is True})
                    else:
                        await process.wait()
                        return line
            output = await asyncio.wait_for(read_result(), 900)
        if process.returncode != 0 or len(output) > 16000:
            logger.warning('Native input process failed (exit=%s).', process.returncode)
            raise ValueError('input_dialog_failed')
        result = json.loads(output)
        if (not isinstance(result, dict) or result.get('action') not in {'send', 'draft', 'stop'}
                or not isinstance(result.get('text'), str) or len(result['text']) > 2000
                or (result['action'] == 'send' and not result['text'].strip())):
            raise ValueError('invalid_input_result')
        return {'action': result['action'], 'text': result['text']}
    finally:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            await process.wait()


class NativeInputBroker:
    def __init__(self, root, runner=run_dialog):
        self.script = Path(root) / 'tools' / 'native_chat_input.py'
        self.runner = runner
        self.owner = None
        self.task = None

    def open(self, owner, draft, deliver, layout=None, progress=None):
        if self.task is not None:
            return False
        logger.warning('Opening native wallpaper input.')
        self.owner = owner

        async def work():
            try:
                try:
                    if progress is not None and self.runner is run_dialog:
                        result = await self.runner(self.script, draft, layout, progress)
                    else:
                        result = await self.runner(self.script, draft, layout)
                except (OSError, ValueError, asyncio.TimeoutError):
                    result = {'action': 'error'}
                await deliver(result)
            finally:
                self.task = None
                self.owner = None

        self.task = asyncio.create_task(work())
        return True

    async def close(self, owner):
        task = self.task
        if self.owner is owner and task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            if self.task is task:
                self.task = None
                self.owner = None

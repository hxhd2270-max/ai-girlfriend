"""Explicit local startup. Credentials remain in process memory only."""
import getpass
import os
import socket
import sys
from pathlib import Path

def main():
    os.chdir(Path(__file__).resolve().parents[1])
    sys.path.insert(0, str(Path.cwd()))
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', 8765)) == 0:
            raise SystemExit('Port 8765 is in use. Stop the existing service first.')
    print('Free quota only. Confirm BOTH qwen-flash and realtime TTS stop on free exhaustion.')
    if input('Type FREE-ONLY to confirm: ').strip() != 'FREE-ONLY':
        raise SystemExit('Cloud mode not started.')
    key = getpass.getpass('DashScope API key (hidden, not saved): ').strip()
    if not key:
        raise SystemExit('No key supplied.')
    os.environ['DASHSCOPE_API_KEY'] = key
    os.environ['COMPANION_FREE_ONLY_CONFIRMED'] = '1'
    os.environ['COMPANION_CLOUD_ENABLED'] = '1'
    key = None
    import uvicorn
    try:
        uvicorn.run('server.app:app', host='127.0.0.1', port=8765, access_log=False, log_level='warning')
    finally:
        os.environ.pop('DASHSCOPE_API_KEY', None)

if __name__ == '__main__':
    main()

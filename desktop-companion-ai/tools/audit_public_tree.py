"""Fail-closed audit for a source tree prepared for public Git history."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import stat

TEXT_SUFFIXES = frozenset({
    '', '.c', '.cmd', '.cs', '.csproj', '.css', '.html', '.ini', '.js',
    '.json', '.md', '.mjs', '.ps1', '.py', '.toml', '.txt', '.xml',
    '.yaml', '.yml',
})
FORBIDDEN_SUFFIXES = frozenset({
    '.7z', '.db', '.dll', '.dmp', '.exe', '.gz', '.key', '.log', '.mp3',
    '.mp4', '.pem', '.pfx', '.sqlite', '.sqlite3', '.tar', '.wav', '.webm',
    '.zip',
})
FORBIDDEN_PARTS = frozenset({
    '.git', '.venv', '__pycache__', 'backups', 'data', 'evidence',
    'integration-local-appdata', 'integration-runtime', 'local-appdata',
    'logs', 'node_modules', 'releases', 'roaming-appdata', 'runtime-profile',
    'video-production',
})
FORBIDDEN_NAMES = frozenset({
    '.env', 'companion-settings.json', 'neko-processes.json',
    'provider-key.dpapi', 'provider-key.txt', 'provider-routes.dpapi',
    'sessions.sqlite3',
})
PRIVATE_PATHS = (
    re.compile(r'(?i)C:[\/]Users[\/](?!<user>|username|example)[^\/\s"\']+'),
    re.compile(r'(?i)[EF]:[\/](?:桌面|知夏项目|SteamLibrary|DesktopCompanion-Wallpaper)'),
    re.compile(r'(?i)OneDrive[\/]Desktop[\/]1 \(2\)\.txt'),
)
SECRET_RULES = (
    ('private-key', re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')),
    ('github-token', re.compile(r'\b(?:gh[oprsu]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b')),
    ('aws-access-key', re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b')),
    ('generic-api-key', re.compile(r'\bsk-[A-Za-z0-9_.-]{12,}\b')),
)
SAFE_MARKERS = ('example', 'fictional', 'fixture', 'placeholder', 'redacted', 'test')


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8', errors='replace')).hexdigest()[:12]


def is_reparse(path: Path) -> bool:
    info = path.lstat()
    return path.is_symlink() or bool(
        getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def audit(root: Path) -> list[dict]:
    root = root.absolute()
    findings: list[dict] = []
    if not root.is_dir() or is_reparse(root):
        return [{'rule': 'invalid-root', 'path': '.', 'line': None, 'fingerprint': None}]
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        shown = relative.as_posix()
        if is_reparse(path):
            findings.append({'rule': 'reparse-point', 'path': shown, 'line': None, 'fingerprint': None})
            continue
        lower_parts = {part.lower() for part in relative.parts}
        if lower_parts & FORBIDDEN_PARTS:
            findings.append({'rule': 'private-directory', 'path': shown, 'line': None, 'fingerprint': None})
        if not path.is_file():
            continue
        if path.name.lower() in FORBIDDEN_NAMES:
            findings.append({'rule': 'private-filename', 'path': shown, 'line': None, 'fingerprint': None})
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            findings.append({'rule': 'forbidden-binary-or-data', 'path': shown, 'line': None, 'fingerprint': None})
        if path.stat().st_size > 5 * 1024 * 1024:
            findings.append({'rule': 'oversized-file', 'path': shown, 'line': None, 'fingerprint': None})
        if path.suffix.lower() not in TEXT_SUFFIXES or path.stat().st_size > 2 * 1024 * 1024:
            continue
        try:
            text = path.read_text(encoding='utf-8-sig')
        except (OSError, UnicodeError):
            findings.append({'rule': 'unreadable-text', 'path': shown, 'line': None, 'fingerprint': None})
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            for pattern in PRIVATE_PATHS:
                for match in pattern.finditer(line):
                    findings.append({'rule': 'private-absolute-path', 'path': shown,
                                     'line': line_number, 'fingerprint': fingerprint(match.group(0))})
            for rule, pattern in SECRET_RULES:
                for match in pattern.finditer(line):
                    value = match.group(0)
                    if rule == 'generic-api-key' and any(marker in value.lower() for marker in SAFE_MARKERS):
                        continue
                    findings.append({'rule': rule, 'path': shown, 'line': line_number,
                                     'fingerprint': fingerprint(value)})
    return findings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('--json', type=Path)
    args = parser.parse_args(argv)
    findings = audit(args.root)
    result = {'ok': not findings, 'root': str(args.root.absolute()),
              'findingCount': len(findings), 'findings': findings}
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(encoded, encoding='utf-8')
    print(encoded, end='')
    return 0 if not findings else 1


if __name__ == '__main__':
    raise SystemExit(main())

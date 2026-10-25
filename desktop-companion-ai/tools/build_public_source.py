"""Build a fresh public source tree from an explicit file allowlist."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import stat

try:
    from tools.audit_public_tree import audit
except ModuleNotFoundError:
    from audit_public_tree import audit

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT.parent / 'neko-lab'
PIN = '435a033983152e13520b1f6cb9912ea0e8c9dc75'

ROOT_FILES = (
    'requirements.txt', 'start-cloud.cmd', 'start-desktop-simulation.cmd',
    'start-desktop.cmd', 'start-neko.cmd', 'start-simulation.cmd',
    'start-standalone-simulation.cmd', 'start-standalone.cmd', 'start.cmd',
    'stop-neko.cmd',
)
PROJECT_DIRECTORIES = {
    'server': frozenset({'.py'}),
    'web': frozenset({'.css', '.html', '.js'}),
    'tests': frozenset({'.mjs', '.py'}),
}
TEST_EXCLUDES = frozenset({
    'live_smoke.py', 'test_portable.py', 'test_public_release.py', 'test_wallpaper_entry.py',
    'we-hooks.test.mjs', 'we-bridge.test.mjs',
})
TOOLS = (
    'audit_public_tree.py', 'build_public_source.py', 'first_run.py',
    'launch_windows.cmd', 'native_chat_input.py', 'portable_runtime.py',
    'start_cloud.py', 'start_neko.py', 'start_plan.py', 'start_standalone.py',
)
WALLPAPER = ('action-player.css', 'action-player.js')
HOST = (
    'app.manifest', 'build.ps1', 'DesktopChat.cs', 'DesktopMode.cs',
    'DesktopSession.cs', 'Host.cs', 'README.md',
)
LAB_FILES = (
    'gateway.py', 'integration.py', 'lab.py', 's1.py', 'test_gateway.py',
    'test_integration.py', 'test_native_bounds.py',
)


def regular(path: Path) -> Path:
    path = path.absolute()
    info = path.lstat()
    if (path.resolve() != path or path.is_symlink()
            or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise RuntimeError('Refusing linked build input: ' + str(path))
    return path


def copy(source: Path, target: Path) -> None:
    source = regular(source)
    if not source.is_file():
        raise RuntimeError('Missing allowlisted file: ' + str(source))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def inventory(root: Path) -> list[dict]:
    rows = []
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.name == 'PUBLIC-MANIFEST.json':
            continue
        with path.open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        rows.append({'path': path.relative_to(root).as_posix(),
                     'bytes': path.stat().st_size, 'sha256': digest})
    return rows


def write_setup_script(path: Path) -> None:
    lines = (
        "$ErrorActionPreference = 'Stop'",
        '$root = Split-Path -Parent $PSScriptRoot',
        "$target = Join-Path $root 'neko-lab\\upstream'",
        "if (Test-Path -LiteralPath $target) { throw 'neko-lab/upstream already exists.' }",
        'git clone https://github.com/Project-N-E-K-O/N.E.K.O.git $target',
        'git -C $target checkout ' + PIN,
        '$actual = (git -C $target rev-parse HEAD).Trim()',
        "if ($actual -ne '" + PIN + "') { throw 'Pinned commit verification failed.' }",
    )
    path.parent.mkdir(parents=True)
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def build(destination: Path) -> dict:
    destination = destination.absolute()
    if destination.exists():
        raise RuntimeError('Destination already exists; use a fresh directory.')
    regular(destination.parent)
    destination.mkdir()
    project = destination / 'desktop-companion-ai'

    for name in ROOT_FILES:
        copy(ROOT / name, project / name)
    for directory, suffixes in PROJECT_DIRECTORIES.items():
        for source in sorted((ROOT / directory).iterdir()):
            if (source.is_file() and source.suffix in suffixes
                    and not (directory == 'tests' and source.name in TEST_EXCLUDES)):
                copy(source, project / directory / source.name)
    for name in TOOLS:
        copy(ROOT / 'tools' / name, project / 'tools' / name)
    for name in WALLPAPER:
        copy(ROOT / 'wallpaper' / name, project / 'wallpaper' / name)
    for name in HOST:
        copy(ROOT / 'desktop-host' / name, project / 'desktop-host' / name)

    public_docs = ROOT / 'docs/public'
    copy(public_docs / 'README.md', destination / 'README.md')
    for name in ('OPEN_SOURCE_READINESS.md', 'PRIVACY.md', 'SECURITY.md', 'THIRD_PARTY_NOTICES.md'):
        copy(public_docs / name, destination / name)
    copy(public_docs / 'gitignore.txt', destination / '.gitignore')
    copy(public_docs / 'gitattributes.txt', destination / '.gitattributes')

    (project / 'config').mkdir(parents=True)
    (project / 'config/usage-limits.json').write_text(json.dumps({
        'chat_calls': 0, 'tts_calls': 0, 'tts_estimated_characters': 0,
    }, indent=2) + '\n', encoding='utf-8')
    (project / 'assets/companion/actions').mkdir(parents=True)
    (project / 'assets/companion/actions/.gitkeep').write_text('', encoding='ascii')

    lab_target = destination / 'neko-lab'
    for name in LAB_FILES:
        copy(LAB / name, lab_target / name)
    (lab_target / 'PINNED-UPSTREAM.txt').write_text(PIN + '\n', encoding='ascii')
    write_setup_script(destination / 'scripts/setup-neko-upstream.ps1')

    findings = audit(destination)
    if findings:
        raise RuntimeError('Public audit failed: ' + json.dumps(findings, ensure_ascii=False))
    rows = inventory(destination)
    manifest = {'schema': 1, 'source': 'explicit-allowlist', 'files': rows}
    (destination / 'PUBLIC-MANIFEST.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return {'files': len(rows), 'bytes': sum(row['bytes'] for row in rows)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('destination', type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(build(args.destination), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""Local daily reservations, never provider billing or a free-balance check.

Import has no I/O. Paths are resolved on each operation: COMPANION_DATA_DIR
defaults to <project>/data and COMPANION_USAGE_LIMITS_PATH defaults to
<project>/config/usage-limits.json. Invalid/missing configuration or storage
errors stop reservations; there is no in-memory fallback or automatic reset.

Limits have exactly the three counter keys, each null (unlimited) or a finite
nonnegative integer. Zero disables that budget. Updates apply on the next operation, retaining all
previous reservations. Only counters and dates are stored, never request text,
credentials, provider responses, or billing information.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3


ROOT = Path(__file__).resolve().parents[1]
TIMEZONE = 'Asia/Shanghai'
# Shanghai uses UTC+08 with no DST for current natural days. Avoid requiring
# a system IANA database (not normally available in Windows Python installs).
SHANGHAI = timezone(timedelta(hours=8), name=TIMEZONE)
COUNTER_KEYS = ('chat_calls', 'tts_calls', 'tts_estimated_characters')
MAX_INTEGER = 2**63 - 1
DISCLAIMER = ('本地每日已预留尝试数及估计字符，包含失败或取消的预留；'
              '不是供应商账单、免费余额或计费保证。')


class UsageError(Exception):
    """Only a fixed local code; never include configuration or database data."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _limits():
    try:
        path = Path(os.environ.get(
            'COMPANION_USAGE_LIMITS_PATH', str(ROOT / 'config' / 'usage-limits.json')))
        # Bound both parsing work and the amount of accidentally selected data read.
        with path.open('rb') as source:
            raw = source.read(4097)
        if len(raw) > 4096:
            raise ValueError('oversized configuration')
        limits = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
        if (type(limits) is not dict or set(limits) != set(COUNTER_KEYS)
                or any(value is not None and (type(value) is not int or not 0 <= value <= MAX_INTEGER)
                       for value in limits.values())):
            raise ValueError('invalid limits')
        return {key: limits[key] for key in COUNTER_KEYS}
    except (OSError, ValueError, RecursionError):
        raise UsageError('usage_config_invalid') from None


def _now():
    return datetime.now(timezone.utc)


def _operate(kind=None, estimate=0):
    try:
        data = Path(os.environ.get('COMPANION_DATA_DIR', str(ROOT / 'data')))
        data.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(data / 'usage.sqlite3', timeout=5,
                                     isolation_level=None)) as db:
            # One write transaction across the read/check/update, shared by all
            # processes. Lock waiting is bounded; no application-level retries.
            db.execute('PRAGMA synchronous = FULL')
            db.execute('BEGIN IMMEDIATE')
            limits = _limits()
            # Resolve the day after obtaining the lock, including midnight waits.
            day = _now().astimezone(SHANGHAI).date().isoformat()
            db.execute('''CREATE TABLE IF NOT EXISTS daily_usage (
                date TEXT PRIMARY KEY NOT NULL,
                chat_calls INTEGER NOT NULL CHECK(typeof(chat_calls) = 'integer' AND chat_calls >= 0),
                tts_calls INTEGER NOT NULL CHECK(typeof(tts_calls) = 'integer' AND tts_calls >= 0),
                tts_estimated_characters INTEGER NOT NULL
                    CHECK(typeof(tts_estimated_characters) = 'integer' AND tts_estimated_characters >= 0)
            )''')
            row = db.execute('''SELECT chat_calls, tts_calls, tts_estimated_characters
                                FROM daily_usage WHERE date = ?''', (day,)).fetchone()
            values = row if row is not None else (0, 0, 0)
            if any(type(value) is not int or not 0 <= value <= MAX_INTEGER for value in values):
                raise UsageError('usage_storage_unavailable')
            reserved = dict(zip(COUNTER_KEYS, values))
            db.execute('''CREATE TABLE IF NOT EXISTS daily_voice_usage (
                date TEXT PRIMARY KEY, asr_calls INTEGER NOT NULL, audio_milliseconds INTEGER NOT NULL)''')
            voice_row = db.execute('SELECT asr_calls, audio_milliseconds FROM daily_voice_usage WHERE date=?',
                                   (day,)).fetchone() or (0, 0)
            voice_reserved = dict(zip(('asr_calls', 'audio_milliseconds'), voice_row))
            if kind == 'chat':
                if limits['chat_calls'] is not None and reserved['chat_calls'] >= limits['chat_calls']:
                    raise UsageError('chat_call_limit')
                reserved['chat_calls'] += 1
            elif kind == 'tts':
                if limits['tts_calls'] is not None and reserved['tts_calls'] >= limits['tts_calls']:
                    raise UsageError('tts_call_limit')
                if (limits['tts_estimated_characters'] is not None and
                        reserved['tts_estimated_characters'] + estimate > limits['tts_estimated_characters']):
                    raise UsageError('tts_character_limit')
                reserved['tts_calls'] += 1
                reserved['tts_estimated_characters'] += estimate
            elif kind == 'asr':
                voice_reserved['asr_calls'] += 1
                voice_reserved['audio_milliseconds'] += estimate
                db.execute('''INSERT INTO daily_voice_usage VALUES(?,?,?) ON CONFLICT(date) DO UPDATE SET
                    asr_calls=excluded.asr_calls, audio_milliseconds=excluded.audio_milliseconds''',
                           (day, voice_reserved['asr_calls'], voice_reserved['audio_milliseconds']))
            if kind in ('chat', 'tts'):
                db.execute('''INSERT INTO daily_usage
                              (date, chat_calls, tts_calls, tts_estimated_characters) VALUES (?, ?, ?, ?)
                              ON CONFLICT(date) DO UPDATE SET
                              chat_calls = excluded.chat_calls, tts_calls = excluded.tts_calls,
                              tts_estimated_characters = excluded.tts_estimated_characters''',
                           (day, *(reserved[key] for key in COUNTER_KEYS)))
            # Commit before a caller can start network I/O. Nothing refunds it.
            db.execute('COMMIT')
        return dict(date=day, timezone=TIMEZONE, reserved=reserved, limits=limits, voiceReserved=voice_reserved,
                    freeBalanceVerified=False, finalBillKnown=False, disclaimer=DISCLAIMER)
    except (OSError, ValueError, sqlite3.Error):
        raise UsageError('usage_storage_unavailable') from None


def reserve(kind: str, estimate: int = 0) -> None:
    """Commit one attempt (and TTS estimate) or raise a fixed UsageError."""
    if (kind not in ('chat', 'tts') or type(estimate) is not int
            or not 0 <= estimate <= MAX_INTEGER or (kind == 'chat' and estimate != 0)):
        raise UsageError('usage_invalid_reservation')
    _operate(kind, estimate)


def usage_status() -> dict:
    """Today's snapshot, configured limits and explicit non-billing statement."""
    return _operate()


def reserve_asr(milliseconds: int) -> None:
    """Separate ASR attempts/duration, without changing the user's three limits."""
    if type(milliseconds) is not int or not 1 <= milliseconds <= 20000:
        raise UsageError('usage_invalid_reservation')
    _operate('asr', milliseconds)


def counters() -> dict[str, int]:
    """Compatibility shape: today's three reserved counters, returned by value."""
    return usage_status()['reserved']

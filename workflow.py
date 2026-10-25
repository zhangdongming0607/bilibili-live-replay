"""Shared configuration, redaction and song-index validation (standard library only)."""
import hashlib
import json
import math
import os
from pathlib import Path
import re


def load_env_file(path):
    path = Path(path).expanduser()
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"\''))


def redact(value):
    text = str(value)
    for key, secret in os.environ.items():
        if any(word in key.upper() for word in ('SECRET', 'TOKEN', 'PASSWORD', 'API_KEY', 'WEBHOOK')) and len(secret) >= 8:
            text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'https?://[^\s<>"\']+', lambda m: m.group() if re.fullmatch(r'https://www\.bilibili\.com/video/BV[0-9A-Za-z]{10}/?', m.group()) else '[URL omitted]', text)
    return re.sub(r'(?i)((?:SESSDATA|bili_jct|access_token|authorization)\s*[:=]\s*)[^\s;,]+', r'\1[REDACTED]', text)


def validate_entries(data, durations):
    entries = data.get('entries') if isinstance(data, dict) else data
    if not isinstance(entries, list) or not entries:
        raise ValueError('歌曲轴必须包含非空 entries 列表')
    previous = (0, -1)
    for n, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ValueError(f'第 {n} 条不是对象')
        part = entry.get('part', 1)
        if isinstance(part, str) and re.fullmatch(r'[Pp]?[1-9]\d*', part):
            part = int(part.lstrip('Pp'))
        start = entry.get('start')
        if type(part) is not int or part not in durations:
            raise ValueError(f'第 {n} 条分 P 无效')
        duration = durations[part]
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
            raise ValueError(f'P{part} 缺少有效媒体时长')
        if type(start) not in (int, float) or not math.isfinite(start) or not 0 <= start < duration:
            raise ValueError(f'第 {n} 条时间超出视频范围')
        if (part, start) <= previous:
            raise ValueError('条目须按分 P、起点严格递增')
        previous = (part, start)
        if not isinstance(entry.get('title'), str) or not entry['title'].strip():
            raise ValueError(f'第 {n} 条缺少标题')
        if entry.get('kind') not in ('song', 'chat', 'bgm', 'unknown'):
            raise ValueError(f'第 {n} 条 kind 无效')
        if entry.get('confidence') not in ('verified', 'candidate', 'uncertain'):
            raise ValueError(f'第 {n} 条 confidence 无效')
        if entry['confidence'] == 'verified' and not str(entry.get('evidence') or '').strip():
            raise ValueError(f'第 {n} 条 verified 需要证据')
        for key in ('title', 'artist', 'note'):
            value = entry.get(key, '')
            if not isinstance(value, str) or '\n' in value or '\r' in value:
                raise ValueError(f'第 {n} 条 {key} 必须为单行文本')
        entry['part'] = part
    return entries


def stamp(seconds):
    seconds = int(seconds)
    return f'{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}'


def render_axis(entries, multipart=False):
    lines, part = [], None
    for entry in entries:
        if multipart and entry['part'] != part:
            part = entry['part']
            lines.append(f'P{part}')
        label = entry['title'].strip()
        if entry.get('artist'):
            label += ' — ' + entry['artist'].strip()
        note = entry.get('note', '').strip()
        if entry['confidence'] != 'verified':
            note = (note + '；' if note else '') + '待核对'
        if note:
            label += ' ' + note
        lines.append(f"{stamp(entry['start'])} {label}")
    return '\n'.join(lines) + '\n'


def review_token(directory):
    directory = Path(directory)
    state = json.loads((directory / 'state.json').read_text(encoding='utf-8'))
    nonce = (state.get('axis_task') or {}).get('review_nonce', '')
    digest = hashlib.sha256((directory.name + nonce).encode())
    for name in ('axis.json', '歌曲轴.txt'):
        digest.update((directory / 'axis' / name).read_bytes())
    return digest.hexdigest()[:16]

#!/usr/bin/env python3
"""Probe a final local video; optionally decode all audio/video. Never edits media."""
import argparse
import datetime as dt
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def binding(path):
    path = Path(path).expanduser().resolve(strict=True)
    st = path.stat()
    return {'path': str(path), 'size_bytes': st.st_size, 'mtime_ns': st.st_mtime_ns}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.' + path.name + '.', suffix='.tmp', delete=False) as file:
            temp = Path(file.name)
            file.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        temp.replace(path)
    finally:
        if temp is not None and temp.exists():
            temp.unlink()


def probe(path, executable='ffprobe'):
    tool = shutil.which(executable)
    if not tool:
        raise ValueError('ffprobe is not installed or executable')
    result = subprocess.run([tool, '-v', 'error', '-show_format', '-show_streams',
                             '-of', 'json', str(path)], capture_output=True, text=True, timeout=60)
    try:
        data = json.loads(result.stdout)
        duration = float(data.get('format', {}).get('duration', 0))
    except (ValueError, TypeError):
        data, duration = {}, 0
    video = any(s.get('codec_type') == 'video' for s in data.get('streams', []))
    return {'ok': result.returncode == 0 and not result.stderr.strip() and video and math.isfinite(duration) and duration > 0,
            'returncode': result.returncode, 'stderr': result.stderr,
            'duration_seconds': duration if math.isfinite(duration) else None,
            'details': data}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--media', required=True)
    parser.add_argument('--report', required=True)
    parser.add_argument('--full-decode', action='store_true')
    parser.add_argument('--ffprobe', default='ffprobe')
    parser.add_argument('--ffmpeg', default='ffmpeg')
    args = parser.parse_args()
    path = Path(args.media).expanduser().resolve()
    report = Path(args.report).expanduser().resolve()
    log = report.with_name(report.stem + '.decode-errors.log')
    if path in (report, log):
        parser.error('Report and log must differ from the media file')
    value = {'checked_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'media': {'path': str(path)},
             'probe': {'ok': False}, 'full_decode': {'requested': args.full_decode, 'performed': False, 'ok': None},
             'coverage_verified': False,
             'limitations': ['Decoder success does not establish complete live coverage or absence of missing content.',
                             'No manual listening or visual inspection is performed by this script.']}
    ok = False
    try:
        value['media'] = binding(path)
        value['probe'] = probe(path, args.ffprobe)
        write_json(report, value)
        if args.full_decode:
            tool = shutil.which(args.ffmpeg)
            if not tool:
                raise ValueError('ffmpeg is not installed or executable')
            with log.open('wb') as errors:
                result = subprocess.run([tool, '-nostdin', '-hide_banner', '-v', 'error',
                                         '-nostats', '-stats_period', '60', '-progress', 'pipe:1',
                                         '-i', str(path), '-map', '0:v', '-map', '0:a?', '-f', 'null', '-'],
                                        stdout=subprocess.PIPE, stderr=errors, text=True)
            end = 'progress=end' in result.stdout.splitlines()
            value['full_decode'].update(performed=True, returncode=result.returncode,
                                        reached_end=end, stderr_bytes=log.stat().st_size, error_log=str(log),
                                        ok=result.returncode == 0 and end and log.stat().st_size == 0)
        value['media_unchanged'] = binding(path) == value['media']
        ok = value['probe']['ok'] and value['media_unchanged'] and (not args.full_decode or value['full_decode']['ok'])
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        value['error'] = type(error).__name__ + ': ' + str(error)
    value['requested_checks_passed'] = bool(ok)
    write_json(report, value)
    print(json.dumps({'report': str(report), 'requested_checks_passed': bool(ok),
                      'full_decode_performed': value['full_decode']['performed']}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())

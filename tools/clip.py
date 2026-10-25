#!/usr/bin/env python3
"""Export a user-reviewed time range from local video. Never uploads or overwrites."""
import argparse
import json
import math
from pathlib import Path
import subprocess


def export(media, start, end, output, copy=False):
    media, output = Path(media).expanduser().resolve(strict=True), Path(output).expanduser().resolve()
    if output.exists():
        raise ValueError('Output exists; choose a new file')
    if not all(type(t) in (int, float) and math.isfinite(t) for t in (start, end)) or not 0 <= start < end:
        raise ValueError('Require finite 0 <= start < end')
    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(media)],
                           check=True, capture_output=True, text=True)
    duration = float(json.loads(probe.stdout)['format']['duration'])
    if not math.isfinite(duration) or end > duration:
        raise ValueError('Range exceeds media duration')
    output.parent.mkdir(parents=True, exist_ok=True)
    command = ['ffmpeg', '-nostdin', '-n', '-v', 'error', '-i', str(media), '-ss', str(start), '-t', str(end-start),
               '-map', '0:v:0', '-map', '0:a:0?']
    command += ['-c', 'copy'] if copy else ['-c:v', 'libx264', '-crf', '18', '-preset', 'medium', '-c:a', 'aac']
    subprocess.run(command + ['-movflags', '+faststart', str(output)], check=True)
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(output)],
                            check=True, capture_output=True, text=True)
    return {'output': str(output), 'requested_duration': end-start,
            'actual_duration': float(json.loads(result.stdout)['format']['duration']),
            'mode': 'keyframe-limited copy' if copy else 're-encode', 'listening_verified': False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--media', required=True)
    p.add_argument('--start', type=float, required=True)
    p.add_argument('--end', type=float, required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--copy', action='store_true')
    a = p.parse_args()
    print(json.dumps(export(a.media, a.start, a.end, a.output, a.copy), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

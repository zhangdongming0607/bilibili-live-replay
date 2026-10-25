#!/usr/bin/env python3
"""Bind and validate a video-relative song index; export text and navigation LLC."""
import argparse
import json
import math
from pathlib import Path
import sys
from media_check import binding, probe, write_json


def stamp(seconds):
    seconds = int(seconds)
    return f'{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}'


def validate(document):
    if document.get('schema_version') != 1:
        raise ValueError('Expected schema_version 1; use init then migrate entries after checking offsets')
    bound = document['media']
    if binding(bound['path']) != {k: bound[k] for k in ('path', 'size_bytes', 'mtime_ns')}:
        raise ValueError('Media changed; recheck timing and bind the final file again')
    duration = bound['duration_seconds']
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise ValueError('Invalid media duration')
    entries = document['entries']
    if not entries:
        raise ValueError('No entries; identify and verify candidate timestamps first')
    previous = -1
    for i, entry in enumerate(entries):
        t = entry['start']
        if type(t) not in (int, float) or not math.isfinite(t) or not previous < t < duration:
            raise ValueError(f'Entry {i}: timestamps must increase strictly and be within media duration')
        if t < 0 or not isinstance(entry.get('title'), str) or not entry['title'].strip():
            raise ValueError(f'Entry {i}: require nonnegative start and nonempty title')
        if entry.get('kind') not in ('song', 'chat', 'bgm', 'unknown'):
            raise ValueError(f'Entry {i}: invalid kind')
        if entry.get('confidence') not in ('verified', 'candidate', 'uncertain'):
            raise ValueError(f'Entry {i}: invalid confidence')
        if entry['confidence'] == 'verified' and (not isinstance(entry.get('evidence'), str) or not entry['evidence'].strip()):
            raise ValueError(f'Entry {i}: verified needs evidence')
        previous = t
    return entries


def label(entry):
    value = entry['title'].strip()
    if entry.get('artist'):
        value += ' — ' + entry['artist']
    if entry.get('note'):
        value += '（' + entry['note'] + '）'
    if entry['confidence'] != 'verified':
        value += '【待核对】'
    if entry['kind'] != 'song':
        value = '[' + entry['kind'] + '] ' + value
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    init = modes.add_parser('init', help='Create an empty index bound to final media; never overwrite an existing index')
    init.add_argument('--media', required=True)
    init.add_argument('--output', required=True)
    init.add_argument('--ffprobe', default='ffprobe')
    build = modes.add_parser('build', help='Validate bindings and export derived navigation artifacts')
    build.add_argument('--input', required=True)
    build.add_argument('--out-dir', required=True)
    args = parser.parse_args()
    if args.mode == 'init':
        output = Path(args.output).expanduser().resolve()
        if output.exists():
            raise ValueError('Output already exists; preserve it and choose another filename')
        bound = binding(args.media)
        result = probe(bound['path'], args.ffprobe)
        if not result['ok']:
            raise ValueError('Media probe failed; run media_check.py and inspect its report')
        if binding(args.media) != bound:
            raise ValueError('Media changed during probing; wait for recording/export to finish')
        bound['duration_seconds'] = result['duration_seconds']
        write_json(output, {'schema_version': 1, 'media': bound, 'timestamp_basis': 'final_video_zero',
                            'precision': 'navigation_not_frame_accurate', 'entries': []})
        print(str(output))
        return
    document = json.loads(Path(args.input).read_text())
    entries = validate(document)
    out = Path(args.out_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    text_path, json_path, llc_path = (out / name for name in ('歌曲轴.txt', '歌曲轴.json', '歌曲导航.llc'))
    if Path(document['media']['path']) in (text_path, json_path, llc_path):
        raise ValueError('Output path collides with media')
    write_json(json_path, document)
    lines = [f"{stamp(e['start'])} {label(e)}" for e in entries]
    text_path.write_text('\n'.join(lines) + '\n')
    segments = [{'start': e['start'],
                 'end': entries[i+1]['start'] if i+1 < len(entries) else document['media']['duration_seconds'],
                 'name': label(e), 'selected': True} for i, e in enumerate(entries)]
    write_json(llc_path, {'version': 2, 'mediaFileName': Path(document['media']['path']).name, 'cutSegments': segments})
    print(json.dumps({'outputs': [str(p) for p in (text_path, json_path, llc_path)],
                      'notice': 'Navigation intervals include gaps/chat; not verified song-only cuts.'}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError) as error:
        sys.exit(str(error))

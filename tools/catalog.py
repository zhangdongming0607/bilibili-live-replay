#!/usr/bin/env python3
"""把以前几场的歌曲轴整理成曲库，给 Mac 上的 Codex 当线索。

  python3 tools/catalog.py --out data/曲库.md 2026-10-10=路径 2026-10-17=路径 ...

路径可以是歌曲轴 JSON（entries 里有 title 或 song），也可以是“HH:MM:SS 歌名 — 原唱（备注）”格式的文本。
"""
import argparse
import json
import re
import unicodedata
from pathlib import Path

LINE = re.compile(r'^(\d{2}):(\d{2}):(\d{2})\s+(.+?)(?:\s+—\s+(.+?))?(?:（.*）)?\s*$')


def key(title):
    title = unicodedata.normalize('NFKC', title).lower().replace('囍', '喜')
    return re.sub(r'[\W_]+', '', re.sub(r'[（(].*?[）)]', '', title))


def read_entries(path):
    path = Path(path).expanduser()
    if path.suffix == '.json':
        data = json.loads(path.read_text(encoding='utf-8'))
        for entry in data['entries'] if isinstance(data, dict) else data:
            if isinstance(entry, dict) and entry.get('kind', 'song') == 'song':
                yield (entry.get('title') or entry.get('song') or '').strip(), (entry.get('artist') or '').strip()
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        match = LINE.match(line.strip())
        if match and not re.search(r'聊天|休息|下播|互动', match.group(4)):
            yield match.group(4).strip(), (match.group(5) or '').strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', required=True)
    parser.add_argument('sources', nargs='+', help='场次=路径')
    args = parser.parse_args()

    songs = {}
    for source in args.sources:
        date, path = source.split('=', 1)
        for title, artist in read_entries(path):
            if not title:
                continue
            song = songs.setdefault(key(title), {'title': title, 'artists': [], 'dates': []})
            if artist and artist not in song['artists']:
                song['artists'].append(artist)
            if date not in song['dates']:
                song['dates'].append(date)

    dates = sorted({s.split('=', 1)[0] for s in args.sources})
    lines = ['# 曲库：历史歌曲线索',
             '',
             f'来自 {"、".join(dates)} 这几场的歌曲轴，共 {len(songs)} 首。只当线索：本场唱没唱、唱的是哪首，以本场的转写和检索证据为准；'
             '这里的原唱也可能有错，跟检索结果冲突时以检索为准。',
             '',
             '| 歌名 | 原唱 | 出现场次 |',
             '|---|---|---|']
    for song in sorted(songs.values(), key=lambda s: (-len(s['dates']), s['title'])):
        lines.append(f'| {song["title"]} | {"、".join(song["artists"])} | {"、".join(song["dates"])} |')
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'{len(songs)} 首 → {args.out}')


if __name__ == '__main__':
    main()

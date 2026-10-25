#!/usr/bin/env python3
"""拿一场已经整理过的录播当参照，看 replay.py 的歌曲轴和字幕差多少。

  python3 tools/compare.py axis 新的axis.json 参照的歌曲轴.json [--window 120]
  python3 tools/compare.py srt 新的.srt [参照的.srt]

参照本身也是机器整理、没逐首人工复听的，结果只说明“和参照对不对得上”，不等于准确率。
"""
import argparse
import json
import re
import statistics
import unicodedata
from pathlib import Path


def norm(title):
    title = unicodedata.normalize('NFKC', title).lower()
    title = re.sub(r'[（(【\[].*?[）)】\]]', '', title)
    return re.sub(r'[\W_]+', '', title)


def same_title(a, b):
    a, b = norm(a), norm(b)
    return bool(a and b) and (a == b or (min(len(a), len(b)) >= 2 and (a in b or b in a)))


def hms(seconds):
    seconds = int(round(seconds))
    return f'{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}'


def load_axis(path, part=1):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    entries = data['entries'] if isinstance(data, dict) else data
    return [e for e in entries if e.get('kind', 'song') == 'song' and int(str(e.get('part', 1)).lstrip('Pp')) == part]


def compare_axis(new_path, ref_path, window, part=1):
    new, ref = load_axis(new_path, part), load_axis(ref_path, part)
    used, matched, missed = set(), [], []
    for r in ref:
        candidates = [(abs(n['start'] - r['start']), i) for i, n in enumerate(new)
                      if i not in used and same_title(n['title'], r['title']) and abs(n['start'] - r['start']) <= window]
        if candidates:
            _, i = min(candidates)
            used.add(i)
            matched.append((r, new[i]))
        else:
            missed.append(r)
    extra = [n for i, n in enumerate(new) if i not in used]

    print(f'参照 {len(ref)} 首，新生成 {len(new)} 条，对上 {len(matched)} 首（歌名一致且时间差 ≤ {window} 秒）')
    print(f'召回 {len(matched) / max(len(ref), 1):.0%}：参照里的歌有多少被找到')
    print(f'精确 {len(matched) / max(len(new), 1):.0%}：新生成的条目有多少在参照里')
    if matched:
        offsets = [n['start'] - r['start'] for r, n in matched]
        print(f'时间差（新减参照，秒）：中位数 {statistics.median(offsets):+.0f}，'
              f'绝对值中位数 {statistics.median(map(abs, offsets)):.0f}，最大 {max(map(abs, offsets)):.0f}')
    print('\n对上的：')
    for r, n in matched:
        print(f'  {hms(r["start"])} {r["title"]}  →  {hms(n["start"])} {n["title"]}（{n["start"] - r["start"]:+.0f}s，{n.get("confidence", "")}）')
    print('\n漏掉的（参照有，新的没有或时间差太大）：')
    for r in missed:
        near = [n for n in new if same_title(n['title'], r['title'])]
        hint = f'  新的里在 {", ".join(hms(n["start"]) for n in near)}' if near else ''
        print(f'  {hms(r["start"])} {r["title"]} — {r.get("artist", "")}  {r.get("note", "")}{hint}')
    print('\n多出来的（新的有，参照里对不上）：')
    for n in extra:
        print(f'  {hms(n["start"])} {n["title"]} — {n.get("artist", "")}  [{n.get("confidence", "")}] {n.get("note", "")}')
        if n.get('evidence'):
            print(f'      依据：{n["evidence"][:80]}')


def parse_srt(path):
    cues = []
    for block in re.split(r'\n\s*\n', Path(path).read_text(encoding='utf-8-sig').strip()):
        lines = block.strip().splitlines()
        if len(lines) < 3 or '-->' not in lines[1]:
            continue
        start, end = (sum(float(x) * f for x, f in zip(t.strip().replace(',', '.').split(':'), (3600, 60, 1)))
                      for t in lines[1].split('-->'))
        cues.append((start, end, ' '.join(lines[2:])))
    return cues


def srt_stats(path):
    cues = parse_srt(path)
    lengths = sorted(len(text) for _, _, text in cues)
    durations = sorted(end - start for start, end, _ in cues)
    covered = sum(durations)
    overlaps = sum(1 for a, b in zip(cues, cues[1:]) if b[0] < a[1] - 0.01)

    def pct(values, p):
        return values[min(len(values) - 1, int(len(values) * p))] if values else 0

    print(f'{path}')
    print(f'  条数 {len(cues)}，覆盖时长 {covered / 60:.0f} 分钟，最后一条结束于 {hms(cues[-1][1]) if cues else "-"}')
    print(f'  每条字数：中位数 {pct(lengths, .5)}，90% 分位 {pct(lengths, .9)}，最长 {pct(lengths, 1)}；超过 30 字的 {sum(l > 30 for l in lengths)} 条')
    print(f'  每条时长：中位数 {pct(durations, .5):.1f}s，90% 分位 {pct(durations, .9):.1f}s，最长 {pct(durations, 1):.1f}s；超过 8 秒的 {sum(d > 8 for d in durations)} 条')
    print(f'  和下一条时间重叠 {overlaps} 条')
    return cues


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    axis = commands.add_parser('axis')
    axis.add_argument('new')
    axis.add_argument('ref')
    axis.add_argument('--window', type=float, default=120)
    axis.add_argument('--part', type=int, default=1)
    srt = commands.add_parser('srt')
    srt.add_argument('new')
    srt.add_argument('ref', nargs='?')
    args = parser.parse_args()
    if args.command == 'axis':
        compare_axis(args.new, args.ref, args.window, args.part)
    else:
        new = srt_stats(args.new)
        if args.ref and new:
            ref = srt_stats(args.ref)
            print('\n同一时间点的两边字幕（每 10 分钟抽一条）：')
            for minute in range(5, int(new[-1][1] // 60), 10):
                t = minute * 60
                pick = lambda cues: next((c[2] for c in cues if c[1] >= t), '')
                print(f'  {hms(t)}\n    新：{pick(new)[:60]}\n    参照：{pick(ref)[:60]}')


if __name__ == '__main__':
    main()

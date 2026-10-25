#!/usr/bin/env python3
"""Mac 端：定时问服务器有没有等着做歌曲轴的录播，有就拉材料、跑 Codex、把结果传回去。
歌曲轴推到飞书后，再去飞书群里读用户的回复：回复带版本确认码的指令才让服务器发评论置顶，其他回复当修改意见交给 Codex 改。

  python3 mac/worker.py            处理所有待做任务（launchd 每 10 分钟跑一次）
  python3 mac/worker.py --list     只列出待做任务

服务器连不上（Mac 没联网、服务器关机）就安静退出，下次再试。
"""
import argparse
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from workflow import load_env_file, redact, validate_entries
load_env_file(Path(os.environ.get('LIVE_REPLAY_CONFIG', str(REPO / 'mac' / '.env'))))
SERVER = os.environ.get('LIVE_REPLAY_SERVER', '')
REMOTE = 'python3 ' + shlex.quote(os.environ.get('LIVE_REPLAY_REMOTE_SCRIPT', '/srv/replay/replay.py'))
JOBS = Path(os.environ.get('LIVE_REPLAY_JOBS', '~/Library/Application Support/live-replay/jobs')).expanduser()
FEISHU_CHAT = os.environ.get('LIVE_REPLAY_FEISHU_CHAT', '')
REVIEWERS = set(filter(None, (v.strip() for v in os.environ.get('LIVE_REPLAY_REVIEWERS', '').split(','))))
CODEX_TIMEOUT = 3 * 3600
# 跨境线路上 SSH 握手时常被重置：复用一条连接，握手失败（返回 255）再重试
SSH_OPTIONS = ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=30',
               '-o', 'ControlMaster=auto', '-o', 'ControlPath=/tmp/live-replay-ssh-%C', '-o', 'ControlPersist=15m']
SSH_ATTEMPTS = 6
os.environ['PATH'] = os.pathsep.join([os.environ.get('PATH', ''), '/opt/homebrew/bin', '/usr/local/bin',
                                      str(Path.home() / '.local/bin')])


def log(message):
    print(f'[{datetime.now():%Y-%m-%d %H:%M:%S}] {redact(message)}', flush=True)


def run_with_retry(command, timeout):
    for attempt in range(1, SSH_ATTEMPTS + 1):
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 255 or attempt == SSH_ATTEMPTS:
            return result
        time.sleep(5 * attempt)


def ssh(command, timeout=120):
    result = run_with_retry(['ssh', *SSH_OPTIONS, SERVER, command], timeout)
    if result.returncode != 0:
        raise RuntimeError(f'ssh 失败（{result.returncode}）：{command}\n{result.stderr.strip()[-800:]}')
    return result.stdout


def scp(source, target, timeout=1800):
    result = run_with_retry(['scp', '-q', *SSH_OPTIONS, source, target], timeout)
    if result.returncode != 0:
        raise RuntimeError(f'scp 失败：{source} → {target}\n{result.stderr.strip()[-800:]}')


def remote(*args):
    return ssh(' '.join([REMOTE, *map(shlex.quote, args)]))


def remote_list(command):
    try:
        return json.loads(remote(command))
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        log(f'问服务器要任务失败（{command}），下次再试：{" ".join(str(error).split())[:400]}')
        return None


def prepare(job, directory):
    inputs = directory / 'input'
    inputs.mkdir(parents=True, exist_ok=True)
    (directory / 'job.json').write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
    lines = []
    for part in job['parts']:
        n = part['part']
        for source, name in ((part['audio'], f'P{n}.ogg'), (part['asr'], f'P{n}.paraformer.json')):
            if not (inputs / name).exists():
                log(f'下载 {name}')
                scp(f'{SERVER}:{source}', str(inputs / name))
        duration = part.get('duration') or 0
        lines.append(f'- P{n}：`P{n}.ogg`、`P{n}.paraformer.json`，时长 {int(duration) // 3600}:{int(duration) // 60 % 60:02d}:{int(duration) % 60:02d}')
    catalog = Path((os.environ.get('LIVE_REPLAY_CATALOG') or str(REPO / 'examples' / 'catalog.md'))).expanduser()
    shutil.copy(catalog, inputs / '曲库.md')
    context = os.environ.get('LIVE_REPLAY_CONTEXT', '歌回录播；语言须按本场实际内容判断。')
    model = os.environ.get('LIVE_REPLAY_WHISPER_MODEL', '')
    (inputs / 'context.md').write_text(context + '\n本地 Whisper 模型目录：' + (model or '未配置；先检查现有环境，不自动下载') + '\n', encoding='utf-8')
    shutil.copytree(REPO / 'skills' / 'bilibili-live-replay', directory / 'skill', dirs_exist_ok=True)
    template = (REPO / 'mac' / 'TASK.md').read_text(encoding='utf-8')
    (directory / 'TASK.md').write_text(template.replace('__PARTS__', '\n'.join(lines)), encoding='utf-8')


def run_codex(directory, prompt):
    codex = shutil.which('codex')
    if not codex:
        raise RuntimeError('找不到 codex 命令')
    sandbox = os.environ.get('LIVE_REPLAY_CODEX_SANDBOX', 'workspace-write')
    if sandbox not in ('workspace-write', 'danger-full-access'):
        raise ValueError('不支持的 Codex sandbox 设置')
    command = [codex, '--search', 'exec', '--skip-git-repo-check', '-s', sandbox,
               '-C', str(directory), '--json', '-o', 'last-message.md', prompt]
    model = os.environ.get('LIVE_REPLAY_CODEX_MODEL')
    if model:
        command[1:1] = ['-m', model]
    started = time.time()
    log('开始跑 Codex')
    with open(directory / 'events.jsonl', 'a') as events, open(directory / 'codex.log', 'a') as errors:
        result = subprocess.run(command, cwd=directory, stdin=subprocess.DEVNULL, stdout=events, stderr=errors,
                                timeout=CODEX_TIMEOUT)
    log(f'Codex 结束，返回码 {result.returncode}，用时 {(time.time() - started) / 60:.0f} 分钟')
    if result.returncode != 0:
        tail = (directory / 'codex.log').read_text(errors='replace')[-800:]
        raise RuntimeError(f'Codex 返回码 {result.returncode}：{tail}')


def check_outputs(directory):
    data = json.loads((directory / 'axis.json').read_text(encoding='utf-8'))
    job = json.loads((directory / 'job.json').read_text(encoding='utf-8'))
    validate_entries(data, {p['part']: p['duration'] for p in job['parts']})
    if not (directory / 'method.md').read_text(encoding='utf-8').strip():
        raise RuntimeError('method.md 是空的')


def upload_results(session, directory):
    ssh(f'mkdir -p {shlex.quote(session + "/axis")}')
    for name in ('axis.json', '歌曲轴.txt', 'method.md'):
        if (directory / name).exists():
            scp(str(directory / name), f'{SERVER}:{session}/axis/{name}')
    remote('axis-done', session)


def handle(job):
    session = job['session']
    directory = JOBS / Path(session).name
    finished = directory / 'codex-finished'
    try:
        remote('axis-start', session)
        if not finished.exists():
            prepare(job, directory)
            run_codex(directory, '阅读当前目录的 TASK.md 并完成它。')
            check_outputs(directory)
            finished.write_text(datetime.now().isoformat())
        else:
            log('上次 Codex 已经做完，只重新上传结果')
        upload_results(session, directory)
        log(f'完成：{session}')
    except Exception as error:
        log(f'失败：{session}：{error}')
        try:
            remote('axis-fail', session, str(error))
        except Exception as report_error:
            log(f'连失败也报不上去：{report_error}')


# ---------- 飞书回复 ----------

def numbered(text):
    """和服务器 replay.py 的 numbered 同一规则：分 P 标题行不编号。"""
    lines, n = [], 0
    for line in text.splitlines():
        if line.strip() and not re.fullmatch(r'P\d+', line.strip()):
            n += 1
            line = f'{n}. {line}'
        lines.append(line)
    return '\n'.join(lines)


def review_command(text, token):
    text = text.strip()
    if text == f'确认 {token}':
        return ('confirm', '')
    prefix = f'修改 {token} '
    if text.startswith(prefix) and text[len(prefix):].strip():
        return ('revise', text[len(prefix):].strip())
    return None


def reviewer_id(message):
    sender = message.get('sender', {})
    return sender.get('id') or sender.get('open_id') or sender.get('sender_id')


def feishu_replies(since):
    """Read all pages. Only explicitly configured reviewer IDs can act on a revision."""
    if not FEISHU_CHAT or not REVIEWERS:
        return []
    start = datetime.fromtimestamp(since - 120).astimezone().isoformat(timespec='seconds')
    messages, page, seen_pages = [], None, set()
    for _ in range(100):
        command = ['lark-cli', 'im', '+chat-messages-list', '--chat-id', FEISHU_CHAT, '--as', 'user',
                   '--format', 'json', '--sort', 'asc', '--page-size', '50', '--start', start]
        if page:
            command += ['--page-token', page]
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            data = {}
        if result.returncode != 0 or not data.get('ok'):
            raise RuntimeError('lark-cli 读群消息失败；请在本机检查登录和权限')
        payload = data['data']
        messages.extend(m for m in payload['messages']
                        if m.get('sender', {}).get('sender_type') == 'user'
                        and reviewer_id(m) in REVIEWERS and m.get('msg_type') == 'text'
                        and not m.get('deleted') and not m.get('updated') and m.get('content', '').strip())
        if not payload.get('has_more'):
            return messages
        page = payload.get('page_token')
        if not page or page in seen_pages:
            raise RuntimeError('飞书分页游标缺失或重复；不处理不完整的消息列表')
        seen_pages.add(page)
    raise RuntimeError('飞书消息超过本次读取上限，缩短读取范围后再试')


def report_feishu_error(error):
    log(str(error))
    flag = JOBS / '.feishu-error-notified'
    if flag.exists() and flag.read_text() == date.today().isoformat():
        return
    flag.write_text(date.today().isoformat())
    try:
        remote('notify', 'Mac 读不了飞书群消息，你在群里的回复暂时收不到。多半是飞书登录过期了，'
                         f'在 Mac 终端运行 lark-cli auth login --domain im 重新登录。\n{str(error)[-300:]}')
    except Exception as report_error:
        log(f'连失败也报不上去：{report_error}')


def revise(job, directory, messages):
    session = job['session']
    if not (directory / 'input').exists() or not (directory / 'job.json').exists():
        prepare(job, directory)
    for name in ('axis.json', '歌曲轴.txt', 'method.md'):
        scp(f'{SERVER}:{session}/axis/{name}', str(directory / name))
    backup = directory / 'versions' / f'v{job.get("revision", 0)}'
    backup.mkdir(parents=True, exist_ok=True)
    for name in ('axis.json', '歌曲轴.txt'):
        shutil.copy(directory / name, backup / name)
    current = numbered((directory / '歌曲轴.txt').read_text(encoding='utf-8').strip())
    template = (REPO / 'mac' / 'REVISE.md').read_text(encoding='utf-8')
    (directory / 'REVISE.md').write_text(
        template.replace('__AXIS__', current).replace('__MESSAGES__', '\n'.join(f'- {m}' for m in messages)),
        encoding='utf-8')
    (directory / 'last-message.md').unlink(missing_ok=True)
    run_codex(directory, '阅读当前目录的 REVISE.md 并完成它。')
    check_outputs(directory)
    summary = (directory / 'last-message.md').read_text(encoding='utf-8').strip() \
        if (directory / 'last-message.md').exists() else ''
    if summary:
        remote('notify', f'改动说明：\n{summary[:1500]}')
    upload_results(session, directory)


def handle_review(job):
    session = job['session']
    directory = JOBS / Path(session).name
    directory.mkdir(parents=True, exist_ok=True)
    seen_path = directory / 'feishu-seen.json'
    seen = set(json.loads(seen_path.read_text())) if seen_path.exists() else set()
    try:
        messages = [m for m in feishu_replies(job['first_notified_at']) if m['message_id'] not in seen]
    except Exception as error:
        report_feishu_error(error)
        return
    if not messages:
        return
    token = job.get('review_token')
    if not token:
        return
    commands = [(m, review_command(m['content'], token)) for m in messages]
    commands = [(m, cmd) for m, cmd in commands if cmd is not None]
    if not commands:
        return
    revisions = [cmd[1] for _, cmd in commands if cmd[0] == 'revise']
    confirmed = any(cmd[0] == 'confirm' for _, cmd in commands)
    try:
        if not revisions:
            if confirmed:
                remote('comment-request', session, token)
                seen.update(m['message_id'] for m, _ in commands)
                seen_path.write_text(json.dumps(sorted(seen)))
            return
        remote('notify', '收到修改意见，Mac 上的 Codex 开始改，改完发新版（期间的“确认”不算，看到新版再确认）：\n'
               + '\n'.join(revisions))
        revise(job, directory, revisions)
        seen.update(m['message_id'] for m, _ in commands)
        seen_path.write_text(json.dumps(sorted(seen)))
        log(f'修改完成：{session}')
    except Exception as error:
        log(f'处理飞书回复失败：{session}：{error}')
        try:
            remote('notify', f'处理你的回复时出错了：{str(error)[-500:]}\n可以再回复一次重试。')
        except Exception as report_error:
            log(f'连失败也报不上去：{report_error}')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--list', action='store_true')
    args = parser.parse_args()

    if not SERVER or SERVER.startswith('-') or any(c.isspace() for c in SERVER):
        parser.error('先在 mac/.env 配置有效的 LIVE_REPLAY_SERVER（SSH 主机别名或 user@host）')
    JOBS.mkdir(parents=True, exist_ok=True)
    lock = open(JOBS / '.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    jobs = remote_list('axis-pending')
    if jobs is None:
        return
    if args.list:
        print(json.dumps({'axis': jobs, 'review': remote_list('review-pending')}, ensure_ascii=False, indent=2))
        return
    for job in jobs:
        log(f'待做：{job["session"]}')
        handle(job)
    for job in remote_list('review-pending') or []:
        handle_review(job)


if __name__ == '__main__':
    sys.exit(main())

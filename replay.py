#!/usr/bin/env python3
"""录播上云第二期：检查、转写、字幕、歌曲轴、CC 字幕上传和飞书通知。

biliup 钩子调用：
  下载前处理  python3 /srv/replay/replay.py hook-start
  下载后处理  python3 /srv/replay/replay.py hook-downloaded
  后处理      python3 /srv/replay/replay.py hook-uploaded    （stdin 每行一个已上传视频的路径）

手动：
  replay.py notify-test                       发一条测试通知
  replay.py new-session 文件...               给已有录像建一个处理会话，打印会话目录
  replay.py process 会话目录 [--until 步骤]    跑流水线；已完成的步骤会跳过，可以反复重跑
  replay.py start 会话目录                    同上，放到后台的独立 systemd 单元里跑

Mac 上的 mac/worker.py 通过 SSH 调用：
  replay.py axis-pending                      列出等着做歌曲轴的会话（JSON）
  replay.py axis-start 会话目录               标记开始
  replay.py axis-done 会话目录                校验 会话目录/axis/ 里的结果并推飞书（首版和修改版都用它）
  replay.py axis-fail 会话目录 原因           标记失败并推飞书
  replay.py review-pending                    列出歌曲轴已推飞书、等用户确认的会话（JSON）
  replay.py comment-request 会话目录          用户在飞书回复了确认：发评论并置顶，稿件没审核完就等审核后再发
  replay.py comment-post 会话目录             直接发评论并置顶（已发过就跳过）
  replay.py notify 文本                       推一条飞书消息
"""
import argparse
import base64
import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from workflow import load_env_file, redact, validate_entries, render_axis, review_token

REPLAY_ROOT = Path(os.environ.get('REPLAY_HOME', '/srv/replay')).expanduser()
SESSIONS = REPLAY_ROOT / 'sessions'
DASHSCOPE = 'https://dashscope.aliyuncs.com'
UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36'
# B 站稿件 state：非负为已通过；这些负数是终态，等下去也不会过
BILI_REJECTED = {-2: '被打回', -4: '被锁定', -16: '转码失败', -100: '已删除'}
# biliup 默认的录像文件名是“主播名 + 开播时间”，例如 某某2026-10-25T13_29_56.flv
RECORDING_NAME = re.compile(r'^(.+?)\d{4}-\d{2}-\d{2}T\d{2}_\d{2}_\d{2}')


def log(message):
    print(f'[{datetime.now():%Y-%m-%d %H:%M:%S}] {redact(message)}', flush=True)


def load_env():
    global DASHSCOPE
    load_env_file(REPLAY_ROOT / '.env')
    DASHSCOPE = os.environ.get('DASHSCOPE_BASE_URL', DASHSCOPE).rstrip('/')


def env(name, default=None):
    value = os.environ.get(name) or default
    if not value:
        raise RuntimeError(f'缺少配置 {name}，请写进 {REPLAY_ROOT}/.env')
    return value


# ---------- HTTP ----------

def http(url, data=None, headers=None, method=None, timeout=60):
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        error.close()
        raise RuntimeError(f'HTTP {error.code} from {urllib.parse.urlsplit(url).hostname}; response omitted') from None


def get_json(url, headers=None, timeout=60, attempts=5):
    for attempt in range(attempts):
        try:
            return json.loads(http(url, headers=headers, timeout=timeout))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if isinstance(error, urllib.error.HTTPError) or attempt == attempts - 1:
                raise
            log(f'网络出错，{10 * (attempt + 1)} 秒后重试：{error}')
            time.sleep(10 * (attempt + 1))


def post_json(url, body, headers=None, timeout=120):
    merged = {'Content-Type': 'application/json'}
    merged.update(headers or {})
    return json.loads(http(url, json.dumps(body, ensure_ascii=False).encode(), merged, 'POST', timeout))


def post_form(url, fields, headers=None):
    merged = {'Content-Type': 'application/x-www-form-urlencoded'}
    merged.update(headers or {})
    return json.loads(http(url, urllib.parse.urlencode(fields).encode(), merged, 'POST'))


def post_multipart(url, fields, file_path, content_type, headers=None, timeout=600):
    boundary = uuid.uuid4().hex
    chunks = [f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
              for key, value in fields.items()]
    chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                  f'filename="{Path(file_path).name}"\r\nContent-Type: {content_type}\r\n\r\n'.encode())
    body = b''.join(chunks) + Path(file_path).read_bytes() + f'\r\n--{boundary}--\r\n'.encode()
    merged = {'Content-Type': f'multipart/form-data; boundary={boundary}'}
    merged.update(headers or {})
    return http(url, body, merged, 'POST', timeout)


# ---------- 飞书通知 ----------

def notify(text):
    url = os.environ.get('FEISHU_WEBHOOK')
    if not url:
        log('未配置 FEISHU_WEBHOOK，通知只写日志：\n' + text)
        return
    body = {'msg_type': 'text', 'content': {'text': redact(text)}}
    secret = os.environ.get('FEISHU_SECRET')
    if secret:
        timestamp = str(int(time.time()))
        digest = hmac.new(f'{timestamp}\n{secret}'.encode(), digestmod=hashlib.sha256).digest()
        body.update(timestamp=timestamp, sign=base64.b64encode(digest).decode())
    try:
        result = post_json(url, body)
        if result.get('code', 0) != 0:
            log(f'飞书通知失败：{result}')
    except Exception as error:
        log(f'飞书通知失败：{error}')


# ---------- 会话 ----------

class Session:
    def __init__(self, path):
        self.dir = Path(path).resolve()
        self.path = self.dir / 'state.json'
        self.state = self.load()

    def load(self):
        return json.loads(self.path.read_text(encoding='utf-8'))

    def update(self, change):
        """流水线跑着的时候，Mac 调用的 axis-*、comment-* 也会改 state.json：加锁、重读、改完再写回。"""
        with open(self.dir / '.state.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self.load()
            change(state)
            temp = self.path.with_suffix('.tmp')
            temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
            temp.replace(self.path)
        self.state = state
        return state


def streamer_of(files):
    """改过 biliup 文件名模板时认不出，返回空串；只影响曲库选择和通知文字。"""
    match = RECORDING_NAME.match(Path(files[0]).name) if files else None
    return match.group(1).strip(' _-') if match else ''


def session_streamer(state):
    return state.get('streamer') or streamer_of(state.get('files', []))


def new_session(paths):
    files = [str(Path(p.strip()).resolve()) for p in paths if p.strip()]
    if len(files) != len(set(files)):
        raise ValueError('视频清单包含重复路径')
    if not files:
        raise RuntimeError('没有收到任何视频路径')

    for filename in files:
        if not Path(filename).is_file():
            raise ValueError('录像文件不存在')
    directory = SESSIONS / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True)
    state = {'created_at': time.time(), 'files': files, 'streamer': streamer_of(files), 'done': []}
    (directory / 'state.json').write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    return directory


def spawn_process(directory):
    command = [sys.executable, str(Path(__file__).resolve()), 'process', str(directory)]
    log_path = directory / 'process.log'
    if shutil.which('systemd-run'):
        # 放进独立的 systemd 单元：biliup 由 systemd 管理时，重启 biliup 会杀掉它 cgroup 里的子进程
        subprocess.run(['systemd-run', '--quiet', '--collect', f'--unit=replay-{directory.name}',
                        f'--setenv=REPLAY_HOME={REPLAY_ROOT}',
                        f'--property=StandardOutput=append:{log_path}', f'--property=StandardError=append:{log_path}',
                        *command], check=True)
        return
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=open(log_path, 'a'), stderr=subprocess.STDOUT,
                     start_new_session=True)


# ---------- 第 1 步：检查 ----------

def step_check(session):
    results = []
    for part, file in enumerate(session.state['files'], 1):
        item = {'part': part, 'file': file, 'exists': Path(file).exists()}
        if item['exists']:
            item['size_bytes'] = Path(file).stat().st_size
            probe = subprocess.run(['ffprobe', '-v', 'error', '-of', 'json', '-show_entries',
                                    'format=duration:stream=codec_type,codec_name,width,height', file],
                                   capture_output=True, text=True)
            item['probe_ok'] = probe.returncode == 0
            if item['probe_ok']:
                info = json.loads(probe.stdout)
                item['streams'] = info.get('streams', [])
                try:
                    item['duration'] = float(info['format']['duration'])
                except (KeyError, TypeError, ValueError):
                    item['duration'] = None
            decode = subprocess.run(['nice', '-n', '10', 'ffmpeg', '-nostdin', '-v', 'error', '-i', file,
                                     '-map', '0', '-f', 'null', '-'], capture_output=True, text=True)
            errors = [line for line in decode.stderr.splitlines() if line.strip()]
            item['decode_returncode'] = decode.returncode
            item['decode_error_lines'] = len(errors)
            item['decode_errors_head'] = errors[:20]
        results.append(item)
        log(f'P{part} 检查完：{json.dumps({k: v for k, v in item.items() if k != "streams"}, ensure_ascii=False)[:300]}')
    return results


# ---------- 第 2 步：抽音频 ----------

def step_audio(session):
    results = []
    for part, file in enumerate(session.state['files'], 1):
        audio = session.dir / f'audio-{part}.ogg'
        if not audio.exists() or audio.stat().st_size == 0:
            temp = session.dir / f'audio-{part}.part.ogg'
            subprocess.run(['nice', '-n', '10', 'ffmpeg', '-nostdin', '-y', '-v', 'error', '-i', file, '-vn',
                            '-ac', '1', '-ar', '16000', '-c:a', 'libopus', '-b:a', '32k', str(temp)], check=True)
            temp.replace(audio)
        results.append({'part': part, 'audio': str(audio), 'size_bytes': audio.stat().st_size})
    return results


# ---------- 第 3 步：转写 ----------

def dashscope_headers(extra=None):
    headers = {'Authorization': f'Bearer {env("DASHSCOPE_API_KEY")}'}
    headers.update(extra or {})
    return headers


def upload_temp_file(path, model):
    policy = get_json(f'{DASHSCOPE}/api/v1/uploads?action=getPolicy&model={model}', dashscope_headers())['data']
    key = f"{policy['upload_dir']}/{Path(path).name}"
    post_multipart(policy['upload_host'], {
        'OSSAccessKeyId': policy['oss_access_key_id'],
        'Signature': policy['signature'],
        'policy': policy['policy'],
        'x-oss-object-acl': policy['x_oss_object_acl'],
        'x-oss-forbid-overwrite': policy['x_oss_forbid_overwrite'],
        'key': key,
        'success_action_status': '200',
    }, path, 'audio/ogg')
    return f'oss://{key}'


def wait_transcription(task_id, timeout=4 * 3600):
    started = time.time()
    while True:
        output = get_json(f'{DASHSCOPE}/api/v1/tasks/{task_id}', dashscope_headers())['output']
        status = output.get('task_status')
        if status == 'SUCCEEDED':
            result = output['results'][0]
            if result.get('subtask_status') != 'SUCCEEDED':
                raise RuntimeError(f'转写子任务失败：{result}')
            return result['transcription_url']
        if status in ('FAILED', 'CANCELED', 'UNKNOWN'):
            raise RuntimeError(f'转写任务 {status}：{output}')
        if time.time() - started > timeout:
            raise TimeoutError(f'转写任务 {task_id} 超过 {timeout // 3600} 小时没完成')
        time.sleep(20)


def sentences_of(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    return [s for t in data.get('transcripts', []) for s in t.get('sentences', []) if s.get('text', '').strip()]


def step_asr(session):
    model = os.environ.get('ASR_MODEL', 'paraformer-v2')
    tasks = session.state.get('asr_tasks', {})
    results = []
    for item in session.state['audio']:
        part = str(item['part'])
        result_path = session.dir / f'asr-{part}.json'
        if not result_path.exists():
            task = tasks.get(part)
            # 转写结果只保留 24 小时，太旧的任务直接重提
            if not task or time.time() - task['submitted_at'] > 20 * 3600:
                url = upload_temp_file(item['audio'], model)
                body = {'model': model, 'input': {'file_urls': [url]}, 'parameters': {}}
                hints = os.environ.get('ASR_LANGUAGE_HINTS')
                if hints:
                    body['parameters']['language_hints'] = [h.strip() for h in hints.split(',') if h.strip()]
                response = post_json(f'{DASHSCOPE}/api/v1/services/audio/asr/transcription', body,
                                     dashscope_headers({'X-DashScope-Async': 'enable',
                                                        'X-DashScope-OssResourceResolve': 'enable'}))
                task = {'task_id': response['output']['task_id'], 'submitted_at': time.time(), 'model': model}
                session.update(lambda s: s.setdefault('asr_tasks', {}).__setitem__(part, task))
                log(f'P{part} 转写任务已提交：{task["task_id"]}')
            transcription_url = wait_transcription(task['task_id'])
            result_path.write_bytes(http(transcription_url, timeout=300))
        results.append({'part': item['part'], 'result': str(result_path),
                        'sentences': len(sentences_of(result_path))})
    return results


# ---------- 第 4 步：字幕 ----------

def srt_time(milliseconds):
    milliseconds = max(0, int(milliseconds))
    hours, milliseconds = divmod(milliseconds, 3600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}'


CUE_MAX_CHARS, CUE_MAX_MS, CUE_GAP_MS = 20, 6000, 800
CLAUSE_END = '。？！；?!;'


def split_cues(sentences):
    """唱歌时一句能有十几分钟，按字的时间戳重新切成短字幕。"""
    cues = []
    for sentence in sentences:
        words = sentence.get('words') or []
        if not words:
            cues.append({'begin_time': sentence['begin_time'], 'end_time': sentence['end_time'],
                         'text': sentence['text'].strip()})
            continue
        current = None
        for word in words:
            text = word.get('text', '')
            if current and (word['begin_time'] - current['end_time'] > CUE_GAP_MS
                            or len(current['text']) + len(text) > CUE_MAX_CHARS
                            or word['end_time'] - current['begin_time'] > CUE_MAX_MS):
                cues.append(current)
                current = None
            if not current:
                current = {'begin_time': word['begin_time'], 'end_time': word['end_time'], 'text': ''}
            if current['text'][-1:].isascii() and current['text'][-1:].isalnum() and text[:1].isascii() and text[:1].isalnum():
                current['text'] += ' '
            current['text'] += text
            current['end_time'] = word['end_time']
            punctuation = word.get('punctuation', '').strip()
            if not punctuation:
                continue
            if punctuation in CLAUSE_END or len(current['text']) >= CUE_MAX_CHARS // 2:
                current['text'] += punctuation if punctuation in '？！?!' else ''
                cues.append(current)
                current = None
            elif punctuation:
                current['text'] += ' '
        if current:
            cues.append(current)
    for cue in cues:
        cue['text'] = cue['text'].strip(' ，,。')
    return [cue for cue in cues if cue['text']]


def to_srt(cues):
    return '\n'.join(f'{n}\n{srt_time(c["begin_time"])} --> {srt_time(c["end_time"])}\n{c["text"]}\n'
                     for n, c in enumerate(cues, 1))


def step_subtitles(session):
    results = []
    for item in session.state['asr']:
        cues = split_cues(sentences_of(item['result']))
        path = session.dir / f'subtitle-{item["part"]}.srt'
        path.write_text(to_srt(cues), encoding='utf-8')
        results.append({'part': item['part'], 'srt': str(path), 'cues': len(cues)})
    return results


# ---------- 第 5 步：歌曲轴（登记任务，由 Mac 上的 Codex 做） ----------

AXIS_STALE_SECONDS = 4 * 3600
AXIS_MAX_ATTEMPTS = 2


def stamp(seconds):
    seconds = int(seconds)
    return f'{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}'


def step_axis_task(session):
    return {'status': 'pending', 'created_at': time.time(), 'attempts': 0}


def axis_job_ready(state):
    job = state.get('axis_task')
    if 'axis_task' not in state.get('done', []) or not job:
        return False
    if job['status'] == 'pending':
        return True
    if job['status'] == 'failed':
        return job['attempts'] < AXIS_MAX_ATTEMPTS
    return job['status'] == 'running' and time.time() - job.get('started_at', 0) > AXIS_STALE_SECONDS


def session_parts(state):
    return [{'part': check['part'], 'duration': check.get('duration'), 'audio': audio['audio'], 'asr': asr['result']}
            for check, audio, asr in zip(state['check'], state['audio'], state['asr'])]


def axis_pending():
    jobs = []
    for state_path in sorted(SESSIONS.glob('*/state.json')):
        session = Session(state_path.parent)
        if not axis_job_ready(session.state):
            continue
        jobs.append({'session': str(session.dir), 'created_at': session.state['created_at'],
                     'streamer': session_streamer(session.state), 'parts': session_parts(session.state)})
    print(json.dumps(jobs, ensure_ascii=False))


def review_pending():
    """歌曲轴已经推到飞书、还没确认发评论的会话。Mac 去飞书群里读回复。"""
    jobs = []
    for state_path in sorted(SESSIONS.glob('*/state.json')):
        session = Session(state_path.parent)
        job = session.state.get('axis_task') or {}
        if job.get('status') != 'done' or session.state.get('comment'):
            continue
        jobs.append({'session': str(session.dir), 'first_notified_at': job.get('finished_at'),
                     'notified_at': job.get('notified_at') or job.get('finished_at'),
                     'revision': job.get('revision', 0), 'review_token': review_token(session.dir),
                     'streamer': session_streamer(session.state), 'parts': session_parts(session.state)})
    print(json.dumps(jobs, ensure_ascii=False))


def axis_update(directory, **changes):
    session = Session(directory)
    session.update(lambda s: s['axis_task'].update(changes))
    return session


def numbered(text):
    """飞书里给每首编号，方便回复“第 5 条删掉”；分 P 标题行不编号。Mac 端 worker.py 用同样的规则。"""
    lines, n = [], 0
    for line in text.splitlines():
        if line.strip() and not re.fullmatch(r'P\d+', line.strip()):
            n += 1
            line = f'{n}. {line}'
        lines.append(line)
    return '\n'.join(lines)


def axis_start(directory):
    session = Session(directory)
    attempts = session.state['axis_task'].get('attempts', 0) + 1
    session = axis_update(directory, status='running', started_at=time.time(), attempts=attempts)
    notify(f'Mac 上的 Codex 开始做歌曲轴（第 {attempts} 次），大约要二三十分钟\n会话：{session.dir}')


def validate_axis(session):
    data = json.loads((session.dir / 'axis' / 'axis.json').read_text(encoding='utf-8'))
    return validate_entries(data, {c['part']: c.get('duration') for c in session.state['check']})


def axis_done(directory):
    session = Session(directory)
    entries = validate_axis(session)
    if session.state.get('comment'):
        raise RuntimeError('评论已请求或尝试过，不能覆盖歌曲轴')
    text = render_axis(entries, len(session.state['files']) > 1)
    (session.dir / 'axis' / '歌曲轴.txt').write_text(text, encoding='utf-8')
    if session.state.get('comment'):
        raise RuntimeError('评论已请求或已发布，先人工核对，不覆盖已确认版本')
    pending = sum(1 for e in entries if e.get('confidence') != 'verified')
    job = session.state['axis_task']
    revision = job.get('revision', 0) + 1 if job.get('status') == 'done' else 0
    now = time.time()
    axis_update(directory, status='done', finished_at=job.get('finished_at', now) if revision else now,
                revised_at=now if revision else None, notified_at=now, revision=revision, entries=len(entries), review_nonce=uuid.uuid4().hex)
    token = review_token(session.dir)
    title = f'歌曲轴第 {revision} 次修改版' if revision else '歌曲轴做好了'
    streamer = session_streamer(session.state)
    title = f'{streamer} 的{title}' if streamer else title
    notify(f'{title}，共 {len(entries)} 条，其中 {pending} 条待核对。\n\n{numbered(text)}\n\n'
           f'修改：回复“修改 {token} 你的修改意见”。\n'
           f'确认并发评论：回复“确认 {token}”。只接受配置的审核人。\n'
           f'会话：{session.dir.name}；证据和做法在 axis/method.md。')



def axis_fail(directory, message):
    session = axis_update(directory, status='failed', error=message[-2000:], failed_at=time.time())
    attempts = session.state['axis_task']['attempts']
    retry = 'Mac 下次检查时会自动重试一次' if attempts < AXIS_MAX_ATTEMPTS else '不再自动重试，需要手动处理'
    notify(f'歌曲轴失败（第 {attempts} 次）：{message[-500:]}\n{retry}\n会话：{session.dir}')


# ---------- 第 6 步：找到刚投的稿件 ----------

def bili_cookies():
    data = json.loads(Path(env('BILI_COOKIE_FILE')).read_text(encoding='utf-8'))
    items = data if isinstance(data, list) else (data.get('cookie_info', {}).get('cookies') or data.get('cookies') or [])
    jar = {c['name']: c['value'] for c in items if 'name' in c and 'value' in c}
    if 'SESSDATA' not in jar or 'bili_jct' not in jar:
        raise RuntimeError('B 站登录信息里没有 SESSDATA 或 bili_jct，检查 BILI_COOKIE_FILE')
    return jar


def bili_headers(jar, referer='https://member.bilibili.com/'):
    return {'Cookie': '; '.join(f'{k}={v}' for k, v in jar.items()), 'User-Agent': UA, 'Referer': referer}


def bili_get(url, jar):
    response = get_json(url, bili_headers(jar))
    if response.get('code') != 0:
        raise RuntimeError(f'B 站接口返回 {response.get("code")} {response.get("message")}：{url.split("?")[0]}')
    return response['data']


def archive_view(bvid, jar):
    return bili_get(f'https://member.bilibili.com/x/vupre/web/archive/view?bvid={bvid}', jar)


def archive_approved(info):
    """开放浏览是 state>=0；仅自己可见的稿件审核过了也一直是 -50，要看 had_passed。"""
    state = info.get('state')
    return isinstance(state, int) and (state >= 0 or (state == -50 and bool(info.get('had_passed'))))


def step_find_archive(session):
    jar = bili_cookies()
    bvid = session.state.get('target_bvid')
    if not bvid:
        raise RuntimeError('先绑定这场的 BVID：replay.py bind-archive 会话目录 BVID；不会按标题猜稿件')
    archive = {'bvid': bvid}
    view = archive_view(archive['bvid'], jar)
    videos = view.get('videos') or []
    if len(videos) != len(session.state['files']):
        raise RuntimeError(f'稿件 {archive["bvid"]} 有 {len(videos)} 个分 P，本地有 {len(session.state["files"])} 个文件，'
                           '对不上，不自动上传字幕')
    return {'bvid': archive['bvid'], 'aid': view['archive']['aid'], 'title': view['archive'].get('title'),
            'cids': [v['cid'] for v in videos]}


# ---------- 第 7 步：审核通过后上传 CC 字幕 ----------

def step_subtitle_upload(session):
    jar, archive = bili_cookies(), session.state['find_archive']
    deadline = time.time() + 48 * 3600
    while True:
        view = archive_view(archive['bvid'], jar)
        if [v['cid'] for v in view.get('videos', [])] != archive['cids']:
            raise RuntimeError('稿件分 P 已变更，不自动上传字幕')
        info = view['archive']
        state = info.get('state')
        if archive_approved(info):
            break
        if state in BILI_REJECTED:
            raise RuntimeError(f'稿件 {archive["bvid"]} {BILI_REJECTED[state]}（{info.get("state_desc")}），不上传字幕')
        if time.time() > deadline:
            raise TimeoutError(f'稿件 {archive["bvid"]} 48 小时还没审核通过（{info.get("state_desc")}）')
        log(f'稿件 {archive["bvid"]} 还没审核通过：{state} {info.get("state_desc")}，10 分钟后再查')
        time.sleep(600)

    # 语言代码以 https://s1.hdslb.com/bfs/subtitle/subtitle_lan.json 为准，zh-Hans 会被拒
    csrf, lan = jar['bili_jct'], os.environ.get('SUBTITLE_LAN', 'zh')
    if len(session.state['subtitles']) != len(archive['cids']):
        raise RuntimeError('字幕数与稿件分 P 不一致')
    uploaded = dict(session.state.get('subtitle_uploaded', {}))
    for item, cid in zip(session.state['subtitles'], archive['cids']):
        part = str(item['part'])
        if part in uploaded:
            continue
        if item['cues'] == 0:
            uploaded[part] = {'skipped': '没有识别出文字'}
            continue
        now = int(time.time() * 1000)
        upload = json.loads(post_multipart(f'https://api.bilibili.com/x/upload/web/image?t={now}&csrf={csrf}',
                                           {'bucket': 'subtitle', 'csrf': csrf, 'content_type': 'application/x-subrip'},
                                           item['srt'], 'application/x-subrip',
                                           bili_headers(jar, 'https://member.bilibili.com/')))
        if upload.get('code') != 0:
            raise RuntimeError(f'P{part} 字幕文件上传失败：{upload.get("message")}')
        files = json.dumps([{'url': upload['data']['location'], 'lan': lan, 'subtitle_id': 0}])
        saved = post_form(f'https://api.bilibili.com/x/v2/dm/subtitle/draft/preSave?t={now}&csrf={csrf}',
                          {'oid': cid, 'type': 1, 'files': files, 'aid': archive['aid'], 'csrf': csrf},
                          bili_headers(jar, 'https://member.bilibili.com/'))
        if saved.get('code') != 0:
            raise RuntimeError(f'P{part} 字幕提交失败：{saved.get("message")}')
        uploaded[part] = {'cid': cid, 'location': upload['data']['location'], 'at': time.time()}
        session.update(lambda s: s.__setitem__('subtitle_uploaded', dict(uploaded)))
        log(f'P{part} 字幕已提交（cid {cid}）')
    return {'archive_state': state, 'parts': uploaded}


# ---------- 用户在飞书确认后：歌曲轴发评论并置顶 ----------

COMMENT_MAX_CHARS = 900
TIME_IN_TEXT = re.compile(r'(?<![\d:#])(\d{1,2}:\d{2}:\d{2})(?![\d:])')


def comment_text(session):
    """多 P 稿件评论里的时间写成“2#01:02:03”才跳到 P2（只在电脑网页、只在主评论里有效）；单 P 去掉 P1 标题行。"""
    text = (session.dir / 'axis' / '歌曲轴.txt').read_text(encoding='utf-8').strip()
    if len(session.state['files']) == 1:
        return '\n'.join(line for line in text.splitlines() if line.strip() != 'P1')
    lines, part = [], 1
    for line in text.splitlines():
        header = re.fullmatch(r'P(\d+)', line.strip())
        if header:
            part = int(header.group(1))
        else:
            line = TIME_IN_TEXT.sub(lambda m: f'{part}#{m.group(1)}', line)
        lines.append(line)
    return '\n'.join(lines)


def comment_chunks(text):
    chunks, current = [], ''
    for line in text.splitlines():
        if current and len(current) + 1 + len(line) > COMMENT_MAX_CHARS:
            chunks.append(current)
            current = ''
        current = f'{current}\n{line}' if current else line
    if current:
        chunks.append(current)
    return chunks


def bili_post(url, fields, jar, bvid):
    headers = bili_headers(jar, f'https://www.bilibili.com/video/{bvid}/')
    headers['Origin'] = 'https://www.bilibili.com'
    response = post_form(url, {**fields, 'csrf': jar['bili_jct']}, headers)
    if response.get('code') != 0:
        raise RuntimeError(f'B 站接口返回 {response.get("code")} {response.get("message")}：{url.split("?")[0]}')
    return response.get('data') or {}


def post_comment(session):
    with open(session.dir / '.comment.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        session = Session(session.dir)
        record = session.state.get('comment') or {}
        if record.get('status') == 'posted':
            return
        if record.get('status') != 'requested' or record.get('review_token') != review_token(session.dir):
            raise RuntimeError('缺少当前版本确认，或上次发送结果不明；先人工核对评论区')
        validate_axis(session)
        jar, archive = bili_cookies(), session.state['find_archive']
        view = archive_view(archive['bvid'], jar)
        if not archive_approved(view['archive']):
            raise RuntimeError('稿件尚未审核通过')
        if [v['cid'] for v in view.get('videos', [])] != archive['cids']:
            raise RuntimeError('稿件分 P 已变更，重新绑定前先核对字幕和歌曲轴')
        base = {'type': 1, 'oid': archive['aid'], 'plat': 1}
        chunks = comment_chunks(comment_text(session))
        if not chunks or any(len(c) > COMMENT_MAX_CHARS for c in chunks):
            raise RuntimeError('评论为空或单行过长')
        rpids = []
        # Persist before POST. If the process dies or response is lost, do not automatically duplicate it.
        session.update(lambda s: s['comment'].update(status='sending', rpids=[], total=len(chunks)))
        try:
            for chunk in chunks:
                data = bili_post('https://api.bilibili.com/x/v2/reply/add', {**base, 'message': chunk}, jar, archive['bvid'])
                rpids.append(data['rpid'])
                session.update(lambda s: s['comment'].update(rpids=list(rpids)))
                time.sleep(5)
        except Exception:
            session.update(lambda s: s['comment'].update(status='needs_review', rpids=list(rpids)))
            notify('评论发送未完整确认；请人工检查评论区，程序不会自动重发。')
            raise
        pin_error = None
        try:
            bili_post('https://api.bilibili.com/x/v2/reply/top', {**base, 'rpid': rpids[0], 'action': 1}, jar, archive['bvid'])
        except Exception as error:
            pin_error = redact(error)
        session.update(lambda s: s['comment'].update(status='posted', rpids=rpids, pinned=pin_error is None,
                                                    pin_error=pin_error, posted_at=time.time()))
    notify(f'歌曲轴评论已提交 {len(rpids)} 条；置顶成功：{pin_error is None}；稿件 {archive["bvid"]}')


def comment_request(directory, token):
    session = Session(directory)
    if (session.state.get('axis_task') or {}).get('status') != 'done' or token != review_token(session.dir):
        raise RuntimeError('确认码不属于当前歌曲轴版本')
    validate_axis(session)
    if session.state.get('comment'):
        raise RuntimeError('此版本已请求或尝试过评论；先核对现有状态')
    session.update(lambda s: s.__setitem__('comment', {'status': 'requested', 'requested_at': time.time(), 'review_token': token}))
    if 'find_archive' not in session.state['done']:
        notify('收到此版本确认。先绑定这场的 BVID 并继续流水线，审核通过后发评论。')
        return
    info = archive_view(session.state['find_archive']['bvid'], bili_cookies())['archive']
    if archive_approved(info):
        post_comment(session)
    else:
        notify('收到此版本确认，等待稿件审核。')


def bind_archive(directory, bvid):
    if not re.fullmatch(r'BV[0-9A-Za-z]{10}', bvid):
        raise ValueError('BVID 格式不正确')
    session = Session(directory)
    if session.state.get('subtitle_uploaded') or (session.state.get('comment') or {}).get('status') in ('sending', 'posted', 'needs_review'):
        raise RuntimeError('已有字幕或评论操作，不能重新绑定；先人工核对')
    session.update(lambda s: s.__setitem__('target_bvid', bvid))
    result = step_find_archive(session)
    def finish(state):
        state['find_archive'] = result
        if 'find_archive' not in state['done']:
            state['done'].append('find_archive')
    session.update(finish)
    print(json.dumps(result, ensure_ascii=False))


def post_comment_if_requested(session):
    session = Session(session.dir)
    if (session.state.get('comment') or {}).get('status') == 'requested':
        # 流水线可能已经跑了两天，用磁盘上最新的 replay.py 发，而不是进程启动时加载的旧代码
        subprocess.run([sys.executable, str(Path(__file__).resolve()), 'comment-post', str(session.dir)], check=True)


# ---------- 流水线 ----------

STEPS = [
    ('check', step_check, '检查录像'),
    ('audio', step_audio, '抽音频'),
    ('asr', step_asr, '转写'),
    ('subtitles', step_subtitles, '生成字幕'),
    ('axis_task', step_axis_task, '登记歌曲轴任务'),
    ('find_archive', step_find_archive, '查找稿件'),
    ('subtitle_upload', step_subtitle_upload, '上传 CC 字幕'),
]


def check_summary(session):
    lines = []
    for c in session.state['check']:
        if not c['exists']:
            lines.append(f'P{c["part"]}：文件不存在 {c["file"]}')
            continue
        size = c['size_bytes'] / 1024 ** 3
        duration = stamp(c['duration']) if c.get('duration') else '未知'
        decode = '通过' if c['decode_returncode'] == 0 and c['decode_error_lines'] == 0 else \
            f'返回码 {c["decode_returncode"]}，{c["decode_error_lines"]} 行错误'
        lines.append(f'P{c["part"]}：{duration}，{size:.2f}GB，探测{"成功" if c["probe_ok"] else "失败"}，完整解码{decode}')
    return '\n'.join(lines)


def summary_after_subtitles(session):
    subtitles = '，'.join(f'P{s["part"]} {s["cues"]} 条' for s in session.state['subtitles'])
    return (f'录像检查和字幕初稿完成（字幕等稿件审核通过后自动上传）\n\n检查：\n{check_summary(session)}\n'
            '（完整解码通过也不能证明直播没有漏录）\n\n'
            f'字幕：{subtitles}\n歌曲轴已排队，等 Mac 上的 Codex 来做；Mac 没开就一直等着。\n\n会话目录：{session.dir}')


def process(directory, until=None):
    session = Session(directory)
    with open(session.dir / '.process.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log('这个会话已经有另一个进程在处理，退出')
            return
        _process_steps(session, until)


def _process_steps(session, until):
    for key, function, label in STEPS:
        if until and [k for k, _, _ in STEPS].index(key) > [k for k, _, _ in STEPS].index(until):
            return
        if key in session.state['done']:
            continue
        log(f'开始：{label}')
        try:
            result = function(session)
        except Exception as error:
            record = {'step': key, 'error': redact(error), 'at': time.time()}
            session.update(lambda s: s.setdefault('errors', []).append(record))
            notify(f'录播处理失败：{label}\n{error}\n\n会话：{session.dir}\n'
                   f'修好后在服务器上运行：python3 {Path(__file__).resolve()} start {session.dir}')
            raise

        def finish(state, key=key, result=result):
            state[key] = result
            state['done'].append(key)
        session.update(finish)
        log(f'完成：{label}')
        if key == 'axis_task':
            notify(summary_after_subtitles(session))
        if key == until:
            log(f'按 --until {until} 停在这里')
            return
    archive = session.state['find_archive']
    notify(f'CC 字幕已提交：{archive["title"]}\nhttps://www.bilibili.com/video/{archive["bvid"]}\n'
           '字幕可能还要等 B 站审核才显示。请在创作中心核对可见范围，转公开由你决定。')
    try:
        post_comment_if_requested(session)
    except Exception as error:
        log(f'发评论失败：{error}')


def main():
    load_env()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('hook-start')
    commands.add_parser('hook-downloaded')
    commands.add_parser('hook-uploaded')
    commands.add_parser('notify-test')
    create = commands.add_parser('new-session')
    create.add_argument('files', nargs='+')
    run = commands.add_parser('process')
    run.add_argument('session')
    run.add_argument('--until', choices=[key for key, _, _ in STEPS])
    commands.add_parser('start').add_argument('session')
    commands.add_parser('axis-pending')
    commands.add_parser('review-pending')
    for name in ('axis-start', 'axis-done', 'axis-fail', 'comment-request', 'comment-post'):
        command = commands.add_parser(name)
        command.add_argument('session')
        if name == 'comment-request':
            command.add_argument('token')
        if name == 'axis-fail':
            command.add_argument('message')
    bind = commands.add_parser('bind-archive')
    bind.add_argument('session')
    bind.add_argument('bvid')
    commands.add_parser('notify').add_argument('message')
    args = parser.parse_args()

    if args.command == 'hook-start':
        notify('检测到开播，服务器开始录制')
    elif args.command == 'hook-downloaded':
        notify('直播结束，录制完成，开始上传投稿')
    elif args.command == 'hook-uploaded':
        directory = new_session(sys.stdin.read().splitlines())
        streamer = Session(directory).state['streamer']
        notify(f'{streamer + "：" if streamer else ""}投稿已提交，开始后续处理（检查、转写、字幕，歌曲轴交给 Mac）\n会话：{directory}')
        spawn_process(directory)
    elif args.command == 'notify-test':
        notify('录播服务器测试通知：能看到这条说明飞书机器人配好了')
    elif args.command == 'new-session':
        print(new_session(args.files))
    elif args.command == 'process':
        process(args.session, args.until)
    elif args.command == 'start':
        spawn_process(Path(args.session).resolve())
    elif args.command == 'axis-pending':
        axis_pending()
    elif args.command == 'review-pending':
        review_pending()
    elif args.command == 'comment-request':
        comment_request(args.session, args.token)
    elif args.command == 'bind-archive':
        bind_archive(args.session, args.bvid)
    elif args.command == 'comment-post':
        post_comment(Session(args.session))
    elif args.command == 'notify':
        notify(args.message)
    elif args.command == 'axis-start':
        axis_start(args.session)
    elif args.command == 'axis-done':
        axis_done(args.session)
    elif args.command == 'axis-fail':
        axis_fail(args.session, args.message)


if __name__ == '__main__':
    main()

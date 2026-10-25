#!/usr/bin/env python3
"""Manually start one live recording; reconnect that session, then exit. No live-start polling."""
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import tempfile
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ROOT = WORK = OUT = STATE = EVENTS = STOP = None
CONFIG = {}


def configure(workspace):
    global ROOT, WORK, OUT, STATE, EVENTS, STOP
    ROOT = Path(workspace).expanduser().resolve()
    WORK = ROOT / 'work'
    OUT = ROOT / 'outputs' / '直播录制'
    STATE = WORK / 'live-watch-status.json'
    EVENTS = WORK / 'live-watch-events.jsonl'
    STOP = WORK / 'stop-live-watch'


def load_config():
    global CONFIG, FFMPEG, FFPROBE, INTERVAL, MIN_FREE, HEADERS, TZ, UID, ROOM, STALL
    CONFIG = json.loads((WORK / 'live-watch-config.json').read_text())
    UID, ROOM = CONFIG['uid'], CONFIG['room_id']
    if type(UID) is not int or type(ROOM) is not int or min(UID, ROOM) <= 0:
        raise ValueError('uid and room_id must be positive integers')
    INTERVAL = int(CONFIG.get('connection_check_seconds', 30))
    STALL = int(CONFIG.get('stall_seconds', 120))
    gib = float(CONFIG.get('min_free_gib', 15))
    if INTERVAL < 5 or STALL < INTERVAL or not 1 <= gib <= 10000:
        raise ValueError('Require connection check >= 5, stall >= check interval, min_free_gib >= 1')
    MIN_FREE = int(gib * 1024**3)
    TZ = CONFIG.get('timezone', 'UTC')
    ZoneInfo(TZ)
    FFMPEG = shutil.which(CONFIG.get('ffmpeg', 'ffmpeg'))
    FFPROBE = shutil.which(CONFIG.get('ffprobe', 'ffprobe'))
    if not FFMPEG or not FFPROBE:
        raise ValueError('ffmpeg and ffprobe must be installed and executable')
    HEADERS = {'User-Agent': 'Mozilla/5.0', 'Referer': f'https://live.bilibili.com/{ROOM}'}
    if STATE.exists():
        old = json.loads(STATE.read_text())
        if old.get('room_id') != ROOM or old.get('uid', UID) != UID:
            raise ValueError('Workspace belongs to another room; inspect it before changing targets')


def capture_pids():
    # Catch a surviving capture even if a monitor died between Popen and state save.
    result = subprocess.run(['/bin/ps', '-ww', '-axo', 'pid=,command='],
                            capture_output=True, text=True, check=True)
    found = set()
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) == 2 and fields[1].startswith(FFMPEG + ' ') and str(OUT) + '/' in fields[1]:
            found.add(int(fields[0]))
    return found


def now():
    return dt.datetime.now(ZoneInfo(TZ)).isoformat()


def write_json(path, value):
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.' + path.name + '.', suffix='.tmp', delete=False) as file:
            temp = Path(file.name)
            file.write(json.dumps(value, ensure_ascii=False, indent=2))
        temp.replace(path)
    finally:
        if temp is not None and temp.exists():
            temp.unlink()


def room_info():
    request = urllib.request.Request(
        f'https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld?mid={UID}',
        headers=HEADERS)
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.load(response)
    if result.get('code') != 0 or result.get('data', {}).get('roomid') != ROOM:
        raise RuntimeError('Unexpected API response or live room')
    if type(result['data'].get('liveStatus')) is not int or result['data']['liveStatus'] not in (0, 1, 2):
        raise RuntimeError('Unknown live status; not an offline result')
    return result['data']


def stream_url(info):
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(info.get('link', '')).query)
    url = query.get('playurl_h264', query.get('h264_session', ['']))[0]
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not (parsed.hostname or '').endswith('.bilivideo.com'):
        raise RuntimeError('Public H264 stream URL unavailable')
    return url


def stop_process(process):
    if process and process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


class ExistingCapture:
    """Manage a surviving FFmpeg process after its monitor is replaced."""
    def __init__(self, pid, filename):
        self.pid = pid
        self.filename = filename
        self.returncode = None

    def poll(self):
        result = subprocess.run(['/bin/ps', '-ww', '-p', str(self.pid), '-o', 'stat=,command='],
                                capture_output=True, text=True)
        line = result.stdout.strip()
        fields = line.split(None, 1)
        if len(fields) != 2 or fields[0].startswith('Z') or not fields[1].startswith(FFMPEG + ' ') or not fields[1].endswith(' ' + self.filename):
            self.returncode = 0
        return self.returncode

    def send_signal(self, signum):
        if self.poll() is None:
            try:
                os.kill(self.pid, signum)
            except ProcessLookupError:
                self.returncode = 0

    def terminate(self):
        self.send_signal(signal.SIGTERM)

    def kill(self):
        self.send_signal(signal.SIGKILL)

    def wait(self, timeout=None):
        deadline = time.monotonic() + timeout if timeout is not None else float('inf')
        while self.poll() is None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired('existing FFmpeg', timeout)
            time.sleep(.1)
        return self.returncode


class Watcher:
    def __init__(self):
        self.process = None
        self.session = None
        self.completed = False
        self.capture_attempts = 0
        self.last_size = 0
        self.last_growth = time.monotonic()
        self.state = {'pid': os.getpid(), 'room_id': ROOM,
                      'uid': UID, 'anchor': CONFIG.get('anchor', ''), 'state': 'starting',
                      'connection_check_seconds': INTERVAL, 'timezone': TZ, 'monitor_script': str(Path(__file__).resolve()),
                      'output_directory': str(OUT), 'api_errors': 0}

    def recover_session(self):
        # Called only while holding the monitor's exclusive file lock.
        alive = capture_pids()
        if not STATE.exists():
            if alive:
                raise RuntimeError('Untracked FFmpeg exists; inspect output and adopt or stop it before recovery')
            return
        previous = json.loads(STATE.read_text())
        session = previous.get('session')
        if not session or session.get('ended_at') or not session.get('files'):
            if alive:
                raise RuntimeError('FFmpeg exists without unfinished session metadata; recovery needs inspection')
            return
        directory = Path(session['directory']).resolve()
        if OUT.resolve() not in directory.parents or any(Path(f).resolve().parent != directory for f in session['files']):
            raise RuntimeError('Unexpected recovery recording path')
        self.session = session
        pid = previous.get('ffmpeg_pid')
        if isinstance(pid, int):
            capture = ExistingCapture(pid, session['files'][-1])
            if capture.poll() is None:
                self.process = capture
                self.state['ffmpeg_pid'] = pid
                self.last_size = Path(session['files'][-1]).stat().st_size
                self.last_growth = time.monotonic()
        if alive - ({self.process.pid} if self.process else set()):
            raise RuntimeError('Additional untracked FFmpeg exists; do not start duplicate recording')
        if isinstance(pid, int) and self.process is None:
            ps = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True)
            if ps.stdout.strip() and not ps.stdout.strip().startswith('Z'):
                raise RuntimeError('Old FFmpeg PID has ambiguous identity; inspect before recovery')
        self.event('monitor_resumed', directory=str(directory), existing_capture=self.process is not None)

    def event(self, kind, **fields):
        with EVENTS.open('a') as file:
            file.write(json.dumps({'at': now(), 'kind': kind, **fields}, ensure_ascii=False) + '\n')

    def save(self):
        self.state['updated_at'] = now()
        self.state['free_bytes'] = shutil.disk_usage(OUT).free
        self.state['session'] = self.session
        if self.session:
            self.session['bytes_recorded'] = sum(Path(p).stat().st_size for p in self.session['files'] if Path(p).exists())
            write_json(Path(self.session['directory']) / 'recording.json', self.session)
        write_json(STATE, self.state)

    def finish(self, reason):
        stop_process(self.process)
        self.process = None
        self.state.pop('ffmpeg_pid', None)
        if not self.session:
            return
        self.session.update(ended_at=now(), finish_reason=reason)
        checks = []
        for filename in self.session['files']:
            try:
                probe = subprocess.run([FFPROBE, '-v', 'error', '-show_entries',
                                    'format=duration,size:stream=codec_type,codec_name',
                                    '-of', 'json', filename], capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired) as error:
                checks.append({'file': filename, 'probe_ok': False, 'warning': type(error).__name__})
                continue
            try:
                details = json.loads(probe.stdout)
            except ValueError:
                details = {}
            checks.append({'file': filename, 'probe_ok': probe.returncode == 0 and bool(details.get('format')) and not probe.stderr.strip(),
                           'details': details, 'warning': probe.stderr[-1500:]})
        self.session['media_probes'] = checks
        self.session['validation'] = 'metadata_only; full decode and gap-free coverage not verified'
        self.save()
        self.event('recording_finished', session=self.session)
        self.state['last_finished'] = self.session
        self.session = None

    def capture_active(self):
        """Check local process/file only; a healthy recording needs no room API request."""
        if not self.process or self.process.poll() is not None:
            return False
        path = Path(self.session['files'][-1])
        size = path.stat().st_size if path.exists() else 0
        if size > self.last_size:
            self.last_size, self.last_growth = size, time.monotonic()
            self.capture_attempts = 0
            self.state['api_errors'] = 0
            self.state.pop('last_error', None)
        elif time.monotonic() - self.last_growth >= STALL:
            self.event('stream_stalled', session_directory=self.session['directory'])
            stop_process(self.process)
            return False
        self.state['state'] = 'recording' if self.last_size > 0 else 'connecting'
        return True

    def tick(self, info):
        if self.completed:
            return
        self.state['last_check_at'] = now()
        self.state['live_status'] = info.get('liveStatus')
        if shutil.disk_usage(OUT).free < MIN_FREE:
            self.finish('low_disk_space')
            self.event('low_disk_space', minimum_free_bytes=MIN_FREE)
            self.state['state'] = 'stopped_low_disk'
            self.completed = True
            self.save()
            return
        if info.get('liveStatus') != 1:
            had_session = self.session is not None
            self.finish('broadcast_ended')
            self.state['state'] = 'finished' if had_session else 'not_live'
            self.completed = True
            if not had_session:
                self.event('not_live')
            self.save()
            return
        if self.capture_active():
            self.save()
            return
        if not self.process or self.process.poll() is not None:
            url = stream_url(info)
            if not self.session:
                stamp = dt.datetime.now(ZoneInfo(TZ)).strftime('%Y-%m-%d_%H-%M-%S_%f')
                directory = OUT / stamp
                directory.mkdir(parents=True)
                self.session = {'started_at': now(), 'title': info.get('title', ''),
                                'room_id': ROOM, 'directory': str(directory), 'files': []}
                self.event('broadcast_detected', directory=str(directory), title=self.session['title'])
            directory = Path(self.session['directory'])
            part = directory / ('part-%03d.mp4' % (len(self.session['files']) + 1))
            command = [FFMPEG, '-hide_banner', '-loglevel', 'warning', '-nostdin', '-n',
                       '-rw_timeout', '20000000', '-user_agent', HEADERS['User-Agent'],
                       '-referer', HEADERS['Referer'], '-i', url, '-map', '0:v:0', '-map', '0:a:0?',
                       '-c', 'copy', '-movflags', '+frag_keyframe+empty_moov+default_base_moof',
                       '-f', 'mp4', str(part)]
            with (directory / 'ffmpeg.log').open('ab') as log:
                self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log)
            self.session['files'].append(str(part))
            self.capture_attempts += 1
            self.last_size, self.last_growth = 0, time.monotonic()
            self.event('capture_started', file=str(part), pid=self.process.pid)
        self.state['state'] = 'recording' if self.last_size > 0 else 'connecting'
        self.state['ffmpeg_pid'] = self.process.pid
        self.save()

    def check_once(self):
        if self.completed:
            return
        try:
            # Disk protection remains active even when the public API fails.
            if shutil.disk_usage(OUT).free < MIN_FREE:
                self.tick({'liveStatus': self.state.get('live_status', 0)})
            elif self.capture_active():
                self.state['last_check_at'] = now()
            elif self.capture_attempts >= 5:
                self.finish('reconnect_failed')
                self.state['state'] = 'reconnect_failed'
                self.completed = True
                self.event('connection_error', error='Five capture attempts failed to produce growing media')
            else:
                # One request on explicit start, or after the current capture disconnects.
                self.tick(room_info())
        except Exception as error:
            self.state['api_errors'] += 1
            self.state['last_error'] = type(error).__name__ + ': public API or recorder check failed; inspect local logs (redact stream URLs)'
            self.state['state'] = 'connection_error'
            if not self.session or self.state['api_errors'] >= 5:
                reason = 'reconnect_failed' if self.session else 'start_failed'
                self.finish(reason)
                self.state['state'] = reason
                self.completed = True
                self.event('connection_error', error=self.state['last_error'])
        else:
            self.state['api_errors'] = 0
            self.state.pop('last_error', None)
        self.save()

    def run(self):
        self.recover_session()
        stopping = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stopping.set())
        signal.signal(signal.SIGTERM, lambda *_: stopping.set())
        awake = subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) if Path('/usr/bin/caffeinate').exists() else None
        self.event('monitor_started', pid=os.getpid())
        try:
            while not self.completed and not STOP.exists() and not stopping.is_set():
                self.check_once()
                if not self.completed:
                    stopping.wait(INTERVAL)
        finally:
            try:
                if not self.completed:
                    self.finish('monitor_stopped')
                    self.state['state'] = 'stopped'
                self.save()
                self.event('monitor_stopped')
            finally:
                stop_process(self.process)
                if awake:
                    awake.terminate()


def start_recording():
    if STOP.exists():
        print(json.dumps({'state': 'user_stopped', 'restarted': False}))
        return
    with (WORK / 'live-watch.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'state': 'already_running'}))
            return
        fcntl.flock(lock, fcntl.LOCK_UN)
    with (WORK / 'live-watch-service.log').open('ab') as log:
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--workspace', str(ROOT), '--run'],
                                   cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   start_new_session=True)
    print(json.dumps({'state': 'started', 'pid': process.pid}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True, help='Existing project root with work/live-watch-config.json')
    modes = parser.add_mutually_exclusive_group()
    for mode in ('run', 'start', 'stop', 'probe'):
        modes.add_argument('--' + mode, action='store_true')
    args = parser.parse_args()
    configure(args.workspace)
    if args.stop:
        WORK.mkdir(parents=True, exist_ok=True)
        STOP.touch()
        if STATE.exists():
            old = json.loads(STATE.read_text())
            pid = old.get('pid')
            if isinstance(pid, int):
                result = subprocess.run(['/bin/ps', '-ww', '-p', str(pid), '-o', 'command='],
                                        capture_output=True, text=True)
                if str(Path(__file__).resolve()) in result.stdout and '--run' in result.stdout and str(ROOT) in result.stdout:
                    os.kill(pid, signal.SIGTERM)
        print(json.dumps({'state': 'stop_requested', 'verify_process_exit': True}))
        return
    if not any((args.run, args.start, args.probe)):
        print(STATE.read_text() if STATE.exists() else '{"state": "not_started"}')
        return
    if STOP.exists() and (args.run or args.start):
        print('{"state": "user_stopped", "restarted": false}')
        return
    load_config()
    if args.probe:
        info = room_info()
        print(json.dumps({key: info.get(key) for key in ('roomid', 'liveStatus', 'title')}, ensure_ascii=False))
        return
    WORK.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    if args.start:
        start_recording()
    elif args.run:
        with (WORK / 'live-watch.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            if not STOP.exists():
                Watcher().run()


if __name__ == '__main__':
    main()

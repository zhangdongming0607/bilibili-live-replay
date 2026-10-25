#!/usr/bin/env python3
"""Generate (do not install) a launchd plist with this checkout's real paths."""
import argparse
from pathlib import Path
import plistlib
import sys


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', required=True)
    p.add_argument('--interval', type=int, default=600)
    a = p.parse_args()
    if a.interval < 60:
        p.error('interval must be at least 60 seconds')
    repo = Path(__file__).resolve().parent.parent
    logs = Path.home() / 'Library/Application Support/live-replay'
    logs.mkdir(parents=True, exist_ok=True)
    data = {'Label': 'org.bilibili-live-replay.worker', 'ProgramArguments': [sys.executable, str(repo/'mac/worker.py')],
            'StartInterval': a.interval, 'RunAtLoad': True, 'WorkingDirectory': str(repo),
            'StandardOutPath': str(logs/'worker.log'), 'StandardErrorPath': str(logs/'worker.log')}
    output = Path(a.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('xb') as f:
        plistlib.dump(data, f)
    print(output)


if __name__ == '__main__':
    main()

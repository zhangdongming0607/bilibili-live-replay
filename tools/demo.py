#!/usr/bin/env python3
"""Offline synthetic-media walkthrough. No accounts, network, ASR, notifications or uploads."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import replay
from tools.clip import export


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', required=True)
    a = p.parse_args()
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    video = out/'synthetic.mp4'
    if not video.exists():
        subprocess.run(['ffmpeg','-nostdin','-n','-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=15',
                        '-f','lavfi','-i','sine=frequency=440:sample_rate=16000','-t','3','-c:v','mpeg4',
                        '-c:a','aac',str(video)],check=True)
    replay.SESSIONS = out/'sessions'
    with patch.object(replay,'http',side_effect=AssertionError('network is disabled in demo')), patch.object(replay,'notify'):
        session = replay.Session(replay.new_session([str(video)]))
        for key, function in [('check',replay.step_check),('audio',replay.step_audio)]:
            result = function(session)
            session.update(lambda s,key=key,result=result:s.update({key:result}))
        assert all(c['probe_ok'] and c['decode_returncode']==0 and c['decode_error_lines']==0 for c in session.state['check'])
        asr = session.dir/'asr-1.json'
        asr.write_text(json.dumps({'transcripts':[{'sentences':[{'text':'演示字幕','begin_time':200,'end_time':1500,
                         'words':[{'text':'演示','begin_time':200,'end_time':700},{'text':'字幕','begin_time':800,'end_time':1500}]}]}]},ensure_ascii=False))
        session.update(lambda s:s.update(asr=[{'part':1,'result':str(asr)}]))
        subtitles = replay.step_subtitles(session)
        session.update(lambda s:s.update(subtitles=subtitles,axis_task=replay.step_axis_task(session)))
        axis = session.dir/'axis';axis.mkdir()
        (axis/'axis.json').write_text(json.dumps({'entries':[{'part':1,'start':0.2,'title':'虚构示例曲','artist':'示例作者',
                      'kind':'song','confidence':'candidate','evidence':'Synthetic fixture, not recognition','note':'仅演示'}]},ensure_ascii=False))
        (axis/'method.md').write_text('Generated synthetic data. No recognition or listening performed.\n')
        replay.axis_done(session.dir)
        clip = export(video,0.5,1.5,session.dir/'clip.mp4')
        assert abs(clip['actual_duration']-1) < 0.2
        report={'offline':True,'checks_passed':True,'session':str(session.dir),'subtitles':subtitles,'clip':clip,
                'live_recording_tested':False,'asr_tested':False,'external_writes_performed':False}
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()

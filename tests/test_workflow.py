import copy
import importlib.util
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import replay
from workflow import validate_entries, render_axis, review_token, redact
spec = importlib.util.spec_from_file_location('worker', ROOT/'mac/worker.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


def entry(**changes):
    return dict({'part':1,'start':1,'title':'Example','artist':'','kind':'song','confidence':'verified',
                 'evidence':'synthetic fixture','note':''}, **changes)


class AxisValidation(unittest.TestCase):
    def test_rejects_bad_timestamps(self):
        for t in (-1,10,float('nan'),float('inf'),True,'2'):
            with self.subTest(t=t), self.assertRaises(ValueError):
                validate_entries([entry(start=t)],{1:10})

    def test_requires_nonempty_evidence_and_valid_part(self):
        for e in (entry(evidence=''),entry(part=2),entry(kind='other'),entry(confidence='done'),entry(title='')):
            with self.subTest(e=e), self.assertRaises(ValueError):validate_entries([e],{1:10})
        with self.assertRaises(ValueError):validate_entries([],{1:10})

    def test_normalizes_legacy_part_and_requires_order(self):
        self.assertEqual(validate_entries([entry(part='P1')],{1:10})[0]['part'],1)
        with self.assertRaises(ValueError):validate_entries([entry(start=2),entry(start=1)],{1:10})
        with self.assertRaises(ValueError):validate_entries([entry(),entry()],{1:10})

    def test_prevents_text_header_injection(self):
        with self.assertRaises(ValueError):validate_entries([entry(title='song\nP2')],{1:10})

    def test_render_preserves_uncertainty_and_part(self):
        es=validate_entries([entry(),entry(part=2,confidence='candidate',evidence='')],{1:10,2:10})
        text=render_axis(es,True)
        self.assertIn('P2\n00:00:01 Example 待核对',text)

    def test_redacts_access_urls_and_environment_credentials(self):
        with patch.dict(os.environ,{'DASHSCOPE_API_KEY':'synthetic-private-value'}):
            value=redact('synthetic-private-value https://example.org/hook/secret?signature=private SESSDATA=secret')
        self.assertNotIn('synthetic-private-value',value)
        self.assertNotIn('signature',value)
        self.assertNotIn('SESSDATA=secret',value)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.old=replay.SESSIONS;replay.SESSIONS=self.root/'sessions'
        self.addCleanup(setattr,replay,'SESSIONS',self.old)
        self.media=self.root/'synthetic.mp4';self.media.write_bytes(b'fixture')
        self.session=replay.Session(replay.new_session([str(self.media)]))
        self.session.update(lambda s:s.update(check=[{'part':1,'duration':10}],axis_task={'status':'pending','attempts':0}))
        axis=self.session.dir/'axis';axis.mkdir()
        (axis/'axis.json').write_text(json.dumps({'entries':[entry()]}))
        (axis/'歌曲轴.txt').write_text('incorrect text')
        (axis/'method.md').write_text('Synthetic evidence.')
        self.notify=patch.object(replay,'notify').start();self.addCleanup(patch.stopall)
        self.network=patch.object(replay,'http',side_effect=AssertionError('unexpected network')).start()

    def test_new_session_rejects_missing_and_names_are_unique(self):
        with self.assertRaises(ValueError):replay.new_session([str(self.root/'missing')])
        self.assertNotEqual(self.session.dir,replay.new_session([str(self.media)]))

    def test_axis_text_derived_from_validated_json_and_token_changes(self):
        replay.axis_done(self.session.dir)
        text=(self.session.dir/'axis/歌曲轴.txt').read_text()
        self.assertIn('00:00:01 Example',text)
        a=review_token(self.session.dir)
        replay.axis_done(self.session.dir)
        self.assertNotEqual(a,review_token(self.session.dir))

    def test_stale_confirmation_rejected_and_current_queues(self):
        replay.axis_done(self.session.dir)
        token=review_token(self.session.dir)
        with self.assertRaises(RuntimeError):replay.comment_request(self.session.dir,'old')
        replay.comment_request(self.session.dir,token)
        self.assertEqual(replay.Session(self.session.dir).state['comment']['status'],'requested')
        with self.assertRaises(RuntimeError):replay.comment_request(self.session.dir,token)

    def test_until_already_done_does_not_run_later_steps(self):
        self.session.update(lambda s:s.update(done=['check']))
        later=Mock(side_effect=AssertionError('ran past boundary'))
        with patch.object(replay,'STEPS',[('check',Mock(),'Check'),('asr',later,'ASR')]):
            replay.process(self.session.dir,until='check')
        later.assert_not_called()

    def test_no_title_guessing(self):
        with patch.object(replay,'bili_cookies',return_value={}):
            with self.assertRaisesRegex(RuntimeError,'绑定'):replay.step_find_archive(self.session)

    def prepare_comment(self):
        replay.axis_done(self.session.dir)
        archive={'bvid':'BV0000000000','aid':1,'cids':[2]}
        self.session.update(lambda s:s.update(find_archive=archive,done=['find_archive'],
              comment={'status':'requested','review_token':review_token(self.session.dir)}))
        self.view={'archive':{'state':-50,'had_passed':True},'videos':[{'cid':2}]}

    def test_unknown_comment_outcome_is_not_retried(self):
        self.prepare_comment()
        with patch.object(replay,'bili_cookies',return_value={}), patch.object(replay,'archive_view',return_value=self.view), \
             patch.object(replay,'bili_post',side_effect=TimeoutError('lost reply')) as post:
            with self.assertRaises(TimeoutError):replay.post_comment(self.session)
            self.assertEqual(replay.Session(self.session.dir).state['comment']['status'],'needs_review')
            with self.assertRaises(RuntimeError):replay.post_comment(self.session)
            self.assertEqual(post.call_count,1)

    def test_posted_comment_not_duplicated(self):
        self.prepare_comment()
        with patch.object(replay,'bili_cookies',return_value={}), patch.object(replay,'archive_view',return_value=self.view), \
             patch.object(replay,'bili_post',side_effect=[{'rpid':3},{}]) as post,patch.object(replay.time,'sleep'):
            replay.post_comment(self.session);replay.post_comment(self.session)
            self.assertEqual(post.call_count,2)
            self.assertTrue(replay.Session(self.session.dir).state['comment']['pinned'])

    def test_subtitle_blocks_changed_parts_before_upload(self):
        self.prepare_comment()
        changed={'archive':{'state':0},'videos':[{'cid':99}]}
        with patch.object(replay,'bili_cookies',return_value={}),patch.object(replay,'archive_view',return_value=changed):
            with self.assertRaisesRegex(RuntimeError,'分 P 已变更'):
                replay.step_subtitle_upload(replay.Session(self.session.dir))

    def test_binding_allows_queued_confirmation_and_checks_part_count(self):
        replay.axis_done(self.session.dir)
        replay.comment_request(self.session.dir,review_token(self.session.dir))
        view={'archive':{'aid':1,'title':'Synthetic'},'videos':[{'cid':2}]}
        with patch.object(replay,'bili_cookies',return_value={}),patch.object(replay,'archive_view',return_value=view):
            replay.bind_archive(self.session.dir,'BV0000000000')
        self.assertEqual(replay.Session(self.session.dir).state['find_archive']['cids'],[2])
        view['videos'].append({'cid':3})
        with patch.object(replay,'bili_cookies',return_value={}),patch.object(replay,'archive_view',return_value=view):
            with self.assertRaisesRegex(RuntimeError,'对不上'):
                replay.step_find_archive(replay.Session(self.session.dir))

    def test_private_post_approval_requires_had_passed(self):
        self.assertFalse(replay.archive_approved({'state':-50}))
        self.assertTrue(replay.archive_approved({'state':-50,'had_passed':True}))
        self.assertFalse(replay.archive_approved({'state':-2,'had_passed':True}))

    def test_multipart_comment_uses_part_prefix(self):
        self.session.update(lambda s:s.update(files=['one','two']))
        (self.session.dir/'axis/歌曲轴.txt').write_text('P1\n00:00:01 One\nP2\n00:00:02 Two\n')
        text=replay.comment_text(replay.Session(self.session.dir))
        self.assertIn('2#00:00:02 Two',text)


class ReviewTests(unittest.TestCase):
    def test_review_requires_exact_current_code(self):
        self.assertIsNone(worker.review_command('确认','abcd'))
        self.assertIsNone(worker.review_command('确认 old','abcd'))
        self.assertEqual(worker.review_command('确认 abcd','abcd'),('confirm',''))
        self.assertEqual(worker.review_command('修改 abcd 第2条删掉','abcd'),('revise','第2条删掉'))

    def test_reviewer_allowlist_and_pagination(self):
        def msg(id,person):return {'message_id':id,'sender':{'sender_type':'user','id':person},'msg_type':'text','content':'确认 code'}
        pages=[{'ok':True,'data':{'messages':[msg('1','allowed'),msg('2','stranger')],'has_more':True,'page_token':'next'}},
               {'ok':True,'data':{'messages':[msg('3','allowed')],'has_more':False}}]
        responses=[Mock(returncode=0,stdout=json.dumps(p),stderr='') for p in pages]
        with patch.object(worker,'FEISHU_CHAT','synthetic-chat'),patch.object(worker,'REVIEWERS',{'allowed'}), \
             patch.object(worker.subprocess,'run',side_effect=responses) as run:
            result=worker.feishu_replies(100)
            self.assertEqual([m['message_id'] for m in result],['1','3'])
            self.assertIn('--page-token',run.call_args.args[0])

    def test_no_reviewer_config_no_read(self):
        with patch.object(worker,'REVIEWERS',set()),patch.object(worker.subprocess,'run') as run:
            self.assertEqual(worker.feishu_replies(100),[])
            run.assert_not_called()


class SubtitleTests(unittest.TestCase):
    def test_word_timestamps_split_long_sentence(self):
        cues=replay.split_cues([{'text':'演示','words':[{'text':'前句','begin_time':0,'end_time':1000},
                                                        {'text':'后句','begin_time':9000,'end_time':10000}]}])
        self.assertEqual(len(cues),2)
        self.assertIn('00:00:09,000 --> 00:00:10,000',replay.to_srt(cues))


if __name__=='__main__':unittest.main()

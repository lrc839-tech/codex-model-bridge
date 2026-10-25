import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from runtime import engine


class ReviewHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.stage=self.root/'workspace';self.stage.mkdir()
        (self.stage/'a.py').write_text('answer = 5\n')
        self.task={'task':'Implement the described text behavior.','writable_files':['a.py'],
                   'verification_files':[],'acceptance':['The value is five.'],
                   'desktop_acceptance':['Inspect an actual rendered screenshot.']}

    def review(self,value):
        client=mock.Mock()
        content=value if isinstance(value,str) else json.dumps(value)
        client.chat.return_value={'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':content}}],
                                  'usage':{'prompt_tokens':7,'completion_tokens':3,'total_tokens':10}}
        with mock.patch.object(engine,'ProviderClient',return_value=client), \
             mock.patch.object(engine.config,'load_profile',return_value={}):
            result=engine.independent_review(self.task,self.stage,self.stage,['a.py'],[],'fixture','private-key',self.root/'review',30)
        return result,client

    def test_extra_json_fields_do_not_block_an_otherwise_complete_pass(self):
        result,_=self.review({'verdict':'pass','summary':'Checked.','findings':[],'unverified':[],
                              'extra_note':'not an acceptance claim'})
        self.assertEqual('pass',result['review']['verdict'])
        self.assertIn('ignored_extra_fields',result['protocol']['normalizations'])

    def test_contradictory_pass_never_silently_hides_issues(self):
        result,_=self.review({'verdict':'pass','summary':'Checked.','findings':[],
                              'unverified':['Required behavior not observed.']})
        self.assertEqual('blocked',result['review']['verdict'])
        self.assertEqual(['Required behavior not observed.'],result['review']['unverified'])

    def test_changes_and_missing_evidence_are_retained_as_blocked(self):
        result,_=self.review({'verdict':'changes_requested','summary':'Concern.',
            'findings':[{'file':'a.py','line':1,'problem':'A concrete concern.'}],
            'unverified':['Needs another observation.']})
        self.assertEqual('blocked',result['review']['verdict'])
        self.assertEqual(1,len(result['review']['findings']))

    def test_invalid_json_is_preserved_and_does_not_regenerate_code_or_retry(self):
        result,client=self.review('not-json private-key')
        self.assertEqual('blocked',result['review']['verdict'])
        self.assertEqual(1,client.chat.call_count)
        raw=(self.root/'review/review-unvalidated.txt').read_text()
        self.assertIn('[REDACTED]',raw);self.assertNotIn('private-key',raw)
        self.assertEqual(10,json.loads((self.root/'review/request-metadata.json').read_text())['usage']['total_tokens'])

    def test_review_cannot_invent_a_line_beyond_file(self):
        result,_=self.review({'verdict':'changes_requested','summary':'Concern.',
            'findings':[{'file':'a.py','line':200,'problem':'Out of range.'}],'unverified':[]})
        self.assertEqual('blocked',result['review']['verdict'])

    def test_visual_observation_remains_codex_only(self):
        result,client=self.review({'verdict':'pass','summary':'Text checked.','findings':[],'unverified':[]})
        packet=json.loads(client.chat.call_args.args[0][1]['content'])
        self.assertEqual(['The value is five.'],packet['acceptance'])
        self.assertNotIn('Inspect an actual rendered screenshot.',json.dumps(packet))
        self.assertEqual('pass',result['review']['verdict'])

    def test_empty_blocked_conclusion_gets_specific_protocol_gap_for_desktop(self):
        result,_=self.review({'verdict':'blocked','summary':'Cannot confirm.','findings':[],'unverified':[]})
        self.assertEqual('blocked',result['review']['verdict'])
        self.assertTrue(result['review']['unverified'])
        self.assertIn('缺少具体问题',result['review']['unverified'][0])

    def test_missing_required_fields_never_become_fabricated_pass_arrays(self):
        result,_=self.review({'verdict':'pass','summary':'Looks fine.'})
        self.assertEqual('blocked',result['review']['verdict'])
        self.assertTrue(result['review']['unverified'])
        self.assertIn('缺少必需字段',result['review']['unverified'][0])

    def test_full_visible_raw_review_is_kept_beyond_sixteen_thousand_characters(self):
        value={'verdict':'pass','summary':'Checked.','findings':[],'unverified':[],
               'extra_note':'x'*18000+'VISIBLE_REVIEW_TAIL'}
        result,_=self.review(value)
        self.assertEqual('pass',result['review']['verdict'])
        raw=(self.root/'review/review-unvalidated.txt').read_text()
        self.assertIn('VISIBLE_REVIEW_TAIL',raw)


if __name__=='__main__':unittest.main()

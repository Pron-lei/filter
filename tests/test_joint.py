import copy
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

from momni.prescreen.client import Client, canonical_citation_pages, canonical_surface_quotes, collect_stream, mock_result, validate
from momni.prescreen.joint import decide


class JointTests(unittest.TestCase):
    def setUp(self):
        self.item=dict(item_id='test',ordinal=1,question={'en':'Compare Video 1 and Video 2.'},
                       options={k:k for k in 'ABCD'},media=[{'index':1},{'index':2}])
        self.original=dict(original_capability='R',original_subtask='R1')
        self.result=mock_result('joint',self.item)
        self.result['capability_review'].update(classification_status='unique',
            predicted_capability='R',predicted_subtask='R1', final_operation='检索满足条件的来源',
            rationale='输出来源编号，不建立身份映射',
            rule_citations=[dict(rule_id='T0',page=8,quote='每题只有一个主能力和主子任务')],
            alternative_labels=[dict(subtask='R2',explanation='可能涉及身份对应',excluded=True,exclusion_reason='设问未要求身份对应')])

    def test_pass_requires_labels_and_multisource_and_clean_surface(self):
        validate('joint',self.result,self.item)
        self.assertEqual(decide(self.result,self.original,[])[0],'pass')
        for capability,subtask in [('R','R2'),('C','C5')]:
            original=dict(original_capability=capability,original_subtask=subtask)
            self.assertEqual(decide(self.result,original,[])[1][0]['code'],'capability_mismatch')
        for status in ['ambiguous','insufficient']:
            changed=copy.deepcopy(self.result)
            changed['capability_review']['classification_status']=status
            self.assertEqual(decide(changed,self.original,[])[0],'rework')
        for judgment in ['single']:
            changed=copy.deepcopy(self.result)
            changed['surface_review']['source_requirement']['judgment']=judgment
            self.assertEqual(decide(changed,self.original,[])[0],'rework')
        for status in ['rework','uncertain']:
            changed=copy.deepcopy(self.result)
            changed['surface_review']['stage1_decision']=status
            self.assertEqual(decide(changed,self.original,[])[0],'rework')
        self.assertEqual(decide(self.result,self.original,[{'code':'missing_file','detail':'test'}])[0],'rework')

    def test_original_labels_hidden_from_model_and_cache_separates_them(self):
        parent=Path(__file__).resolve().parents[1]/'outputs/deepseek_test_tmp'
        parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temp:
            args=Namespace(output=Path(temp),mode='live',model='test',base_url='https://api.deepseek.com',
                           max_tokens=1000,thinking='enabled',retries=0)
            requests=[]
            def request(client,system,item):
                requests.append(json.dumps([system,item],ensure_ascii=False))
                return dict(usage={},choices=[dict(finish_reason='stop',message={'content':json.dumps(self.result)})])
            with patch.object(Client,'request',request):
                a=Client(args).run('joint',self.item,{'original_subtask':'HIDDEN_LABEL_ONE'})
                b=Client(args).run('joint',self.item,{'original_subtask':'HIDDEN_LABEL_TWO'})
            self.assertNotEqual(a['cache_key'],b['cache_key'])
            self.assertEqual(requests[0],requests[1])
            self.assertNotIn('HIDDEN_LABEL',requests[0])
            self.assertIn('R1/R2',requests[0])
            self.assertIn('T-C',requests[0])

    def test_invalid_citation_and_unexcluded_competitor_cannot_pass(self):
        changed=copy.deepcopy(self.result)
        changed['capability_review']['rule_citations'][0]['page']=99
        with self.assertRaises(ValueError): validate('joint',changed,self.item)

    def test_stream_requires_completion_and_keeps_usage(self):
        def event(data): return ('data: '+json.dumps(data)+'\n').encode()
        lines=[b': keepalive\n', event({'choices':[{'delta':{'reasoning_content':'thinking'}}]}),
               event({'choices':[{'delta':{'content':'{"ok":'}}]}),
               event({'choices':[{'delta':{'content':'true}'},'finish_reason':None}]}),
               event({'choices':[{'delta':{},'finish_reason':'stop'}],
                      'usage':{'prompt_tokens':10,'completion_tokens':5,'prompt_cache_hit_tokens':8}}),b'data: [DONE]\n']
        response=collect_stream(lines)
        self.assertEqual(response['choices'][0]['message']['content'],'{"ok":true}')
        self.assertEqual(response['usage']['prompt_cache_hit_tokens'],8)
        self.assertFalse(collect_stream(lines[:-1])['transport_metadata']['done_marker_received'])
        with self.assertRaises(ValueError): collect_stream(lines[:-2])
        changed=copy.deepcopy(self.result)
        changed['capability_review']['alternative_labels'][0]['excluded']=False
        with self.assertRaises(ValueError): validate('joint',changed,self.item)

    def test_page_metadata_correction_preserves_classification_and_rejects_fake_quotes(self):
        changed=copy.deepcopy(self.result)
        changed['capability_review']['rule_citations'][0]['page']=99
        corrections=canonical_citation_pages('joint',changed)
        self.assertEqual(corrections,[dict(rule_id='T0',field='page',original=99,canonical=8)])
        self.assertEqual(changed['capability_review']['predicted_subtask'],'R1')
        validate('joint',changed,self.item)
        changed['capability_review']['rule_citations'][0].update(page=99,quote='fabricated')
        self.assertEqual(canonical_citation_pages('joint',changed),[])
        with self.assertRaises(ValueError):validate('joint',changed,self.item)

    def test_direct_route_disables_only_request_proxy_and_separates_cache(self):
        parent=Path(__file__).resolve().parents[1]/'outputs/deepseek_test_tmp'
        parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temp:
            args=Namespace(output=Path(temp),mode='live',model='test',base_url='https://api.deepseek.com',
                           max_tokens=1000,thinking='enabled',retries=0,timeout=5,proxy_mode='direct')
            fake=MagicMock()
            fake.__enter__.return_value=iter([
                b'data: {"choices":[{"delta":{"content":"{}"},"finish_reason":"stop"}],"usage":{}}\n',
                b'data: [DONE]\n'])
            opener=MagicMock();opener.open.return_value=fake
            with patch.dict('os.environ',{'DEEPSEEK_API_KEY':'unit-test-key'}), \
                 patch('urllib.request.ProxyHandler') as handler, \
                 patch('urllib.request.build_opener',return_value=opener), \
                 patch('urllib.request.urlopen') as system_open:
                Client(args).request('test',self.item)
                handler.assert_called_once_with({})
                system_open.assert_not_called()
            response=dict(usage={},choices=[dict(finish_reason='stop',message={'content':json.dumps(self.result)})])
            with patch.object(Client,'request',return_value=response):
                direct=Client(args).run('joint',self.item,self.original)
                args.proxy_mode='system'
                system=Client(args).run('joint',self.item,self.original)
            self.assertNotEqual(direct['cache_key'],system['cache_key'])

    def test_quote_whitespace_recovery_does_not_change_words(self):
        changed=copy.deepcopy(self.result)
        note=dict(code='test',quote='Video 1  and Video 2',detail='test',certainty='unknown')
        changed['surface_review']['nonblocking_notes']=[note]
        with self.assertRaises(ValueError):validate('joint',changed,self.item)
        corrections=canonical_surface_quotes('joint',changed,self.item)
        self.assertEqual(len(corrections),1)
        self.assertEqual(note['quote'],'Video 1 and Video 2')
        validate('joint',changed,self.item)
        note['quote']='Video 1 and Video 9'
        self.assertEqual(canonical_surface_quotes('joint',changed,self.item),[])
        with self.assertRaises(ValueError):validate('joint',changed,self.item)
        note['quote']='CompareVideo1andVideo2'
        self.assertEqual(canonical_surface_quotes('joint',changed,self.item),[])


if __name__=='__main__': unittest.main()

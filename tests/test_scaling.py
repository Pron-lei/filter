import copy,json,tempfile,unittest,contextlib,io
import urllib.error
from pathlib import Path
from unittest.mock import patch
from momni.prescreen.scaling import load_candidates,next_action
from momni.prescreen.joint import main
from momni.prescreen.client import Client,mock_result


def sample(iid='x'):
    return dict(item_id=iid,question='Compare Video 1 and Video 2. Which follows the stated instruction?',
                options={k:k for k in 'ABCD'},capability='C',subtask='C4',
                media=[dict(asset='a',duration_s=10,tracks=['video','audio']),
                       dict(asset='b',duration_s=12,tracks=['video','audio'])],
                valid_answers=['SECRET_ANSWER'],quality_audit={'comment':'SECRET_COMMENT'})


class ScalingTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[1]/'outputs/scaling_test_tmp';parent.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=parent);self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.input=self.root/'input.jsonl'
    def write(self,rows):self.input.write_text('\n'.join(json.dumps(r) if not isinstance(r,str) else r for r in rows),encoding='utf8')
    def test_allowlist_and_unknown_media_are_supported(self):
        self.write([sample()]);_,items,labels,raw,errors=load_candidates(self.input)
        self.assertFalse(errors);self.assertNotIn('SECRET',json.dumps(items))
        self.assertNotIn('capability',json.dumps(items));self.assertEqual(labels['x']['original_subtask'],'C4')
        self.assertIsNone(items[0]['media'][0]['file_available'])
        self.assertEqual(raw['x']['valid_answers'],['SECRET_ANSWER'])
    def test_bad_rows_and_duplicate_ids_are_isolated(self):
        self.write([sample('ok'),'{broken',sample('dup'),sample('dup'),dict(item_id='bad')])
        _,items,_,_,errors=load_candidates(self.input)
        self.assertEqual([i['item_id'] for i in items],['ok'])
        self.assertEqual(len(errors),4)
    def test_resume_exports_and_does_not_send_gold(self):
        self.write([sample()]);out=self.root/'out';calls=[]
        def request(client,system,item):
            calls.append((system,item));r=mock_result('joint',item)
            r['capability_review'].update(classification_status='unique',predicted_capability='C',predicted_subtask='C4',
                final_operation='核验指令约束',rationale='核验候选满足给定要求',
                rule_citations=[dict(rule_id='T-C',page=8,quote='C4 指令约束核验')],
                alternative_labels=[dict(subtask='J1',explanation='可能比较遵循程度',excluded=True,exclusion_reason='没有优先级比较')])
            r['source_audit']=dict(task_obligations=[],sufficiency_basis='unknown',rationale='test',shortcut_issue_codes=[])
            return dict(usage={'prompt_tokens':10},choices=[dict(finish_reason='stop',message={'content':json.dumps(r)})])
        args=['--input',str(self.input),'--output',str(out),'--mode','live','--model','test','--base-url','https://example.invalid/v1']
        with patch.dict('os.environ',{'SCREENING_API_KEY':'test-only'}),patch.object(Client,'request',request),contextlib.redirect_stdout(io.StringIO()):
            main(args);main(args)
        self.assertEqual(len(calls),1)
        self.assertNotIn('SECRET',json.dumps(calls))
        stats=json.loads((out/'statistics.json').read_text(encoding="utf8"))
        self.assertEqual(stats['counts']['pass'],1);self.assertEqual(stats['api_attempts'],0)
        self.assertEqual(json.loads((out/'pass_candidates.jsonl').read_text(encoding="utf8"))['valid_answers'],['SECRET_ANSWER'])
    def test_explicit_single_skips_llm_and_is_discarded(self):
        r=sample();r['question']='只考察视频1。判断哪个选项符合画面。';self.write([r])
        out=self.root/'single'
        with patch.object(Client,'run',side_effect=AssertionError('must not call model')),contextlib.redirect_stdout(io.StringIO()):
            main(['--input',str(self.input),'--output',str(out),'--mode','live'])
        row=json.loads((out/'results.json').read_text(encoding="utf8"))['rows'][0]
        self.assertEqual(row['final_decision'],'rework')
        self.assertEqual(row['next_action'],'discard_and_generate_replacement')
    def test_one_invalid_record_does_not_block_valid_record(self):
        self.write(['{bad',sample()]);out=self.root/'mixed'
        with contextlib.redirect_stdout(io.StringIO()):main(['--input',str(self.input),'--output',str(out),'--mode','mock'])
        d=json.loads((out/'statistics.json').read_text(encoding="utf8"))
        self.assertEqual(d['n0'],2);self.assertEqual(d['counts']['processing_error'],1)

    def test_reordering_and_gold_changes_reuse_model_cache(self):
        rows=[sample('a'),sample('b')]
        for row in rows:row['question']+=' Case '+row['item_id']+'.'
        self.write(rows);out=self.root/'reorder'
        args=['--input',str(self.input),'--output',str(out),'--mode','mock']
        with contextlib.redirect_stdout(io.StringIO()):main(args)
        rows.reverse();rows[0]['valid_answers']=['NEW_GOLD'];self.write(rows)
        with patch.object(Client,'request',side_effect=AssertionError('must reuse')),contextlib.redirect_stdout(io.StringIO()):main(args)
        s=json.loads((out/'statistics.json').read_text(encoding='utf8'))
        self.assertEqual(s['cache_hits'],2)
        r=json.loads((out/'results.json').read_text(encoding='utf8'))['rows']
        self.assertEqual([(x['ordinal'],x['item_id']) for x in r],[(1,'b'),(2,'a')])

    def test_invalid_key_stops_subsequent_network_calls(self):
        from argparse import Namespace
        args=Namespace(output=self.root/'auth',mode='live',model='test',base_url='https://api.deepseek.com',max_tokens=100,thinking='enabled',retries=2)
        _,items,labels,_,_=self.loaded_sample()
        client=Client(args)
        with patch.object(Client,'request',side_effect=urllib.error.HTTPError('https://api.deepseek.com',401,'unauthorized',{},None)) as req:
            a=client.run('joint',items[0],labels['x']);b=client.run('joint',items[0],labels['x'])
        self.assertEqual(req.call_count,1);self.assertTrue(a['processing_error']);self.assertTrue(b['processing_error'])
    def loaded_sample(self):
        self.write([sample()]);return load_candidates(self.input)


if __name__=='__main__':unittest.main()

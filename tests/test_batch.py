import contextlib,io,json,tempfile,unittest
from pathlib import Path
from momni.prescreen.batch import main


class BatchTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[1]/'outputs/batch_tests';parent.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=parent);self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.input=self.root/'input.jsonl'
    def record(self,n):
        return dict(item_id=f'q{n}',question=f'Compare Video 1 and Video 2, case {n}.',options={k:k for k in 'ABCD'},
            capability='R',subtask='R1',media=[dict(asset='a'),dict(asset='b')])
    def run_args(self,rows):
        self.input.write_text('\n'.join(json.dumps(r) for r in rows),encoding='utf8')
        return ['--input',str(self.input),'--output',str(self.root/'out'),'--batch-size','3','--mode','mock']
    def test_split_merge_and_resume(self):
        args=self.run_args([self.record(n) for n in range(8)])
        with contextlib.redirect_stdout(io.StringIO()):main(args);main(args)
        p=self.root/'out';s=json.loads((p/'batch_statistics.json').read_text(encoding='utf8'))
        self.assertEqual(s['processed'],8);self.assertEqual(s['total_batches'],3);self.assertEqual(s['pending'],0)
        self.assertEqual(s['api_attempts_this_invocation'],0)
        rows=[json.loads(x) for x in (p/'all_results.jsonl').read_text(encoding='utf8').splitlines()]
        self.assertEqual([r['source_line'] for r in rows],list(range(1,9)))
        manifest=json.loads((p/'batch_manifest.json').read_text(encoding='utf8'))
        self.assertTrue(all(not p['executed'] for p in manifest['plans']))
    def test_cross_batch_duplicate_ids_stop_before_api(self):
        rows=[self.record(n) for n in range(5)];rows[-1]['item_id']='q0';args=self.run_args(rows)
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):main(args)
    def test_cross_batch_exact_question_dedup(self):
        rows=[self.record(n) for n in range(5)];rows[-1]=dict(rows[0],item_id='another')
        args=self.run_args(rows)
        with contextlib.redirect_stdout(io.StringIO()):main(args)
        p=self.root/'out';mapping=json.loads((p/'_global_duplicates.json').read_text(encoding='utf8'))
        self.assertEqual(mapping,{'another':'q0'})
        rows=[json.loads(x) for x in (p/'all_results.jsonl').read_text(encoding='utf8').splitlines()]
        duplicate=next(r for r in rows if r['item_id']=='another')
        self.assertEqual(duplicate['execution_backend'],'local_rules')


if __name__=='__main__':unittest.main()

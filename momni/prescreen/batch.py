"""Split, resume and aggregate large JSONL jobs without loading all model replies."""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from ..io import read_json,write_json
from .client import ASSETS
from .inputs import digest
from .joint import main as run_batch
from .scaling import load_candidates


def aggregate(output,plans):
    counts=Counter();usage=Counter();processed=0;attempts=0;completed=0
    names=['all_results.jsonl','pass_candidates.jsonl','rework_candidates.jsonl',
           'processing_error_candidates.jsonl','processing_error_results.jsonl']
    handles={n:(output/n).open('w',encoding='utf8') for n in names}
    try:
        for plan in plans:
            folder=Path(plan['output']);stats=read_json(folder/'statistics.json')
            if not plan.get('complete') or not stats:continue
            completed+=1;counts.update(stats['counts']);processed+=stats['n0']
            if plan.get('executed'):
                attempts+=stats['api_attempts']
                usage.update({k:v for k,v in stats['usage'].items() if isinstance(v,(int,float))})
            for name in names:
                source=folder/name
                if not source.exists():continue
                with source.open(encoding='utf8') as stream:
                    for line in stream:
                        if name.endswith('results.jsonl'):
                            row=json.loads(line);row['batch_id']=plan['batch_id']
                            local=row.get('ordinal')
                            row['source_line']=plan['source_lines'][local-1] if isinstance(local,int) and 0<local<=len(plan['source_lines']) else None
                            line=json.dumps(row,ensure_ascii=False)+'\n'
                        handles[name].write(line)
    finally:
        for f in handles.values():f.close()
    summary=dict(total_input=sum(len(p['source_lines']) for p in plans),processed=processed,
        pending=sum(len(p['source_lines']) for p in plans)-processed,completed_batches=completed,total_batches=len(plans),
        counts={s:counts[s] for s in ['pass','rework','processing_error']},
        api_attempts_this_invocation=attempts,usage_this_invocation=dict(usage),actual_api_cost=None)
    write_json(output/'batch_statistics.json',summary)
    (output/'batch_report.md').write_text('# 批量初筛汇总\n\n'+json.dumps(summary,ensure_ascii=False,indent=2)+
        '\n\npass_candidates.jsonl为可接后续审核的原始题单。processing_error_results.jsonl是错误清单，不计为质量不合格。每批完整响应保留于batches/。修正错误后重跑同一命令，成功缓存可复用。\n',encoding='utf8')
    return summary


def main(argv=None):
    p=argparse.ArgumentParser(description='万题级分批协调器；其它参数原样传给单批初筛')
    p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--batch-size',type=int,default=500)
    p.add_argument('--media-root',type=Path)
    p.add_argument('--mode',choices=['live','mock','dry-run'],default='dry-run')
    a,rest=p.parse_known_args(argv)
    if a.batch_size<=0:p.error('batch-size must be positive')
    if any(x in ['--bundle','--item-id','--limit','--duplicate-map','--cache-root'] or x.startswith(('--bundle=','--item-id=','--limit=','--duplicate-map=','--cache-root=')) for x in rest):
        p.error('批量入口不支持bundle/item-id/limit/duplicate-map/cache-root；使用input和batch-size')
    source=a.input.resolve();out=a.output.resolve();media_root=a.media_root.resolve() if a.media_root else source.parent
    if source.is_relative_to(out):p.error('output must be separate from input')
    out.mkdir(parents=True,exist_ok=True)
    sha=hashlib.sha256();seen=set();duplicate_ids=set();total=0
    with source.open(encoding='utf-8-sig') as stream:
        for line in stream:
            sha.update(line.encode())
            if not line.strip():continue
            total+=1
            try:
                row=json.loads(line);iid=row.get('item_id') if isinstance(row,dict) else None
                if isinstance(iid,str):
                    if iid in seen:duplicate_ids.add(iid)
                    seen.add(iid)
            except ValueError:pass
    if duplicate_ids:p.error('跨批重复item_id，尚未调用API，请先修复：'+str(sorted(duplicate_ids)[:10]))
    if not total:p.error('empty input')
    assets=digest({f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in ASSETS.iterdir() if f.suffix in ['.py','.txt','.json']})
    extra_files={}
    for i,x in enumerate(rest):
        flag,separator,inline=x.partition('=')
        if flag in ['--extra-body','--reviewer-resources']:
            value=inline if separator else (rest[i+1] if i+1<len(rest) else '')
            if value:extra_files[flag]=hashlib.sha256(Path(value).read_bytes()).hexdigest()
    import os
    profile=digest(dict(input_hash=sha.hexdigest(),batch_size=a.batch_size,args=rest,mode=a.mode,
        assets=assets,extra_files=extra_files,media_root=str(media_root),
        model_env=os.getenv('SCREENING_MODEL'),base_env=os.getenv('SCREENING_BASE_URL')))
    plans=[];chunk=[];numbers=[];n=0
    def save():
        batch_id=f'batch_{len(plans)+1:05d}';path=out/'_inputs'/f'{batch_id}.jsonl'
        path.parent.mkdir(exist_ok=True);path.write_text(''.join(chunk),encoding='utf8')
        folder=out/'batches'/batch_id;marker=read_json(folder/'_batch_complete.json')
        good=bool(marker and marker['profile']==profile and marker['processing_errors']==0 and (folder/'results.json').exists())
        plans.append(dict(batch_id=batch_id,input=str(path),output=str(folder),source_lines=list(numbers),complete=good,executed=False))
    with source.open(encoding='utf-8-sig') as stream:
        for n,line in enumerate(stream,1):
            if not line.strip():continue
            chunk.append(line if line.endswith('\n') else line+'\n');numbers.append(n)
            if len(chunk)>=a.batch_size:save();chunk=[];numbers=[]
    if chunk:save()
    # Cross-batch content de-duplication uses the same sanitized metadata as the runner.
    fingerprints={};duplicates={}
    resources=None
    for i,x in enumerate(rest):
        flag,separator,inline=x.partition('=')
        if flag=='--reviewer-resources':
            value=inline if separator else (rest[i+1] if i+1<len(rest) else '')
            if value:resources=read_json(value)
    for plan in plans:
        _,items,_,_,_=load_candidates(Path(plan['input']),resources,media_root)
        for item in items:
            signature=digest({k:item[k] for k in ['question','options','media','reviewer_resources']})
            if signature in fingerprints:duplicates[item['item_id']]=fingerprints[signature]
            else:fingerprints[signature]=item['item_id']
    write_json(out/'_global_duplicates.json',duplicates)
    write_json(out/'batch_manifest.json',dict(profile=profile,input=str(source),input_hash=sha.hexdigest(),plans=plans))
    for plan in plans:
        if plan['complete']:
            print(plan['batch_id']+' reused completed batch',flush=True);continue
        args=['--input',plan['input'],'--output',plan['output'],'--mode',a.mode,'--media-root',str(media_root),
              '--cache-root',str(out/'cache'),'--duplicate-map',str(out/'_global_duplicates.json')]+rest
        if not any(x=='--near-pair-budget' or x.startswith('--near-pair-budget=') for x in rest):args+=['--near-pair-budget','0']
        print(plan['batch_id']+' starting',flush=True)
        run_batch(args);plan['executed']=True
        if a.mode!='dry-run':
            stats=read_json(Path(plan['output'])/'statistics.json');plan['complete']=True
            write_json(Path(plan['output'])/'_batch_complete.json',dict(profile=profile,processing_errors=stats['counts']['processing_error']))
            write_json(out/'batch_manifest.json',dict(profile=profile,input=str(source),plans=plans))
            write_json(out/'batch_progress.json',dict(completed_batches=sum(p['complete'] for p in plans),total_batches=len(plans),
                completed_records=sum(len(p['source_lines']) for p in plans if p['complete']),total_records=total))
            # Auth/balance/model configuration failures affect all remaining batches.
            with (Path(plan['output'])/'all_results.jsonl').open(encoding='utf8') as stream:
                for line in stream:
                    error=json.loads(line).get('processing_error') or ''
                    if any(code in error for code in ['HTTP 401','HTTP 402','HTTP 403','HTTP 404']):
                        aggregate(out,plans)
                        p.error('接口配置/鉴权失败，剩余批次未调用；已保存进度，修复后续跑')
    if a.mode=='dry-run':
        write_json(out/'batch_preflight.json',dict(total_input=total,total_batches=len(plans),global_exact_duplicates=len(duplicates),model_calls=0))
    else:print(aggregate(out,plans),flush=True)


if __name__=='__main__':main()

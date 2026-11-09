"""Generic JSONL adapter and bounded batch/output helpers for handoff."""
from __future__ import annotations

import json
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from ..io import read_json, write_json, write_jsonl
from .inputs import digest


SUBTASKS = {f'{c}{i}' for c,n in [('R',3),('C',5),('J',4),('O',6),('G',3)] for i in range(1,n+1)}


def content(value):
    if isinstance(value,str) and value.strip(): return value
    if isinstance(value,dict) and value and all(k in ('zh','en') and isinstance(v,str) for k,v in value.items()) and any(v.strip() for v in value.values()):
        return value
    raise ValueError('question/options text must be nonempty string or zh/en object')


def load_candidates(path, resources=None, media_root=None):
    path=Path(path).resolve()
    media_root=Path(media_root).resolve() if media_root else path.parent
    items, originals, raw_records, errors, lines_by_id = [], {}, {}, [], {}
    def error(line, iid, message):
        return dict(item_id=iid,ordinal=line,record_key=f'line_{line}',stage='input',
                    mode=None,processing_error=message,error_kind='input_error',result=None,
                    final_decision=None,rework_reasons=[],cache_hit=False,attempts=[],
                    invocation_attempts=[],elapsed_s=0,input_line=line,next_action='repair_input')
    with path.open(encoding='utf-8-sig') as stream:
        for number,line in enumerate(stream,1):
            if not line.strip():continue
            iid=f'_invalid_line_{number}'
            try:
                raw=json.loads(line)
                if not isinstance(raw,dict):raise ValueError('record must be an object')
                iid=raw.get('item_id')
                if not isinstance(iid,str) or not iid.strip():
                    iid=f'_invalid_line_{number}';raise ValueError('item_id must be a nonempty string')
                lines_by_id.setdefault(iid,[]).append(number)
                question=content(raw.get('question'))
                options=raw.get('options')
                if not isinstance(options,dict) or set(options)!={'A','B','C','D'}:
                    raise ValueError('options must contain exactly A/B/C/D')
                options={k:content(v) for k,v in options.items()}
                cap=raw.get('original_capability',raw.get('capability'))
                sub=raw.get('original_subtask',raw.get('subtask'))
                if sub not in SUBTASKS or cap!=sub[0]:raise ValueError('invalid original capability/subtask')
                if ('capability' in raw and 'original_capability' in raw and raw['capability']!=cap) or ('subtask' in raw and 'original_subtask' in raw and raw['subtask']!=sub):
                    raise ValueError('conflicting original labels')
                source_media=raw.get('media')
                if not isinstance(source_media,list) or not source_media:raise ValueError('media must be a nonempty ordered list')
                media=[]
                for index,m in enumerate(source_media,1):
                    if not isinstance(m,dict):raise ValueError('each media entry must be an object')
                    if m.get('index',index)!=index:raise ValueError('media index must match ordered list position (1-based)')
                    asset=m.get('asset',m.get('media_id'))
                    if not isinstance(asset,str) or not asset.strip():raise ValueError('media asset/media_id required')
                    duration=m.get('duration_s')
                    if duration is not None and (isinstance(duration,bool) or not isinstance(duration,(int,float)) or not 0<duration<float('inf')):
                        raise ValueError('duration_s must be positive finite number or null')
                    available=m.get('file_available')
                    if available is not None and type(available) is not bool:raise ValueError('file_available must be true/false/null')
                    availability_source='provided_metadata' if available is not None else 'unknown'
                    if m.get('path'):
                        local=Path(m['path']);local=local if local.is_absolute() else media_root/local
                        available=local.is_file();availability_source='local_file_check'
                    tracks=m.get('tracks')
                    if tracks is not None and (not isinstance(tracks,list) or any(t not in ('video','audio','subtitle') for t in tracks)):
                        raise ValueError('tracks must list video/audio/subtitle or be null')
                    media.append(dict(index=index,asset=asset,duration_s=duration,tracks=tracks,
                        file_available=available,availability_source=availability_source,
                        track_status='provided_metadata' if tracks is not None else 'unknown',
                        parent_asset=m.get('parent_asset'),parent_duration_s=m.get('parent_duration_s'),
                        relationship_source=m.get('relationship_source','provided_metadata') if m.get('parent_asset') else None,
                        relationship_status='metadata_declared_not_content_verified' if m.get('parent_asset') else None))
                rr=resources if resources is not None else raw.get('reviewer_resources',{})
                if not isinstance(rr,dict):raise ValueError('reviewer_resources must be an object')
                # Deliberate allowlist: no answer, human comment, quality_audit,
                # distractor explanations or original labels enter model input.
                item=dict(item_id=iid,ordinal=number,question=question,options=options,media=media,
                          reviewer_resources={k:rr.get(k,'unknown') for k in ('tools','measurements')})
                items.append(item)
                originals[iid]=dict(original_capability=cap,original_subtask=sub)
                raw_records[iid]=raw
            except (ValueError,TypeError,KeyError,OverflowError) as exc:
                errors.append(error(number,iid,str(exc)))
    duplicates={iid for iid,lines in lines_by_id.items() if len(lines)>1}
    for item in items:
        if item['item_id'] in duplicates:
            errors.append(error(item['ordinal'],item['item_id'],'duplicate item_id; all occurrences excluded from API calls'))
    items=[i for i in items if i['item_id'] not in duplicates]
    for iid in duplicates:originals.pop(iid,None);raw_records.pop(iid,None)
    # An invalid occurrence of a duplicate is already represented once above.
    errors=list({r['input_line']:r for r in errors}.values())
    if not items and not errors:raise ValueError('input contains no records')
    source_hash=__import__('hashlib').sha256(path.read_bytes()).hexdigest()
    pack=dict(pack_id='jsonl_'+source_hash[:16],source_sha256=source_hash,version='candidate-jsonl-v1')
    return pack,items,originals,raw_records,errors


def bounded_results(items,work,workers):
    iterator=iter(items)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending=set()
        def fill():
            while len(pending)<workers*2:
                try:item=next(iterator)
                except StopIteration:break
                pending.add(pool.submit(work,item))
        fill()
        while pending:
            done,pending=wait(pending,return_when=FIRST_COMPLETED)
            for future in done:yield future.result()
            fill()


def next_action(row):
    if row['processing_error']:return 'repair_input' if row.get('error_kind')=='input_error' else 'retry_processing'
    codes={r['code'] for r in row['rework_reasons']}
    if codes & {'single_source_not_allowed','explicit_single_video_task'}:return 'discard_and_generate_replacement'
    if 'exact_duplicate' in codes:return 'deduplicate_keep_first'
    if row['final_decision']=='pass':return 'continue_to_media_and_human_review'
    if codes <= {'capability_mismatch'}:return 'correct_capability_label'
    if codes & {'classification_ambiguous','classification_insufficient'}:return 'resolve_capability_definition'
    return 'review_stated_issues'


def export_candidates(output,rows,raw_records):
    for state in ('pass','rework','processing_error'):
        chosen=[r for r in rows if ('processing_error' if r['processing_error'] else r['final_decision'])==state]
        write_jsonl(output/f'{state}_results.jsonl',chosen)
        # Full original rows go downstream, never into LLM messages.
        write_jsonl(output/f'{state}_candidates.jsonl',[
            raw_records[r['item_id']] for r in chosen if r['item_id'] in raw_records])
    write_json(output/'actions.json', {action:[dict(item_id=r['item_id'],ordinal=r['ordinal']) for r in rows if next_action(r)==action]
        for action in sorted({next_action(r) for r in rows})})


def input_assignment(row,item):
    if row['processing_error'] or next_action(row)=='discard_and_generate_replacement':
        return dict(status='not_applicable',unassigned_inputs=[])
    surface=(row.get('result') or {}).get('surface_review',{})
    known={m['index'] for m in item['media']}
    remaining=known-set(surface.get('core_inputs',[]))-set(surface.get('redundant_inputs',[]))
    return dict(status='needs_check' if remaining else 'complete',unassigned_inputs=sorted(remaining),
                note='未分配不等于已确认冗余，不自动删除媒体')

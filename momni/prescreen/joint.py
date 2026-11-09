"""Independent capability prediction plus text triage, one API call per item."""
from __future__ import annotations

import argparse
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..io import read_json, write_json, write_jsonl
from .client import Client, RULES
from .inputs import digest, duplicate_groups, issue, load_bundle, local_checks
from .scaling import bounded_results, export_candidates, input_assignment, load_candidates, next_action


POLICY = 'capability_match_and_multisource_and_no_errors_v1'


def decide(result, original, local_issues):
    capability, surface = result['capability_review'], result['surface_review']
    reasons = []
    status = capability['classification_status']
    if status != 'unique':
        reasons.append(dict(code='classification_' + status, detail=capability['rationale']))
    elif (capability['predicted_capability'], capability['predicted_subtask']) != (
            original['original_capability'], original['original_subtask']):
        reasons.append(dict(code='capability_mismatch', detail=(
            f"原标签{original['original_capability']}/{original['original_subtask']}，"
            f"独立预测{capability['predicted_capability']}/{capability['predicted_subtask']}")))
    if surface['source_requirement']['judgment'] == 'single':
        reasons.append(dict(code='single_source_not_allowed', detail=surface['source_requirement']['derivation']))
    if surface['stage1_decision'] != 'pass':
        reasons.append(dict(code='surface_' + surface['stage1_decision'], detail=surface['issues']))
    reasons.extend(dict(code=r['code'], detail=r['detail']) for r in local_issues)
    return ('rework' if reasons else 'pass'), reasons


def summarize(output, meta, rows, elapsed, invocation_rows=None):
    counts = {s: sum((r['final_decision'] if not r['processing_error'] else 'processing_error') == s
                     for r in rows) for s in ('pass', 'rework', 'processing_error')}
    queues = {s: [dict(ordinal=r['ordinal'], item_id=r['item_id']) for r in rows if
                  (r['final_decision'] if not r['processing_error'] else 'processing_error') == s] for s in counts}
    usage, retries = Counter(), Counter()
    invocation_rows = rows if invocation_rows is None else invocation_rows
    attempts = [a for r in invocation_rows for a in r.get('invocation_attempts', [])]
    for a in attempts:
        for k, v in a['usage'].items():
            if isinstance(v, (int, float)):
                usage[k] += v
                if a['attempt'] > 1:
                    retries[k] += v
    reasons = Counter(reason['code'] for r in rows for reason in r.get('rework_reasons', []))
    cross = Counter(f"{r['original_subtask']} -> {r['result']['capability_review']['predicted_subtask']}"
                    for r in rows if r['result'])
    summary = dict(mode=meta['mode'], n0=len(rows), counts=counts,
                   successfully_processed=len(rows)-counts['processing_error'],
                   pass_rate_among_successfully_processed=(counts['pass']/(len(rows)-counts['processing_error'])
                        if len(rows)>counts['processing_error'] and meta['mode']=='live' else None),
                   pass_rate=counts['pass']/len(rows) if rows and meta['mode'] == 'live' else None,
                   reason_counts=dict(reasons), original_to_predicted=dict(cross),
                   elapsed_s=elapsed, api_attempts=len(attempts) if meta['mode']=='live' else 0,
                   mock_attempts=len(attempts) if meta['mode']=='mock' else 0,
                   cache_hits=sum(r['cache_hit'] for r in invocation_rows),
                   reused_successful_results=len(rows)-len(invocation_rows),
                   usage=dict(usage), retry_attempts=sum(a['attempt'] > 1 for a in attempts),
                   retry_usage=dict(retries), actual_api_cost=None,
                   unknown_usage_attempts=sum(bool(a['error']) and not a['usage'] for a in attempts))
    write_json(output / 'statistics.json', summary)
    write_json(output / 'queues.json', queues)
    write_json(output / 'pass.json', queues['pass'])
    write_json(output / 'rework.json', [r for r in rows if r['final_decision'] == 'rework'])
    write_jsonl(output / 'all_results.jsonl', rows)
    lines = ['# 能力与题面联合初筛', '',
             f"模式：{meta['mode']}；{len(rows)}题 → pass {counts['pass']}，rework {counts['rework']}，执行故障 {counts['processing_error']}。", '',
             '每题一次调用：先按五类能力/21子任务独立分类，再检查题面；原标签不进入模型输入，调用结束后由代码比较主能力和主子任务。',
             '能力不匹配、分类歧义/信息不足、single、题面rework/uncertain或本地阻断问题均为rework；只有全部条件满足才pass。执行故障单列。',
             '参考答案/人工结果不进入模型；本次不核验媒体事实和选择题答案。', '',
             '## 结果队列', '']
    for state, group in queues.items():
        lines += [f"- {state}：" + '、'.join(f"{r['ordinal']} ({r['item_id']})" for r in group)]
    lines += ['', '## 非通过题及原因', '']
    for r in rows:
        if r['final_decision'] == 'pass':
            continue
        lines += [f"### 第{r['ordinal']}题 `{r['item_id']}`", '']
        if r['processing_error']:
            lines += [r['processing_error'], '']
        elif r['result'] is None:
            lines += ['本地确定性检查直接处理，未调用模型分类。','']
            lines += [f"- {reason['code']}：{reason['detail']}" for reason in r['rework_reasons']]
            lines += ['']
        else:
            c = r['result']['capability_review']
            lines += [f"原标签 {r['original_subtask']} → 预测 {c['predicted_subtask']}（{c['classification_status']}）。", '',
                      c['rationale'], '']
            lines += [f"- {reason['code']}：{reason['detail']}" for reason in r['rework_reasons']]
            lines += ['']
    lines += ['## 用量', '', str(summary), '',
              '这是文本及元数据初筛；pass表示基本符合本轮条件，可以继续媒体/人工审核，不代表最终入库认证。实际账单金额未知时为空。', '']
    if meta['mode']!='live':
        lines += ['**当前为离线模式，以上结果只验证工程流程，不是模型审核结论。**','']
    (output/'report.md').write_text('\n'.join(lines), encoding='utf8')


def compare_human(bundle, output, pack, rows):
    path = bundle/'review_results/author_audit/R01_3439162580_reviews_with_comments.json'
    if not path.exists():
        return
    human = read_json(path)
    if human['pack_id'] != pack['pack_id'] or human['source_sha256'] != pack.get('source_sha256'):
        raise ValueError('human comparison pack/version mismatch')
    detailed, matrix = [], Counter()
    for row in rows:
        review = human['reviews'][row['item_id']]
        choices = review['choices']
        capability = (row['result'] or {}).get('capability_review', {})
        surface = (row['result'] or {}).get('surface_review', {})
        predicted, target = capability.get('predicted_subtask'), choices.get('subtask')
        state = row['final_decision'] or 'processing_error'
        matrix[f"{choices['decision']} -> {state}"] += 1
        detailed.append(dict(ordinal=row['ordinal'], item_id=row['item_id'], final_decision=state,
            original_subtask=row['original_subtask'], predicted_subtask=predicted,
            classification_status=capability.get('classification_status'),
            human_subtask=target, human_label_match=(predicted == target) if target and target != 'uncertain' and predicted else None,
            human_decision=choices['decision'], human_reasons=choices.get('reasons', []),
            human_comment=review.get('comment', ''), model_source_requirement=surface.get('source_requirement'),
            rework_reasons=row['rework_reasons']))
    comparable = [r for r in detailed if r['human_label_match'] is not None]
    summary = dict(matrix=dict(matrix), explicit_human_label_comparable=len(comparable),
        explicit_human_label_matches=sum(r['human_label_match'] for r in comparable),
        unique_prediction_human_label_comparable=sum(r['classification_status']=='unique' for r in comparable),
        unique_prediction_human_label_matches=sum(r['human_label_match'] and r['classification_status']=='unique' for r in comparable),
        human_revise_items=[r for r in detailed if r['human_decision']=='revise'])
    write_json(output/'offline_human_comparison.json', dict(summary=summary, rows=detailed))


def main(argv=None):
    p = argparse.ArgumentParser(description='能力盲预测+原标签比较+单视频/题面错误联合初筛')
    sources=p.add_mutually_exclusive_group()
    sources.add_argument('--bundle', type=Path, default=Path(__file__).resolve().parents[3]/'review_bundle')
    sources.add_argument('--input', type=Path, help='通用JSONL，每行一道题；格式见交接说明')
    p.add_argument('--media-root', type=Path, help='JSONL媒体相对路径根目录，默认输入文件所在目录')
    p.add_argument('--cache-root', type=Path, help='可选共享成功缓存目录')
    p.add_argument('--duplicate-map', type=Path, help='批处理协调器生成的跨批精确重复映射')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--mode', choices=['live','mock','dry-run'], default='dry-run')
    p.add_argument('--limit', type=int)
    p.add_argument('--item-id', action='append')
    p.add_argument('--model', default=os.getenv('SCREENING_MODEL'))
    p.add_argument('--base-url', default=os.getenv('SCREENING_BASE_URL'))
    p.add_argument('--api-format', choices=['chat-completions','anthropic-messages'], default='chat-completions')
    p.add_argument('--api-key-env', default='SCREENING_API_KEY')
    p.add_argument('--stream', action='store_true', help='Chat Completions SSE；默认非流式')
    p.add_argument('--stream-usage', action=argparse.BooleanOptionalAction, default=True,help='服务商不支持stream_options时用--no-stream-usage')
    p.add_argument('--response-format', choices=['none','json-object'], default='none')
    p.add_argument('--max-tokens-field', choices=['max_tokens','max_completion_tokens'], default='max_tokens')
    p.add_argument('--auth-style', choices=['bearer','x-api-key','none'], default='bearer')
    p.add_argument('--api-version', default='2026-10-21', help='原生Messages版本请求头')
    p.add_argument('--extra-body', type=Path, help='可选供应商扩展JSON，不可包含密钥或覆盖核心请求字段')
    p.add_argument('--proxy-mode', choices=['system','direct'], default='system', help='system跟随系统代理；direct仅本进程绕过HTTP代理')
    p.add_argument('--thinking', choices=['enabled','disabled'], default=None)
    p.add_argument('--max-tokens', type=int, default=24576)
    p.add_argument('--timeout', type=float, default=240)
    p.add_argument('--concurrency', type=int, default=2)
    p.add_argument('--retries', type=int, default=2)
    p.add_argument('--reviewer-resources', type=Path)
    p.add_argument('--prompt-version', choices=['v1','v2','v3','v4'], help='JSONL默认v4，历史bundle默认v1')
    p.add_argument('--near-pair-budget', type=int, default=5000, help='近重复候选比较上限；精确去重始终覆盖全量')
    a = p.parse_args(argv)
    if min(a.timeout, a.concurrency, a.max_tokens) <= 0 or a.retries < 0 or (a.limit is not None and a.limit < 1):
        p.error('invalid execution limits')
    a.base_url=a.base_url or 'https://not-configured.invalid'
    a.model=a.model or 'not-configured'
    try:a.extra_body=read_json(a.extra_body) if a.extra_body else {}
    except (OSError,ValueError) as exc:p.error('无法读取extra-body: '+str(exc))
    forbidden={'model','messages','system','stream','stream_options','max_tokens','max_completion_tokens','api_key','authorization','x-api-key'}
    if not isinstance(a.extra_body,dict) or forbidden & {str(k).lower() for k in a.extra_body}:
        p.error('extra-body必须为JSON对象，且不能包含核心字段或密钥')
    def has_secret(value):
        if isinstance(value,dict):
            return any(str(k).lower() in {'api_key','api-key','apikey','authorization','x-api-key','access_token'} or has_secret(v) for k,v in value.items())
        return isinstance(value,list) and any(has_secret(v) for v in value)
    if has_secret(a.extra_body):p.error('extra-body任何层级均不得包含认证密钥；请用环境变量')
    if a.api_format=='anthropic-messages' and a.stream:
        p.error('原生Messages目前仅支持非流式')
    if a.api_format=='anthropic-messages' and (a.thinking is not None or a.response_format!='none' or a.max_tokens_field!='max_tokens'):
        p.error('原生Messages请使用对应协议参数；扩展参数通过extra-body配置')
    from urllib.parse import urlsplit
    url = urlsplit(a.base_url)
    local_http=url.scheme=='http' and url.hostname in ('localhost','127.0.0.1','::1') and a.auth_style=='none'
    if (url.scheme!='https' and not local_http) or not url.hostname or url.username or url.password or url.query or url.fragment:
        p.error('base-url需HTTPS且无凭证/查询/fragment；本地回环HTTP须搭配--auth-style none')
    a.output=a.output.resolve()
    if a.input and a.input.resolve().is_relative_to(a.output):
        p.error('output must be separate from the input directory; refusing to overwrite source data')
    existing=read_json(a.output/'manifest.json')
    if existing and existing['mode'] != a.mode:
        p.error('不同模式须使用不同输出目录')
    bundle=a.bundle.resolve()
    resources=read_json(a.reviewer_resources) if a.reviewer_resources else None
    raw_records, input_errors = {}, []
    a.prompt_version=a.prompt_version or ('v4' if a.input else 'v1')
    if a.input:
        try:
            pack,items,originals,raw_records,input_errors=load_candidates(a.input,resources,a.media_root)
        except (OSError,ValueError) as exc:
            p.error(str(exc))
        if a.item_id:
            selected=set(a.item_id)
            known={i['item_id'] for i in items}|{r['item_id'] for r in input_errors}
            if selected-known:p.error(f'unknown item_id: {sorted(selected-known)}')
            items=[i for i in items if i['item_id'] in selected]
            input_errors=[r for r in input_errors if r['item_id'] in selected]
        if a.limit:
            ordinals=sorted([i['ordinal'] for i in items]+[r['ordinal'] for r in input_errors])[:a.limit]
            items=[i for i in items if i['ordinal'] in ordinals]
            input_errors=[r for r in input_errors if r['ordinal'] in ordinals]
    else:
        pack, items, originals=load_bundle(bundle,resources,a.item_id)
        if a.limit:items=items[:a.limit]
    needs_api=any(not (a.input and local_checks(i)[0]) for i in items)
    if a.mode=='live' and needs_api:
        if a.model=='not-configured' or a.base_url=='https://not-configured.invalid':
            p.error('live需要--model、--base-url（也可设置SCREENING_MODEL/SCREENING_BASE_URL）')
        if a.auth_style!='none' and not os.getenv(a.api_key_env):p.error('需要设置环境变量 '+a.api_key_env)
    if a.near_pair_budget<0:p.error('near-pair-budget must be nonnegative')
    exact, near=duplicate_groups(items,a.near_pair_budget)
    if a.duplicate_map:
        global_exact=read_json(a.duplicate_map)
        exact.update({i['item_id']:global_exact[i['item_id']] for i in items if i['item_id'] in global_exact})
    meta=dict(mode=a.mode, model=a.model, pack_id=pack['pack_id'], source_sha256=pack.get('source_sha256'),
              base_url=a.base_url,api_key_env=a.api_key_env,auth_style=a.auth_style,
              api_version=a.api_version,extra_body=a.extra_body,
              proxy_mode=a.proxy_mode, api_format=a.api_format, stream=a.stream,
              response_format=a.response_format, max_tokens_field=a.max_tokens_field,
              input_hash=digest(items), n0=len(items)+len(input_errors), policy=POLICY, prompt_version='joint_'+a.prompt_version,
              input_format='candidate-jsonl-v1' if a.input else 'review-bundle',
              input_path=str(a.input.resolve()) if a.input else None,
              near_duplicate_scope=dict(total_possible_pairs=len(items)*(len(items)-1)//2,
                  compared_pairs=min(a.near_pair_budget,len(items)*(len(items)-1)//2),advisory_only=True),
              pdf_excerpt_hash=digest(RULES), original_labels=originals)
    write_json(a.output/'manifest.json',meta)
    write_json(a.output/'sanitized_inputs.json',items)
    write_json(a.output/'near_duplicate_candidates.json',near)
    for row in input_errors:row['mode']=a.mode
    write_json(a.output/'input_errors.json',input_errors)
    from .inputs import explicit_single_video
    if a.mode=='dry-run':
        write_json(a.output/'preflight.json',dict(total=meta['n0'],valid_input=len(items),
            input_errors=len(input_errors),explicit_single_video=[i['item_id'] for i in items if explicit_single_video(i)],
            local_checks=[dict(item_id=i['item_id'],issues=local_checks(i)[0],notes=local_checks(i)[1]) for i in items],
            model_calls=0,quality_decisions='not_run'))
        return
    client, collected, start=Client(a), {}, time.monotonic()
    for row in input_errors:collected[('input_error',row['record_key'])]=row

    def work(item):
        problems, notes=local_checks(item)
        if item['item_id'] in exact:
            problems.append(issue('exact_duplicate','','与'+exact[item['item_id']]+'完全重复'))
        original=originals[item['item_id']]
        if a.input and problems:
            row=dict(item_id=item['item_id'],ordinal=item['ordinal'],stage='joint',mode=a.mode,
                     input_hash=digest(item),result=None,processing_error=None,cache_hit=False,
                     attempts=[],invocation_attempts=[],elapsed_s=0,execution_backend='local_rules',
                     final_decision='rework',rework_reasons=[dict(code=r['code'],detail=r['detail']) for r in problems])
            row.update(original,local_issues=problems,local_notes=notes,decision_policy_version=POLICY)
            row['next_action']=next_action(row)
            row['input_assignment']=input_assignment(row,item)
            return row
        row=client.run('joint',item,original,context=None)
        row.update(original, local_issues=problems, local_notes=notes, decision_policy_version=POLICY)
        row['final_decision'],row['rework_reasons']=(decide(row['result'],original,problems)
            if row['result'] is not None and not row['processing_error'] else (None,[]))
        row['next_action']=next_action(row)
        row['input_assignment']=input_assignment(row,item)
        return row

    for row in bounded_results(items,work,a.concurrency):
        collected[('item',row['item_id'])]=row
        # O(1) checkpoint writes per completion, bounded outstanding futures.
        write_json(a.output/'items'/f"{digest(row['item_id'])}.json",row)
        write_jsonl(a.output/'progress.jsonl',[dict(item_id=row['item_id'],ordinal=row['ordinal'],
                    final_decision=row['final_decision'],processing_error=row['processing_error'])],append=True)
        cap=(row['result'] or {}).get('capability_review',{})
        print(f"joint {len(collected)}/{meta['n0']} Q{row['ordinal']} {row['original_subtask']} -> {cap.get('predicted_subtask')}: {row['final_decision'] or 'processing_error'}",flush=True)
    rows=sorted(collected.values(),key=lambda r:r['ordinal'])
    elapsed=time.monotonic()-start
    write_json(a.output/'results.json',dict(meta=meta,rows=rows,elapsed_s=elapsed))
    summarize(a.output,meta,rows,elapsed)
    if a.input:export_candidates(a.output,rows,raw_records)
    else:compare_human(bundle,a.output,pack,rows)


if __name__=='__main__':
    main()

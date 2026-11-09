from __future__ import annotations

import argparse
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..io import read_json, write_json, write_jsonl
from .client import Client, RULES, validate
from .inputs import digest, duplicate_groups, explicit_single_video, issue, load_bundle, local_checks
from .report import report


def stage2_decision(result, original):
    status = result['classification_status']
    if status != 'unique':
        return status
    if (result['predicted_capability'], result['predicted_subtask']) == (
            original['original_capability'], original['original_subtask']):
        return 'keep_original'
    return 'relabel'


def stage1_decision(row):
    if row['processing_error'] or row['result'] is None:
        return None
    if row['result']['source_requirement']['judgment'] == 'single':
        return 'rework'
    return 'rework' if row['local_issues'] else row['result']['stage1_decision']


def finalize_stage1(row):
    """Enforce the organizer's policy after both fresh and cached model calls."""
    decision = stage1_decision(row)
    if decision == 'rework' and row['result']['source_requirement']['judgment'] == 'single' and row['result']['stage1_decision'] != 'rework':
        # Keep the unmodified model response traceable; result is the effective
        # result, so consumers cannot accidentally read a conflicting pass.
        row['model_result'] = row['result']
        row['result'] = dict(row['result'], stage1_decision='rework', issues=[
            *row['result']['issues'],
            issue('single_source_not_allowed', '',
                  'source_requirement.judgment=single；按初筛规则，单视频任务统一标为rework，不进入pass队列。')])
    row['stage1_decision'] = decision
    row['decision_policy_version'] = 'single_source_rework_v1'
    return row


def run_wave(args, stage, items, originals, exact):
    client, rows = Client(args), {}
    started = time.monotonic()

    def work(item):
        problems, notes = local_checks(item)
        if item['item_id'] in exact:
            problems.append(issue('exact_duplicate', '', '与 ' + exact[item['item_id']] + ' 完全重复'))
        single = explicit_single_video(item) if stage == 'stage1' else None
        if single:
            index, quote = single
            result = dict(stage1_decision='rework', issues=[p for p in problems if p['code'] == 'explicit_single_video_task'], nonblocking_notes=notes,
                          source_requirement=dict(judgment='single', derivation=f'题干逐字限定：{quote}'),
                          core_inputs=[index], redundant_inputs=[m['index'] for m in item['media'] if m['index'] != index],
                          manual_verification_scope=['媒体内容和参考答案未核验'],
                          media_verification='not_checked', answer_verification='not_checked')
            validate(stage, result, item)
            row = dict(item_id=item['item_id'], ordinal=item['ordinal'], stage=stage, mode=args.mode,
                       input_hash=digest(item), processing_error=None, result=result,
                       execution_backend='local_rules', rule_version='explicit_single_video_v1',
                       cache_hit=False, attempts=[], invocation_attempts=[], elapsed_s=0,
                       invocation_elapsed_s=0)
        else:
            row = client.run(stage, item, originals[item['item_id']] if stage == 'stage2' else None,
                             dict(local_issues=problems, local_notes=notes) if stage == 'stage1' else None)
        if stage == 'stage1':
            row.update(local_issues=problems, local_notes=notes)
            finalize_stage1(row)
        else:
            if args.adjudicate and row['result'] and row['result']['classification_status'] in ('ambiguous', 'insufficient'):
                extra = client.run(stage, item, originals[item['item_id']],
                                   context={'initial_analysis': row['result'], 'purpose': 'optional_adjudication'})
                row['primary_result'] = row['result']
                row['adjudication'] = extra
                row['invocation_attempts'] = [dict(x, phase='primary') for x in row.get('invocation_attempts', [])] + [
                    dict(x, phase='adjudication') for x in extra.get('invocation_attempts', [])]
                if not extra['processing_error']:
                    row['result'] = extra['result']
            row.update(originals[item['item_id']])
            row['stage2_decision'] = stage2_decision(row['result'], row) if row['result'] else None
        return row

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(work, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            # Per-item cache is committed before the checkpoint. Failed calls are
            # checkpointed too but remain retryable on the next invocation.
            row = future.result()
            rows[row['item_id']] = row
            write_json(args.output / f'{stage}_checkpoint.json', rows)
            print(f'{stage} {len(rows)}/{len(items)} {row["item_id"]}: '
                  f'{row.get(stage + "_decision") or "processing_error"}', flush=True)
    return sorted(rows.values(), key=lambda r: r['ordinal']), time.monotonic() - started


def legacy_main(argv=None):
    p = argparse.ArgumentParser(description='DeepSeek 两波文本初筛；不写回原题或人工接收记录')
    p.add_argument('--bundle', type=Path, default=Path(__file__).resolve().parents[3] / 'review_bundle')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--stage', choices=['all', 'stage1', 'stage2'], default='all')
    p.add_argument('--stage1-results', type=Path, help='stage2 必须指定 stage1.json；不会改写该文件')
    p.add_argument('--mode', choices=['live', 'mock', 'dry-run'], default='dry-run')
    p.add_argument('--model', default='deepseek-flash')
    p.add_argument('--base-url', default='https://api.deepseek.com')
    p.add_argument('--proxy-mode', choices=['system','direct'], default='system')
    p.add_argument('--thinking', choices=['enabled', 'disabled'], default='enabled')
    p.add_argument('--max-tokens', type=int, default=16384)
    p.add_argument('--timeout', type=float, default=180)
    p.add_argument('--concurrency', type=int, default=3)
    p.add_argument('--retries', type=int, default=2)
    p.add_argument('--adjudicate', action='store_true', help='仅对第二波 ambiguous/insufficient 额外调用一次，单列用量')
    p.add_argument('--limit', type=int)
    p.add_argument('--item-id', action='append', help='只运行指定item_id，可重复；不与limit同时使用')
    p.add_argument('--reviewer-resources', type=Path, help='JSON：明确提供的测量结果和允许工具；缺省 unknown')
    a = p.parse_args(argv)
    if a.item_id and a.limit is not None:
        p.error('--item-id cannot be combined with --limit')
    if a.stage == 'stage2' and not a.stage1_results:
        p.error('--stage stage2 requires --stage1-results')
    if a.stage1_results and a.stage != 'stage2':
        p.error('--stage1-results is only valid for stage2')
    if min(a.timeout, a.concurrency, a.max_tokens) <= 0 or a.retries < 0 or (a.limit is not None and a.limit < 1):
        p.error('invalid limits')
    from urllib.parse import urlsplit
    url = urlsplit(a.base_url)
    if url.scheme != 'https' or url.username or url.password or url.query or url.fragment:
        p.error('base-url must be HTTPS without credentials/query/fragment')
    a.output = a.output.resolve()
    if a.stage1_results and (a.output / 'stage1.json').resolve() == a.stage1_results.resolve():
        p.error('stage2 output must differ from the source stage1 directory')
    existing = read_json(a.output / 'manifest.json')
    if existing and existing['mode'] != a.mode:
        p.error('不同模式须使用不同输出目录，避免把模拟结果当成真实结果')
    resources = read_json(a.reviewer_resources) if a.reviewer_resources else None
    pack, items, originals = load_bundle(a.bundle.resolve(), resources, a.item_id)
    if a.limit:
        items = items[:a.limit]
    if a.mode == 'live' and not os.environ.get('DEEPSEEK_API_KEY') and (
            a.stage != 'stage1' or any(not explicit_single_video(i) for i in items)):
        p.error('需要设置 DEEPSEEK_API_KEY；密钥不要写入文件或命令历史')
    exact, near = duplicate_groups(items)
    meta = dict(mode=a.mode, model=a.model, base_url=a.base_url, pack_id=pack['pack_id'],
                pack_version=pack.get('version'), source_sha256=pack.get('source_sha256'),
                input_hash=digest(items), n0=len(items), pdf_excerpt_hash=digest(RULES),
                scope='exploratory_text_and_metadata_only; no evidence table; no media/answer verification',
                original_labels=originals)
    write_json(a.output / 'manifest.json', meta)
    write_json(a.output / 'near_duplicate_candidates.json', near)
    write_json(a.output / 'sanitized_inputs.json', items)
    if a.mode == 'dry-run':
        rows = [dict(item_id=i['item_id'], ordinal=i['ordinal'], stage1_decision=None,
                     processing_error=None, status='not_run',
                     local_issues=local_checks(i)[0], local_notes=local_checks(i)[1]) for i in items]
        write_json(a.output / 'preflight.json', dict(meta=meta, rows=rows))
        report(a.output, meta, items, [], [], {})
        return
    durations = {}
    if a.stage == 'stage2':
        saved = read_json(a.stage1_results)
        if saved['meta']['mode'] != a.mode or saved['meta']['input_hash'] != digest(items):
            p.error('stage1 模式或题面/媒体元数据不匹配，请使用相同 bundle/resources/limit')
        s1 = saved['rows']
        if {r['item_id'] for r in s1} != {i['item_id'] for i in items} or len(s1) != len(items):
            p.error('stage1 必须包含完整、无重复的固定集合')
        by_id = {i['item_id']: i for i in items}
        for row in s1:
            item = by_id[row['item_id']]
            problems, notes = local_checks(item)
            if item['item_id'] in exact:
                problems.append(issue('exact_duplicate', '', '与 ' + exact[item['item_id']] + ' 完全重复'))
            if row['input_hash'] != digest(item) or row['local_issues'] != problems:
                p.error('stage1 题面或本地规则结果不匹配')
            if row['result'] is not None:
                validate('stage1', row['result'], item)
            if row['stage1_decision'] != stage1_decision(row):
                p.error('stage1 状态不一致')
            row['invocation_attempts'] = []  # Source-wave calls were billed in its own run.
        meta['stage1_source'] = str(a.stage1_results.resolve())
        meta['stage1_source_hash'] = digest(saved)
        write_json(a.output / 'manifest.json', meta)
    else:
        s1, durations['stage1'] = run_wave(a, 'stage1', items, originals, exact)
        write_json(a.output / 'stage1.json', dict(meta=meta, rows=s1, elapsed_s=durations['stage1']))
    eligible = {r['item_id'] for r in s1 if not r['processing_error'] and r['stage1_decision'] == 'pass'}
    s2 = []
    if a.stage != 'stage1':
        s2, durations['stage2'] = run_wave(a, 'stage2', [i for i in items if i['item_id'] in eligible], originals, exact)
        write_json(a.output / 'stage2.json', dict(meta=meta, eligible_item_ids=sorted(eligible), rows=s2,
                                                elapsed_s=durations['stage2']))
    report(a.output, meta, items, s1, s2, durations, stage2_run=a.stage != 'stage1')
    # Offline-only analysis is deliberately loaded after all inference calls.
    human_file = a.bundle / 'review_results/author_audit/R01_3439162580_reviews_with_comments.json'
    if human_file.exists():
        human = read_json(human_file)
        if human.get('pack_id') == pack['pack_id'] and human.get('source_sha256') == pack.get('source_sha256'):
            h = human['reviews']
            by2 = {r['item_id']: r for r in s2}
            comparisons = []
            for r in s1:
                c = h.get(r['item_id'], {}).get('choices', {})
                comparisons.append(dict(item_id=r['item_id'], ordinal=r['ordinal'],
                    stage1_decision=r['stage1_decision'], stage2_decision=by2.get(r['item_id'], {}).get('stage2_decision'),
                    predicted_subtask=(by2.get(r['item_id'], {}).get('result') or {}).get('predicted_subtask'),
                    human_subtask=c.get('subtask'), human_source_need=c.get('source_need'),
                    human_shortcut=c.get('shortcut'), human_clarity=c.get('clarity'),
                    human_redundancy=c.get('redundancy'), human_decision=c.get('decision')))
            write_json(a.output / 'offline_human_comparison.json', comparisons)
        else:
            write_json(a.output / 'offline_human_comparison.json', {'skipped': 'pack/version mismatch'})


def main(argv=None):
    import sys
    arguments = sys.argv[1:] if argv is None else argv
    if any(arg in ('--stage', '--stage1-results') or arg.startswith(('--stage=', '--stage1-results=')) for arg in arguments):
        return legacy_main(arguments)
    from .joint import main as joint_main
    return joint_main(arguments)


if __name__ == '__main__':
    main()

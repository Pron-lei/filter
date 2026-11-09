from collections import Counter

from ..io import write_json, write_jsonl


def report(output, meta, items, s1, s2, durations, stage2_run=False):
    def identity(row):
        return {k: row[k] for k in ('ordinal', 'item_id')}

    def queues(rows, stage, states):
        return {state: [identity(r) for r in rows if
                       ('processing_error' if r['processing_error'] else r[stage + '_decision']) == state]
                for state in states}

    q1 = queues(s1, 'stage1', ['pass', 'rework', 'uncertain', 'processing_error'])
    q2 = queues(s2, 'stage2', ['keep_original', 'relabel', 'ambiguous', 'insufficient', 'processing_error'])
    n1 = len(q1['pass']) if s1 else None
    n2 = len(q2['keep_original']) if stage2_run else None
    ratio = lambda a, b: a / b if a is not None and b else None
    usage = {}
    for name, rows in [('stage1', s1), ('stage2', s2)]:
        attempts = [a for r in rows for a in r.get('invocation_attempts', [])]
        totals = Counter()
        for a in attempts:
            for k, v in a['usage'].items():
                if isinstance(v, (int, float)):
                    totals[k] += v
        retry = [a for a in attempts if a['attempt'] > 1]
        adjudication = [a for a in attempts if a.get('phase') == 'adjudication']
        adjudication_usage = Counter()
        for attempt in adjudication:
            for k, v in attempt['usage'].items():
                if isinstance(v, (int, float)):
                    adjudication_usage[k] += v
        usage[name] = dict(elapsed_s=durations.get(name), attempts=len(attempts),
                           successful_items=sum(not r['processing_error'] for r in rows),
                           cache_hits=sum(r.get('cache_hit', False) for r in rows),
                           tokens=dict(totals), retry_attempts=len(retry),
                           retry_elapsed_s=sum(a['elapsed_s'] for a in retry),
                           retry_usage=[a['usage'] for a in retry], actual_api_cost=None,
                           adjudication_attempts=len(adjudication), adjudication_usage=dict(adjudication_usage),
                           adjudication_actual_api_cost=None,
                           cost_note='未取得实际账单费用；不以估算冒充实际费用',
                           unknown_usage_attempts=sum(bool(a['error']) and not a['usage'] for a in attempts))
    cross, boundary, kept = Counter(), Counter(), Counter()
    for r in s2:
        if r['processing_error']:
            continue
        v = r['result']
        cross[f"{r['original_capability']}/{r['original_subtask']} -> {v['predicted_capability']}/{v['predicted_subtask']}"] += 1
        if r['stage2_decision'] == 'keep_original':
            kept[v['predicted_capability']] += 1
        for alt in v['alternative_labels']:
            if not alt['excluded']:
                boundary['/'.join(sorted({v['predicted_subtask'] or 'unknown', alt['subtask']}))] += 1
    stats = dict(mode=meta['mode'], actual_api_run=meta['mode'] == 'live' and any(
                 r.get('execution_backend') != 'local_rules' for r in s1 + s2),
                 n0=meta['n0'], n1=n1 if meta['mode'] == 'live' else None,
                 n2=n2 if meta['mode'] == 'live' else None,
                 stage1_pass_rate=ratio(n1, meta['n0']) if meta['mode'] == 'live' else None,
                 stage2_conditional_pass_rate=ratio(n2, n1) if meta['mode'] == 'live' else None,
                 cumulative_strict_retention_rate=ratio(n2, meta['n0']) if meta['mode'] == 'live' else None,
                 potential_usable_after_relabel=(n2 + len(q2['relabel'])) if n2 is not None and meta['mode'] == 'live' else None,
                 stage2_denominator=n1, stage2_successful_items=sum(not r['processing_error'] for r in s2),
                 stage1_queues=q1, stage2_queues=q2, usage=usage,
                 original_to_predicted=dict(cross), unresolved_boundaries=dict(boundary),
                 strict_retained_capabilities=dict(kept))
    write_json(output / 'statistics.json', stats)
    write_json(output / 'queues.json', dict(stage1=q1, stage2=q2))
    by1, by2 = {r['item_id']: r for r in s1}, {r['item_id']: r for r in s2}
    write_jsonl(output / 'all_results.jsonl', [dict(item_id=i['item_id'], ordinal=i['ordinal'],
                stage1=by1.get(i['item_id']), stage2=by2.get(i['item_id'])) for i in items])
    write_json(output / 'stage1_pass.json', q1['pass'])
    write_json(output / 'strict_retained.json', q2['keep_original'])
    write_json(output / 'suggested_relabels.json', [r for r in s2 if r['stage2_decision'] == 'relabel'])
    show = lambda x: str(x) if x is not None else '待真实 API 运行'
    lines = ['# DeepSeek 两波探索性试跑报告', '',
             f"模式：{meta['mode']}。{meta['n0']}题 → 第一波通过 {show(stats['n1'])} 题 → 原标签严格通过 {show(stats['n2'])} 题。", '',
             f"本次是否执行API调用：{stats['actual_api_run']}；本地规则直接处理：{sum(r.get('execution_backend') == 'local_rules' for r in s1)}题。", '',
             '仅审题面和元数据；无配套 evidence 表。媒体事实和参考答案均未核验。',
             '这是重复使用当前题集的探索性试跑，不是独立泛化验证；不追求固定保留数量。', '',
             f"第一波通过率：{stats['stage1_pass_rate']}；第二波条件通过率：{stats['stage2_conditional_pass_rate']}；累计严格保留率：{stats['cumulative_strict_retention_rate']}。", '',
             f"接受建议重标后的潜在可用数：{stats['potential_usable_after_relabel']}。仍须人工复核，N1−N2 不等同于坏题数。", '',
             f"第二波固定分母：{stats['stage2_denominator']}；成功处理：{stats['stage2_successful_items']}。", '']
    if meta['mode'] != 'live':
        lines += ['**以下队列仅为离线工程产物，不能解读为实际筛选结论。**', '']
    for name, queue in [('第一波', q1), ('第二波', q2)]:
        lines += [f'## {name}队列', '']
        for state, group in queue.items():
            lines += [f"- {state}：{len(group)}；" + '、'.join(f"{r['ordinal']} ({r['item_id']})" for r in group)]
        lines += ['']
    for name, rows in [('第一波', s1), ('第二波', s2)]:
        lines += [f'## {name}典型退出当前队列的记录', '']
        for row in [r for r in rows if r.get('stage1_decision', r.get('stage2_decision')) not in ('pass', 'keep_original')][:5]:
            result = row.get('result') or {}
            reason = row['processing_error'] or result.get('rationale') or str(row.get('local_issues', []) + result.get('issues', []))
            lines += [f"- {row['ordinal']} ({row['item_id']})：{reason}"]
        lines += ['']
    lines += ['## 用量、费用和分类分布', '',
              '逐次 API 用量（含缓存命中 token 和重试）、耗时、交叉表、争议边界及严格保留能力分布见 statistics.json；逐题明细见 all_results.jsonl。',
              '实际费用未知时为空；传输失败可能无法取得 token 用量，单列 unknown_usage_attempts。缓存命中不算本次新增 API 用量。',
              '人工标签对照仅在全部模型调用后生成 offline_human_comparison.json，不参与提示词、阈值或质量判定。', '']
    (output / 'report.md').write_text('\n'.join(lines), encoding='utf-8')

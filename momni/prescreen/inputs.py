from __future__ import annotations

import hashlib
import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path

from ..io import read_json, read_jsonl


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def issue(code, quote, detail, certainty='confirmed'):
    return dict(code=code, quote=quote, detail=detail, certainty=certainty)


def load_bundle(bundle: Path, resources=None, item_ids=None):
    pack = read_json(bundle / 'reviewer_app/data/pack.json')
    questions = pack['questions']
    if item_ids:
        requested = set(item_ids)
        questions = [q for q in questions if q['item_id'] in requested]
        missing = requested - {q['item_id'] for q in questions}
        if missing:
            raise ValueError(f'unknown item_id: {sorted(missing)}')
    manifest = read_json(bundle / 'organizer_only/reconstruction_manifest.json')
    catalog = read_json(bundle / 'reviewer_app/data/media.json')
    candidates = {r['item_id']: r for r in read_jsonl(bundle / 'organizer_only/candidate_records.jsonl')}
    by_asset = {r['asset']: r for r in manifest['media']}
    by_mid = {r['media_id']: r for r in manifest['media']}
    probe = bundle / 'tools/ffmpeg/bin/ffprobe.exe'

    def inspect(asset):
        meta = catalog[asset]
        path = bundle / 'media' / meta['filename']
        result = dict(file_available=path.is_file(), duration_s=meta.get('duration_s'),
                      tracks=None, track_status='unknown')
        if result['file_available'] and probe.exists():
            try:
                r = subprocess.run([str(probe), '-v', 'error', '-show_entries',
                                    'format=duration:stream=codec_type', '-of', 'json', str(path)],
                                   capture_output=True, text=True, timeout=30, check=True)
                data = json.loads(r.stdout)
                result.update(tracks=[s['codec_type'] for s in data['streams']],
                              track_status='probed_metadata',
                              duration_s=float(data['format']['duration']))
            except (OSError, ValueError, subprocess.SubprocessError, KeyError):
                pass  # Unknown tracks are not a quality failure.
        return asset, result

    used = sorted({m['asset'] for q in questions for m in q['media']})
    with ThreadPoolExecutor(max_workers=8) as pool:
        files = dict(pool.map(inspect, used))
    items, originals, seen = [], {}, set()
    for q in questions:
        iid = q['item_id']
        if iid in seen:
            raise ValueError(f'duplicate item_id: {iid}')
        seen.add(iid)
        record = candidates[iid]
        # A matching item_id alone does not establish the same question version.
        if isinstance(q['question'], dict) and 'zh' in q['question'] and record.get('question') is not None:
            if q['question']['zh'] != record['question'] or any(
                    isinstance(v, dict) and 'zh' in v and v['zh'] != record.get('options', {}).get(k)
                    for k, v in q['options'].items()):
                raise ValueError(f'{iid}: pack/candidate question version mismatch')
        expected = manifest['question_media_order'][iid]
        actual = [by_asset[m['asset']]['media_id'] for m in q['media']]
        if actual != expected or actual != record['input_media_order']:
            raise ValueError(f'{iid}: media version/order mismatch')
        originals[iid] = dict(original_capability=record['capability'],
                              original_subtask=record['subtask'],
                              annotation_version=record.get('annotation_version'),
                              generation_version=record.get('generation_version'))
        media = []
        for m in q['media']:
            raw = by_asset[m['asset']]
            parent = raw.get('parent_media_id')
            parent_asset = by_mid[parent]['asset'] if parent in by_mid else None
            media.append(dict(index=m['index'], asset=m['asset'], **files[m['asset']],
                              parent_asset=parent_asset,
                              parent_duration_s=files.get(parent_asset, {}).get('duration_s', by_mid.get(parent, {}).get('duration_s')),
                              relationship_source='reconstruction_manifest' if parent else None,
                              relationship_status='metadata_declared_not_content_verified' if parent else None))
        # Explicit allowlist. No candidate prose, answers, flags or human comments.
        items.append(dict(item_id=iid, ordinal=q['ordinal'], question=q['question'],
                          options=q['options'], media=media,
                          reviewer_resources=resources or dict(measurements='unknown', tools='unknown')))
    return pack, items, originals


def text(value):
    return '\n'.join(str(v) for v in value.values()) if isinstance(value, dict) else str(value)


def explicit_single_video(item):
    """Conservative surface rule, independent of model labels or media facts.

    Requires an exclusive task instruction at a sentence start, one legal video
    number throughout the stem, and no parent/reference qualification. Options
    cannot introduce this instruction. Other single-source tasks stay with LLM.
    """
    stem = text(item['question'])
    refs = {int(n) for n in re.findall(r'(?:视频\s*|\bvideo\s+)(\d+)', stem, re.I)}
    known = {m['index'] for m in item['media']}
    if len(refs) != 1 or not refs <= known:
        return None
    patterns = [
        r'(?:^|[。！？\n])\s*(?:请)?(?P<quote>(?:只|仅)(?:考虑|考察|观看|查看|观察|使用)\s*视频\s*(?P<index>\d+))(?!\d)(?=\s*[,，。；;！？!?]|\s*$)',
        r'(?:^|[.!?\n])\s*(?P<quote>(?:consider|watch|view|use|inspect)\s+only\s+video\s+(?P<index>\d+))\b(?=\s*[,.;!?]|\s*$)',
    ]
    for pattern in patterns:
        match = re.search(pattern, stem, re.I | re.M)
        if match and int(match['index']) in refs:
            return int(match['index']), match['quote']
    return None


def local_checks(item):
    problems, notes = [], []
    q, options, media = item['question'], item['options'], item['media']
    single = explicit_single_video(item)
    if single:
        index, quote = single
        problems.append(issue('explicit_single_video_task', quote,
                              f'题干明确将整个任务限定为视频{index}，不满足多视频题要求；其他输入为冗余，不能据此放行。'))
    if not text(q).strip() or set(options) != {'A', 'B', 'C', 'D'} or any(not text(v).strip() for v in options.values()) or not media:
        problems.append(issue('required_fields', 'question/options/media', '必填题干、四个选项或媒体缺失'))
    for lang in ('zh', 'en'):
        values = [v.get(lang, '') if isinstance(v, dict) else v for v in options.values()]
        if all(values) and len(values) != len(set(values)):
            problems.append(issue('duplicate_options', lang, '存在完全重复的选项'))
    indices = {m['index'] for m in media}
    if len(indices) != len(media) or indices != set(range(1, len(media) + 1)):
        problems.append(issue('media_order', str(sorted(indices)), '编号重复或不连续'))
    body = text(q) + '\n' + '\n'.join(text(v) for v in options.values())
    for match in re.finditer(r'(?:视频\s*|Video\s+)(\d+)', text(q), re.I):
        if int(match[1]) not in indices:
            problems.append(issue('invalid_media_reference', match[0], '引用不存在的媒体编号'))
    for m in media:
        if m['file_available'] is False:
            problems.append(issue('missing_file', str(m['index']), '所需媒体文件缺失'))
        elif m['file_available'] is None:
            notes.append(issue('file_availability_unknown', '', f"视频{m['index']}文件可用性未知，需在媒体审核环节确认", 'unknown'))
    # Only unambiguous, explicitly scoped times are blocked locally. Other forms
    # remain for the text reviewer; never compare a parent's time to a child.
    for match in re.finditer(r'(?:视频\s*|Video\s+)(\d+)(?:自身|自己|的|\s|\'s|时间轴|timeline|at|在|第)*?(\d+(?:\.\d+)?)\s*(?:秒|seconds?\b|s\b)', text(q), re.I):
        m = next((m for m in media if m['index'] == int(match[1])), None)
        if m and m['duration_s'] is not None and float(match[2]) > m['duration_s']:
            problems.append(issue('time_out_of_range', match[0], '显式所指视频时间超过其自身时长'))
    if re.search(r'母本|parent', body, re.I):
        notes.append(issue('parent_time_scope', '', '母本时间须按显式母本编号或 parent_duration_s；指代不明确时交模型判断，不套用变体时长', 'unknown'))
    return problems, notes


def duplicate_groups(items, max_near_pairs=5000):
    seen, exact, near = {}, {}, []
    for item in items:
        signature = digest({k: item[k] for k in ('question', 'options', 'media', 'reviewer_resources')})
        if signature in seen:
            exact[item['item_id']] = seen[signature]
        else:
            seen[signature] = item['item_id']
    # Exact hashes remain O(N). Similarity is advisory and has a fixed budget.
    pairs = 0
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            if pairs >= max_near_pairs:
                return exact, near
            pairs += 1
            if SequenceMatcher(None, text(a['question']), text(b['question'])).ratio() >= .85:
                near.append([a['item_id'], b['item_id']])
    return exact, near

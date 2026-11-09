from __future__ import annotations

import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from ..io import read_json, write_json
from .inputs import digest

ASSETS = Path(__file__).parent
RULES = read_json(ASSETS / 'pdf_rules.json')


def collect_stream(lines, timeout=240):
    content, usage, finish, model, done = [], {}, None, None, False
    started = time.monotonic()
    for raw in lines:
        if time.monotonic() - started > timeout:
            raise TimeoutError('stream exceeded request deadline')
        line = raw.decode('utf-8').strip()
        if not line.startswith('data:'):
            continue
        payload = line[5:].strip()
        if payload == '[DONE]':
            done = True
            break
        chunk = json.loads(payload)
        if chunk.get('error'):
            raise ValueError('API stream error')
        model = chunk.get('model', model)
        if chunk.get('usage'):
            usage = chunk['usage']
        for choice in chunk.get('choices', []):
            delta = choice.get('delta', {})
            if delta.get('content'):
                content.append(delta['content'])
            if choice.get('finish_reason'):
                finish = choice['finish_reason']
    if finish is None or (not done and finish!='stop'):
        raise ValueError('incomplete API stream')
    return dict(model=model, usage=usage, choices=[dict(finish_reason=finish,
                message={'content': ''.join(content)})],transport_metadata={'done_marker_received':done})


def canonical_citation_pages(stage, result):
    """Use known source metadata only when the quoted source text is verified.

    Never repairs labels, rationale or fabricated quotations. Raw model output
    remains in attempts, and every page correction is explicitly recorded.
    """
    if stage not in ('stage2', 'joint') or not isinstance(result, dict):
        return []
    capability = result.get('capability_review', {}) if stage == 'joint' else result
    if not isinstance(capability, dict) or not isinstance(capability.get('rule_citations'), list):
        return []
    corrections = []
    for citation in capability['rule_citations']:
        if not isinstance(citation, dict):
            continue
        rule = RULES['rules'].get(citation.get('rule_id'))
        quote = citation.get('quote')
        if rule and isinstance(quote, str) and quote.strip() and ''.join(quote.split()) in ''.join(rule['text'].split()):
            if citation.get('page') != rule['page']:
                corrections.append(dict(rule_id=citation['rule_id'], field='page',
                                        original=citation.get('page'), canonical=rule['page']))
                citation['page'] = rule['page']
    return corrections


def canonical_surface_quotes(stage, result, item):
    """Repair whitespace-only quote differences against actual input text."""
    if stage not in ('stage1', 'joint') or not isinstance(result, dict):
        return []
    surface = result.get('surface_review') if stage == 'joint' else result
    if not isinstance(surface, dict):
        return []
    def strings(value):
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [s for v in value.values() for s in strings(v)]
        if isinstance(value, list):
            return [s for v in value for s in strings(v)]
        return []
    sources = strings(item)
    corrections = []
    groups=[(field,surface.get(field),sources) for field in ('issues','nonblocking_notes')]
    if stage=='joint' and isinstance(result.get('source_audit'),dict):
        groups.append(('source_audit.task_obligations',result['source_audit'].get('task_obligations'),strings(item['question'])+strings(item['options'])))
    for field,notes,sources in groups:
        if not isinstance(notes, list):
            continue
        for index, note in enumerate(notes):
            if not isinstance(note, dict):
                continue
            quote = note.get('quote')
            if not isinstance(quote, str) or not quote or any(quote in s for s in sources):
                continue
            needle = ''.join(quote.split())
            if not needle:
                continue
            for source in sources:
                positions = [i for i, c in enumerate(source) if not c.isspace()]
                compact = ''.join(source[i] for i in positions)
                start = compact.find(needle)
                while start >= 0:
                    canonical = source[positions[start]:positions[start + len(needle) - 1] + 1]
                    # Keep ASCII word/number boundaries: "1 2" must not be
                    # repaired to "12", nor two English words merged into one.
                    if re.findall(r'[A-Za-z0-9_]+', quote) == re.findall(r'[A-Za-z0-9_]+', canonical):
                        corrections.append(dict(field=f'{field}[{index}].quote', original=quote, canonical=canonical))
                        note['quote'] = canonical
                        break
                    start = compact.find(needle, start + 1)
                if note['quote'] != quote:
                    break
    return corrections


def validate(stage, result, item):
    schema_file = 'joint_v3.schema.json' if stage=='joint' and isinstance(result,dict) and 'source_audit' in result else f'{stage}.schema.json'
    Draft202012Validator(read_json(ASSETS / schema_file)).validate(result)
    if stage == 'joint':
        validate('stage2', result['capability_review'], item)
        validate('stage1', result['surface_review'], item)
        if 'source_audit' in result:
            audit=result['source_audit'];surface=result['surface_review']
            required={n for o in audit['task_obligations'] for n in o['media_indices']}
            known={m['index'] for m in item['media']}
            if not required<=known:raise ValueError('source obligations reference unknown media')
            question=item['question'];texts=list(question.values()) if isinstance(question,dict) else [question]
            for option in item['options'].values():
                texts.extend(option.values() if isinstance(option,dict) else [option])
            if any(not any(o['quote'] in s for s in texts) for o in audit['task_obligations']):
                raise ValueError('source obligation quote not found in question/options')
            if not required<=set(surface['core_inputs']) or required & set(surface['redundant_inputs']):
                raise ValueError('required source omitted or marked redundant')
            if surface['source_requirement']['judgment']=='single':
                if audit['sufficiency_basis'] not in ('explicit_task','provided_evidence') or len(required)>1 or len(surface['core_inputs'])!=1:
                    raise ValueError('single source is not supported by full-task obligations')
            if surface['source_requirement']['judgment']=='multiple' and len(surface['core_inputs'])<2:
                raise ValueError('multiple requires at least two core sources')
            if audit['sufficiency_basis']=='option_structure_only' and any(i['code'] in audit['shortcut_issue_codes'] for i in surface['issues']):
                raise ValueError('unverified option-structure risk cannot be a confirmed blocker')
        return
    if stage == 'stage1':
        known = {m['index'] for m in item['media']}
        core, redundant = set(result['core_inputs']), set(result['redundant_inputs'])
        if not (core | redundant) <= known or core & redundant:
            raise ValueError('invalid/overlapping input indices')
        for row in result['issues'] + result['nonblocking_notes']:
            def strings(value):
                if isinstance(value, str):
                    return [value]
                if isinstance(value, dict):
                    return [s for v in value.values() for s in strings(v)]
                if isinstance(value, list):
                    return [s for v in value for s in strings(v)]
                return []
            if row['quote'] and not any(row['quote'] in value for value in strings(item)):
                raise ValueError('issue quote not found in input')
        if result['stage1_decision'] == 'pass' and result['issues']:
            raise ValueError('pass with blocking issues; place nonblocking risks in notes')
        if result['stage1_decision'] != 'pass' and not result['issues']:
            raise ValueError('non-pass requires a specific issue')
    else:
        cap, sub = result['predicted_capability'], result['predicted_subtask']
        if not result['final_operation'].strip() or not result['rationale'].strip():
            raise ValueError('classification needs substantive final operation and rationale')
        if (cap is None) != (sub is None) or (sub and sub[0] != cap):
            raise ValueError('capability/subtask mismatch')
        unique = result['classification_status'] == 'unique'
        if unique and (not sub or not result['rule_citations'] or not result['alternative_labels']):
            raise ValueError('unique classification needs label, PDF citations and competing-label analysis')
        if unique and any(not r['excluded'] or not r['exclusion_reason'].strip() for r in result['alternative_labels']):
            raise ValueError('unresolved competing label cannot be unique')
        for citation in result['rule_citations']:
            rule = RULES['rules'][citation['rule_id']]
            normalize = lambda s: ''.join(s.split())
            if citation['page'] != rule['page'] or normalize(citation['quote']) not in normalize(rule['text']):
                raise ValueError('fabricated PDF quotation/page')


def mock_result(stage, item):
    if stage == 'joint':
        return dict(capability_review=mock_result('stage2', item), surface_review=mock_result('stage1', item))
    common = dict(media_verification='not_checked', answer_verification='not_checked')
    if stage == 'stage1':
        return dict(stage1_decision='pass', issues=[], nonblocking_notes=[],
                    source_requirement=dict(judgment='unknown', derivation='MOCK：仅验证工程接线，不作质量判断'),
                    core_inputs=[m['index'] for m in item['media']], redundant_inputs=[],
                    manual_verification_scope=['MOCK：所有媒体事实及答案均未核验'], **common)
    return dict(classification_status='insufficient', predicted_capability=None,
                predicted_subtask=None, auxiliary_operations=[], alternative_labels=[],
                final_operation='MOCK：无实际分类', rationale='仅验证第二波管线',
                rule_citations=[], implementation_rules=[], **common)


class Client:
    def __init__(self, args):
        self.args = args
        self.prompt_lock = threading.Lock()
        self.fatal_http_error = None

    def request(self, system, item):
        a = self.args
        protocol = getattr(a, 'api_format', 'chat-completions')
        stream = getattr(a, 'stream', True)
        key_env = getattr(a, 'api_key_env', 'DEEPSEEK_API_KEY')
        auth_style = getattr(a, 'auth_style', 'x-api-key' if protocol=='anthropic-messages' else 'bearer')
        key = os.environ[key_env] if auth_style!='none' else ''
        user = json.dumps(item, ensure_ascii=False)
        extra = getattr(a, 'extra_body', None) or {}
        if protocol == 'anthropic-messages':
            if stream:
                raise ValueError('native Messages streaming is not supported; omit --stream')
            body = dict(model=a.model, system=system, messages=[dict(role='user', content=user)],
                        max_tokens=a.max_tokens, stream=False)
            suffix = '/messages'
            headers = {'Content-Type':'application/json', 'x-api-key':key,
                       'anthropic-version':getattr(a, 'api_version', '2023-06-01')}
        else:
            body = dict(model=a.model, messages=[dict(role='system', content=system),
                        dict(role='user', content=user)], stream=stream)
            body[getattr(a, 'max_tokens_field', 'max_tokens')] = a.max_tokens
            if getattr(a, 'response_format', 'json-object') == 'json-object':
                body['response_format'] = {'type':'json_object'}
            if getattr(a, 'thinking', None) is not None:
                body['thinking'] = {'type':a.thinking}
            if stream and getattr(a,'stream_usage',True):
                body['stream_options'] = {'include_usage':True}
            suffix = '/chat/completions'
            headers = {'Content-Type':'application/json', 'Authorization':'Bearer '+key}
        headers.pop('Authorization', None)
        headers.pop('x-api-key', None)
        if auth_style!='none':
            headers['x-api-key' if auth_style=='x-api-key' else 'Authorization'] = key if auth_style=='x-api-key' else 'Bearer '+key
        body.update(extra)
        endpoint = a.base_url.rstrip('/')
        if not endpoint.endswith(suffix):
            endpoint += suffix
        req = urllib.request.Request(endpoint, data=json.dumps(body).encode(), headers=headers)
        proxy_mode = getattr(a, 'proxy_mode', 'system')
        if proxy_mode not in ('system', 'direct'):
            raise ValueError('unsupported proxy mode')
        open_request = (urllib.request.build_opener(urllib.request.ProxyHandler({})).open
                        if proxy_mode == 'direct' else urllib.request.urlopen)
        with open_request(req, timeout=a.timeout) as response:
            content_type=str(getattr(response,'headers',{}).get('Content-Type','')).lower()
            if stream and not content_type.startswith('application/json'):
                return collect_stream(response, a.timeout)
            result = json.load(response)
            if protocol == 'anthropic-messages':
                blocks = result.get('content', [])
                text = ''.join(b.get('text','') for b in blocks if b.get('type')=='text')
                stop = 'stop' if result.get('stop_reason')=='end_turn' else result.get('stop_reason')
                return dict(model=result.get('model'), usage=result.get('usage', {}),
                            choices=[dict(finish_reason=stop, message={'content':text})])
            return result

    def run(self, stage, item, original=None, context=None):
        a = self.args
        version = (getattr(a, 'prompt_version', None) or 'v1') if stage == 'joint' else ('v2' if stage == 'stage1' else 'v1')
        schema = read_json(ASSETS / (f'joint_{version}.schema.json' if stage=='joint' and version in ('v3','v4') else f'{stage}.schema.json'))
        system = (ASSETS / f'{stage}_{version}.txt').read_text(encoding='utf-8')
        if stage in ('stage2', 'joint'):
            system += '\nPDF 原文摘录（仅以下可作为引用）：\n' + json.dumps(RULES, ensure_ascii=False)
            if context:
                system += '\n这是可选的争议复核调用。请重新检查以下初次分析中的合理竞争解释；不能仅因需要唯一标签而消除歧义。初次分析也属于待审材料：\n' + json.dumps(context, ensure_ascii=False)
        system += '\n严格 JSON schema：\n' + json.dumps(schema, ensure_ascii=False)
        example=mock_result(stage, {'media': [{'index': 1}, {'index': 2}]})
        if stage=='joint' and version in ('v3','v4'):
            example['source_audit']=dict(task_obligations=[],sufficiency_basis='unknown',rationale='形状示例，非当前题目的判断',shortcut_issue_codes=[])
        system += '\nJSON 输出形状示例（内容只是示例，不是当前题目的判定）：\n' + json.dumps(example,ensure_ascii=False)
        model_item = {k:v for k,v in item.items() if k!='ordinal'} if stage=='joint' and version in ('v2','v3','v4') else item
        identity = dict(stage=stage, prompt_version=version, system=system, input=model_item,
                        model=a.model, base_url=a.base_url, max_tokens=a.max_tokens,
                        thinking=a.thinking, mode=a.mode, local_context=context)
        if hasattr(a, 'api_format'):
            identity['api_config'] = dict(api_format=a.api_format, stream=a.stream,
                response_format=a.response_format, max_tokens_field=a.max_tokens_field,
                api_version=a.api_version, extra_body=a.extra_body)
            if a.stream and not getattr(a,'stream_usage',True):
                identity['api_config']['stream_usage']=False
            if getattr(a,'auth_style','bearer')!='bearer':
                identity['api_config']['auth_style']=a.auth_style
        proxy_mode = getattr(a, 'proxy_mode', 'system')
        if proxy_mode != 'system':
            identity['proxy_mode'] = proxy_mode  # Preserve historical system-route cache keys.
        if stage in ('stage2', 'joint'):
            identity.update(pdf_hash=digest(RULES), original=original)
        key = digest(identity)
        system_hash = digest(system)
        prompt_path = a.output / 'prompts' / f'{stage}_{system_hash}.json'
        with self.prompt_lock:
            if not prompt_path.exists():
                write_json(prompt_path, dict(stage=stage, prompt_version=version, system=system, schema=schema))
        cache_root=getattr(a,'cache_root',None) or a.output/'cache'
        path = Path(cache_root) / stage / f'{key}.json'
        base = dict(item_id=item['item_id'], ordinal=item['ordinal'], stage=stage,
                    cache_key=key, input_hash=digest(item), mode=a.mode, system_hash=system_hash,
                    proxy_mode=proxy_mode)
        if path.exists():
            try:
                cached = read_json(path)
                Draft202012Validator(schema).validate(cached['result'])
                validate(stage, cached['result'], item)
                return dict(cached, ordinal=item['ordinal'],input_hash=digest(item),system_hash=system_hash, proxy_mode=proxy_mode,
                            cache_hit=True, invocation_attempts=[], invocation_elapsed_s=0)
            except (ValueError, KeyError, TypeError, ValidationError):
                pass  # A damaged cache is not evidence about item quality.
        attempts, started = [], time.monotonic()
        if self.fatal_http_error:
            return dict(base, processing_error=self.fatal_http_error + ': batch configuration failure; no new API call',
                        error_kind='configuration_error',result=None,cache_hit=False,attempts=[],
                        invocation_attempts=[],elapsed_s=0,invocation_elapsed_s=0)
        for attempt in range(a.retries + 1):
            tick, usage, raw, corrections, quote_corrections = time.monotonic(), {}, None, [], []
            try:
                if a.mode == 'mock':
                    result = mock_result(stage, item)
                    if stage=='joint' and version in ('v3','v4'):result['source_audit']=example['source_audit']
                else:
                    response = self.request(system, model_item)
                    usage = response.get('usage', {})
                    choice = response['choices'][0]
                    raw = choice['message']['content']
                    if isinstance(raw,list) and all(isinstance(b,dict) and b.get('type')=='text' and isinstance(b.get('text'),str) for b in raw):
                        raw=''.join(b['text'] for b in raw)
                    if choice.get('finish_reason') != 'stop':
                        raise ValueError('incomplete API response')
                    candidate = raw.strip() if isinstance(raw,str) else raw
                    if isinstance(candidate,str) and candidate.startswith('```') and candidate.endswith('```'):
                        candidate = candidate.split('\n',1)[1].rsplit('```',1)[0].strip()
                    result = json.loads(candidate)
                corrections = canonical_citation_pages(stage, result)
                quote_corrections = canonical_surface_quotes(stage, result, item)
                Draft202012Validator(schema).validate(result)
                validate(stage, result, item)
                attempts.append(dict(attempt=attempt + 1, elapsed_s=time.monotonic() - tick,
                                     usage=usage, error=None, raw_output=raw, citation_page_corrections=corrections,
                                     quote_whitespace_corrections=quote_corrections))
                break
            except Exception as exc:
                # Do not store request headers, credential-bearing URLs, or full HTTP bodies.
                if isinstance(exc, urllib.error.HTTPError):
                    err = f'HTTP {exc.code}'
                elif isinstance(exc, urllib.error.URLError):
                    reason = exc.reason
                    detail = re.sub(r'sk-[A-Za-z0-9]+', '[REDACTED]', str(reason))[:160]
                    err = f'URLError: {type(reason).__name__}: {detail}'
                elif isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError):
                    err = type(exc).__name__ + ': ' + str(exc)[:180]
                else:
                    err = type(exc).__name__ + ': invalid response or transport failure'
                attempts.append(dict(attempt=attempt + 1, elapsed_s=time.monotonic() - tick,
                                     usage=usage, error=err, raw_output=raw, citation_page_corrections=corrections,
                                     quote_whitespace_corrections=quote_corrections))
                if isinstance(exc, urllib.error.HTTPError) and exc.code in (401,402,403,404):
                    self.fatal_http_error = f'HTTP {exc.code}'
                permanent = isinstance(exc, urllib.error.HTTPError) and exc.code in (400,401,402,403,404,422)
                if permanent or attempt == a.retries:
                    break
                time.sleep(min(20, 2 ** attempt) + random.random())
        if attempts and attempts[-1]['error'] is None:
            row = dict(base, processing_error=None, result=result, cache_hit=False,
                       attempts=attempts, elapsed_s=time.monotonic() - started)
            # Local persistence failures must never trigger a second paid call.
            write_json(path, row)
            return dict(row, invocation_attempts=attempts, invocation_elapsed_s=row['elapsed_s'])
        return dict(base, processing_error=attempts[-1]['error'], result=None, cache_hit=False,
                    attempts=attempts, elapsed_s=time.monotonic() - started,
                    invocation_attempts=attempts, invocation_elapsed_s=time.monotonic() - started)

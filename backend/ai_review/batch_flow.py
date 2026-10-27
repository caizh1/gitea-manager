"""第三版计划：语法关系复核与候选汇总反证，旧计划不进入此路径。"""
from collections import Counter
import json
from itertools import combinations

from .common import digest
from . import batch_plan, batch_graph, batch_references, batch_locations, token_budget

VERSION = 'diff-batches-v3'
MAX_SINGLE_NET_ADDED_LINES = 1000  # 仅用于已冻结的第五版任务
MAX_SINGLE_CHANGED_LINES = 1200
# 历史分批模式的装箱目标；默认单次模式使用独立的千行与完整 Token 门槛。
PRIMARY_WORK_LINES = 320
ASSOCIATION_WORK_LINES = 320
SYSTEM = '''
本任务采用语法关系分批。primary 必须检查全部变更及已有保护；association 仅核查 connections 所列的跨函数交互，避免重新报告单函数已能成立的问题。
context 行是固定版本只读源码，不是新增变更。仅报告本次修改引入的具体缺陷。不同版本源码不可当成同时执行的代码。
P0/P1 必须给出合理可达的触发路径、严重影响及已有保护为何无效。反证逐个检查 candidates 的证据、调用约束和保护条件；每个编号独立给出结论，不能遗漏或凭标题相似合并。
'''
SINGLE_SYSTEM = '''
本任务一次审查完整 PR。必须检查全部变更、跨函数交互、已有保护以及提供的固定版本上下文。
context 行是只读源码，不是新增变更；不同版本不能当成同时执行的代码。仅报告本次修改引入的具体缺陷。
P0/P1 必须给出合理可达的触发路径、严重影响及已有保护为何无效。独立反证必须对 candidates 的每个编号分别给出结论，不能遗漏或凭标题相似合并。
'''


def make_unit(snapshot, kind, evidence, *, core=None, connections=None, candidates=None):
    unit = {'kind': kind, 'flow_version': VERSION, 'reference_protocol': snapshot.get('batch_reference_protocol', batch_references.PROTOCOL),
            'evidence': list({r['id']: r for r in evidence}.values()), 'core': core or []}
    if snapshot.get('review_mode') == 'single':
        unit['review_mode'] = 'single'
    if snapshot.get('batch_evidence_encoding'):
        unit['evidence_encoding'] = snapshot['batch_evidence_encoding']
    if snapshot.get('batch_output_protocol'):
        unit['output_protocol'] = snapshot['batch_output_protocol']
    if connections:
        unit['connections'] = connections
    if candidates is not None:
        unit['candidates'] = candidates
    unit['id'] = digest({'snapshot': snapshot['id'], **unit})[:24]
    return unit


def count(snapshot, unit, config, profile):
    return token_budget.count(batch_plan.messages(snapshot, unit, unit.get('candidates')), config, profile)


def validate_single_size(snapshot, *, net_limit=False):
    """新任务按新增加删除准入；第五版冻结任务仍沿用净增加门槛。"""
    from .diff import parse_diff
    kinds = [kind for patch in parse_diff(snapshot['diff'].encode())
             for hunk in patch.hunks for kind, _ in hunk.rows]
    added, deleted = kinds.count('+'), kinds.count('-')
    net = max(0, added - deleted)
    if not net_limit and added + deleted > MAX_SINGLE_CHANGED_LINES:
        raise token_budget.failure('PR_TOTAL_CHANGE_LIMIT',
            f'PR 新增 {added} 行、删除 {deleted} 行，合计 {added + deleted} 行，超过当前 {MAX_SINGLE_CHANGED_LINES} 行上限；未发送模型')
    if net_limit and net > MAX_SINGLE_NET_ADDED_LINES:
        raise token_budget.failure('PR_CHANGE_LIMIT',
            f'PR 新增 {added} 行、删除 {deleted} 行，净增加 {net} 行，超过当前 {MAX_SINGLE_NET_ADDED_LINES} 行上限；未发送模型')
    return {'added_lines': added, 'deleted_lines': deleted, 'net_added_lines': net}


def build(snapshot, config, history=0, *, single_request=False, net_limit=False):
    if single_request:
        line_counts = validate_single_size(snapshot, net_limit=net_limit)
        snapshot = dict(snapshot, review_mode='single')
    profile = token_budget.resolve(config)
    limit = token_budget.capacity(config) - 512
    original = batch_plan.build(snapshot, config, history, primary_limit=limit, associations_enabled=False)
    rows = list({r['id']: r for u in original['units'] for r in u['evidence']}.values())
    expected = [r['id'] for r in rows if r['kind'] in ('+', '-')]
    scopes, relations, limitations = batch_graph.analyze(snapshot, rows)
    owner, groups = {}, {}
    for row in rows:
        side = 'new' if row['kind'] != '-' else 'old'
        matches = [s for s in scopes.values() if s['path'] == row['path'] and s['side'] == side and
                   s['start'] <= (row[side] or 0) <= s['end']]
        scope = min(matches, key=lambda s: s['end'] - s['start']) if matches else None
        key = scope['id'] if scope else (row['path'], row['hunk'])
        if row['id'] in expected:
            owner[row['id']] = key
        groups.setdefault(key, []).append(row)
    changed_scopes = set(owner.values())
    affected_scopes = set(changed_scopes)
    # 沿固定语法关系收集变更影响范围，保留未改动调用方和保护条件。
    while True:
        reached = {key for relation in relations
                   if affected_scopes & {relation['left'], relation['right']}
                   for key in (relation['left'], relation['right'])}
        if reached <= affected_scopes:
            break
        affected_scopes.update(reached)
    parts = []
    for key, content in groups.items():
        core = [r['id'] for r in content if r['id'] in owner]
        if not core:
            continue
        evidence = scopes[key]['evidence'] if key in scopes else content
        if not single_request and key in scopes and count(snapshot, make_unit(snapshot, 'primary', evidence, core=core), config, profile) > limit:
            original['gaps'].append({'path': scopes[key]['path'],
                'reason': '单个语法范围及保护上下文超出输入额度；变更仍拆分记录，但跨片段依赖未完整校验，禁止正式发布'})

        def split(items, owned):
            unit = make_unit(snapshot, 'primary', items, core=owned)
            if count(snapshot, unit, config, profile) <= limit:
                parts.append(unit)
                return
            if len(items) <= 1:
                raise token_budget.failure('DIFF_UNSPLITTABLE', '单行或元信息超过完整请求额度，未截断')
            mid = len(items) // 2
            for lo, hi in ((0, mid), (mid, len(items))):
                ids = {r['id'] for r in items[lo:hi]}
                selected = [i for i in owned if i in ids]
                if selected:
                    # 相邻上下文随本次分割保留，核心归属仍严格唯一。
                    split(items[lo:hi], selected)
        if single_request:
            parts.append(make_unit(snapshot, 'primary', evidence, core=core))
        else:
            split(evidence, core)
    primary, pending = [], None
    if single_request and expected:
        # 一次主审同时看到全部变更和原关系复核所需上下文；超额则停止，不裁剪保护条件。
        evidence = [r for unit in parts for r in unit['evidence']] + rows
        evidence.extend(r for key in sorted(k for k in affected_scopes if isinstance(k, str))
                        if key in scopes for r in scopes[key]['evidence'])
        unit = make_unit(snapshot, 'primary', evidence, core=expected)
        tokens = count(snapshot, unit, config, profile)
        if tokens > limit:
            raise token_budget.input_failure(tokens, limit, 'PR 完整变更及相关上下文超过一次审查输入额度；拆分未启用，未发送模型')
        primary = [unit]
    for unit in ([] if single_request else parts):
        combined = make_unit(snapshot, 'primary', (pending['evidence'] if pending else []) + unit['evidence'],
                             core=(pending['core'] if pending else []) + unit['core'])
        if pending and (len(combined['core']) > PRIMARY_WORK_LINES or count(snapshot, combined, config, profile) > limit):
            primary.append(pending)
            pending = unit
        else:
            pending = combined
    if pending:
        primary.append(pending)
    # 每个关系均记录在哪个主审或关联任务中检查，不用“没有发现意见”决定是否跳过。
    scope_units = {sid: {u['id'] for u in primary if {r['id'] for r in s['evidence']} <=
                        {r['id'] for r in u['evidence']}} for sid, s in scopes.items()}
    cross, covered = [], []
    for relation in relations:
        left, right = relation['left'], relation['right']
        # 没有任何变更的两函数不形成当前 PR 任务。
        if left not in affected_scopes and right not in affected_scopes:
            continue
        common = scope_units[left] & scope_units[right]
        detail = dict(relation, code=[{k: scopes[i][k] for k in ('id', 'path', 'side', 'name', 'start', 'end')}
                                      for i in (left, right)])
        if common:
            covered.append(dict(detail, covered_by=sorted(common)))
        else:
            cross.append(detail)
    associations = []

    def relation_rows(items):
        return list({r['id']: r for item in items for key in (item['left'], item['right'])
                     for r in scopes[key]['evidence']}.values())

    def install(items):
        unit = make_unit(snapshot, 'association', relation_rows(items), connections=items)
        if count(snapshot, unit, config, profile) <= limit:
            associations.append(unit)
        elif len(items) > 1:
            mid = len(items) // 2
            install(items[:mid])
            install(items[mid:])
        else:
            original['gaps'].append({'path': items[0]['code'][0]['path'],
                                     'reason': '跨函数关系及保护上下文超过输入额度，未丢弃关系'})

    # 字节权重只用于装箱提案，每个最终请求仍完整分词校验。避免每追加一条关系
    # 就把数万 Token 的相同源码重新分词；超预算时递归拆分关系，不裁剪证据。
    calibration = primary[0] if primary else make_unit(snapshot, 'primary', [])
    encoded = json.dumps(batch_plan.messages(snapshot, calibration), ensure_ascii=False, separators=(',', ':')).encode()
    ratio = count(snapshot, calibration, config, profile) / max(1, len(encoded))
    weights, remaining = {}, []
    scope_work = {key: max(1, len(scope.get('body_ids', scope['evidence']))) for key, scope in scopes.items()}
    for relation in cross:
        item = make_unit(snapshot, 'association', relation_rows([relation]))
        local, _ = (batch_locations.payload(item) if item['reference_protocol'] == batch_locations.PROTOCOL else batch_references.payload(item))
        for original_row, rendered in zip(item['evidence'], local):
            weights.setdefault(original_row['id'], len(json.dumps(rendered, ensure_ascii=False, separators=(',', ':')).encode()) * ratio)
        remaining.append((relation, {r['id'] for r in item['evidence']},
                          len(json.dumps(relation, ensure_ascii=False).encode()) * ratio))
    while remaining:
        pending_relations, pending_ids, pending_functions, weight = [], set(), set(), 512
        while remaining:
            function_sets = [{key for key in (item[0]['left'], item[0]['right'])
                              if scopes[key].get('scope_type', 'function') == 'function'} for item in remaining]
            pairs = {frozenset(functions) for functions in function_sets if len(functions) == 2}
            def packing_score(n):
                combined = pending_functions | function_sets[n]
                if pending_relations and combined - pending_functions and sum(scope_work[key] for key in combined) > ASSOCIATION_WORK_LINES:
                    return (1, 0, n)
                closed = sum(frozenset(pair) in pairs for pair in combinations(combined, 2))
                return (-closed, -len(remaining[n][1] & pending_ids), n)
            choices = sorted(range(len(remaining)), key=packing_score)
            selected = None
            for n in choices:
                relation, ids, overhead = remaining[n]
                functions = {key for key in (relation['left'], relation['right'])
                             if scopes[key].get('scope_type', 'function') == 'function'}
                if pending_relations and functions - pending_functions and sum(scope_work[key] for key in pending_functions | functions) > ASSOCIATION_WORK_LINES:
                    continue
                added = sum(weights[i] for i in ids - pending_ids) + overhead
                if not pending_relations or weight + added <= limit * .94:
                    selected = n
                    break
            if selected is None:
                break
            relation, ids, overhead = remaining.pop(selected)
            weight += sum(weights[i] for i in ids - pending_ids) + overhead
            pending_ids.update(ids)
            pending_functions.update(key for key in (relation['left'], relation['right'])
                                     if scopes[key].get('scope_type', 'function') == 'function')
            pending_relations.append(relation)
        install(pending_relations)
    units = primary + associations
    for unit in units:
        unit['input_tokens'] = count(snapshot, unit, config, profile)
    actual = [i for u in primary for i in u['core']]
    if Counter(actual) != Counter(expected) or len(actual) != len(set(actual)):
        raise token_budget.failure('DIFF_COVERAGE', '第三版主审覆盖证明失败')
    plan = dict(original, version=VERSION, units=units, primary=len(primary), association=len(associations),
                base_calls=len(units) + original['history'], max_calls=4 * len(units) + original['history'],
                input_tokens=sum(u['input_tokens'] for u in units), prompt_hash=digest(batch_plan.LOCAL_SYSTEM + SYSTEM),
                relations=covered + cross, limitations=limitations, scopes=scopes,
                affected_scopes=sorted(key for key in affected_scopes if isinstance(key, str)),
                refutation_planned=False)
    plan['reference_protocol'] = snapshot.get('batch_reference_protocol', batch_references.PROTOCOL)
    if single_request:
        if associations:
            raise token_budget.failure('DIFF_COVERAGE', '一次主审未包含全部关联上下文，未发送模型')
        plan.update(review_mode='single', **line_counts)
        plan['max_net_added_lines' if net_limit else 'max_changed_lines'] = (
            MAX_SINGLE_NET_ADDED_LINES if net_limit else MAX_SINGLE_CHANGED_LINES)
    if units:
        plan['prompt_hash'] = digest(batch_plan.messages(snapshot, units[0])[0]['content'])
        plan['correction_prompt_hash'] = digest(batch_plan.messages(snapshot, units[0], correction={'findings': []})[0]['content'])
    plan.pop('hash', None)
    plan['hash'] = digest(plan)
    return plan


def candidates(plan):
    """只合并完整语义字段及引用均一致的报告，保留全部来源。"""
    found = {}
    for unit in plan['units']:
        if unit['kind'] not in ('primary', 'association') or unit['state'] == 'replaced':
            continue
        if unit['state'] != 'done':
            raise token_budget.failure('BATCH_OUTPUT_INCOMPLETE', '主审与关联尚未全部完成')
        for item in unit['result']['findings']:
            key = digest({k: v for k, v in item.items() if k != 'id'})
            if key not in found:
                found[key] = dict(item, id='C' + str(len(found) + 1), origins=[], source_units=[])
            found[key]['origins'].append(item['id'])
            if unit['id'] not in found[key]['source_units']:
                found[key]['source_units'].append(unit['id'])
    return list(found.values())


def refutations(snapshot, plan, config):
    value, records = plan['value'], {u['id']: u['value'] for u in plan['units']}
    if value.get('review_mode') == 'single':
        selected = [c for c in candidates(plan) if c['severity'] in ('P0', 'P1')]
        if not selected:
            return []
        evidence = [r for c in selected for key in c['source_units'] for r in records[key]['evidence']]
        unit = make_unit(dict(snapshot, review_mode='single'), 'refutation', evidence, candidates=selected)
        unit['input_tokens'] = count(snapshot, unit, config, value['profile'])
        if unit['input_tokens'] > token_budget.capacity(config) - 512:
            raise token_budget.input_failure(unit['input_tokens'], token_budget.capacity(config) - 512,
                                            '全部候选和保护上下文超过一次反证额度；拆分未启用，未删减候选')
        return [unit]
    groups = []
    for candidate in candidates(plan):
        if candidate['severity'] not in ('P0', 'P1'):
            continue
        source_rows = {r['id']: r for key in candidate['source_units'] for r in records[key]['evidence']}
        # 来源单元已包含完整函数、契约和保护上下文；关联单元也包含连接两端。
        # 反证保留实际见过的全部证据，不再沿共享状态图无界扩展到其他批次。
        evidence = list(source_rows.values())
        item = make_unit(snapshot, 'refutation', evidence, candidates=[candidate])
        if count(snapshot, item, config, value['profile']) > token_budget.capacity(config) - 512:
            raise token_budget.failure('MODEL_INPUT_BUDGET', '单个候选的证据和保护条件超出反证额度，未删减保护')
        merged = False
        for n, group in enumerate(groups):
            if len(group['candidates']) >= max(1, (config.get('model_max_output_tokens') or 8192) // 1024):
                continue
            combined = make_unit(snapshot, 'refutation', group['evidence'] + item['evidence'],
                                 candidates=group['candidates'] + [candidate])
            if count(snapshot, combined, config, value['profile']) <= token_budget.capacity(config) - 512:
                groups[n] = combined
                merged = True
                break
        if not merged:
            groups.append(item)
    for unit in groups:
        unit['input_tokens'] = count(snapshot, unit, config, value['profile'])
    expected = {c['id'] for c in candidates(plan) if c['severity'] in ('P0', 'P1')}
    actual = [c['id'] for u in groups for c in u['candidates']]
    if set(actual) != expected or len(actual) != len(expected):
        raise token_budget.failure('BATCH_OUTPUT_INVALID', '分组反证候选覆盖校验失败')
    return groups

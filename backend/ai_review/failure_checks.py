"""有界、只读的失败检查；不调用模型、不修改结果、不猜测源码证据。"""
import contextlib
import hashlib
import json
import math
import re
import sqlite3
import time
from datetime import datetime, timezone

from .common import ReviewError

SUPPORTED = {'BATCH_OUTPUT_INVALID', 'BATCH_OUTPUT_INCOMPLETE', 'MODEL_INPUT_BUDGET'}
MAX_BYTES = 8 * 1024 * 1024
MAX_UNITS = 64
REFERENCE = re.compile(r'(?:E[0-9a-f]{1,16}|R[1-9][0-9]*|F[1-9][0-9]*H[1-9][0-9]*L[1-9][0-9]*)\Z')


class CheckLimit(Exception):
    pass


def supported(diagnostic):
    return isinstance(diagnostic, dict) and diagnostic.get('error_code') in SUPPORTED


def analyze(store, job, diagnostic):
    """绑定具体任务和会话；报告失败不能覆盖业务的最初失败。"""
    code = diagnostic.get('error_code') if isinstance(diagnostic, dict) else None
    report = {'version': 1, 'status': 'complete', 'checks': [],
              'generated_at': datetime.now(timezone.utc).isoformat(),
              'summary': '以下为保存记录的确定性检查，未执行重试或修改审查结论。'}
    if code not in SUPPORTED:
        return dict(report, status='unsupported', summary='此错误尚无自动检查器，请保留原始诊断。')
    deadline, consumed = time.monotonic() + 2, 0

    def check(key, state, title, detail):
        if len(report['checks']) < 12:
            report['checks'].append({'code': key, 'state': state, 'title': title, 'detail': detail})

    def decode(value):
        nonlocal consumed
        if not isinstance(value, str):
            raise ValueError()
        consumed += len(value.encode())
        if consumed > MAX_BYTES or time.monotonic() > deadline:
            raise CheckLimit()
        return json.loads(value)

    def number(config, key, default):
        value = config.get(key, default)
        if type(value) is not int or value < 0 or value > 2**31:
            raise ValueError()
        return value

    try:
        path = store.path.resolve().as_uri() + '?mode=ro'
        with contextlib.closing(sqlite3.connect(path, uri=True, timeout=.1)) as db:
            db.execute('PRAGMA query_only=ON')
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            db.execute('BEGIN')
            config_row = db.execute('SELECT value FROM configuration_history WHERE revision=?',
                                    (job['config_revision'],)).fetchone()
            if code == 'MODEL_INPUT_BUDGET':
                if not config_row:
                    check('frozen_config', 'unknown', '固定配置', '任务原配置已缺失，未使用当前全局设置代替。')
                    report['status'] = 'partial'
                    return report
                config = decode(config_row[0])
                context = number(config, 'model_context_tokens', 262144)
                output = number(config, 'model_max_output_tokens', 0) or 8192
                target = number(config, 'batch_input_tokens', 32768)
                capacity = min(target, context - output - max(2048, math.ceil(context * .05)))
                primary = capacity - 512 if job.get('batch_flow_version', 2) >= 3 else (capacity - output - 512) // 2
                check('input_allocation', 'confirmed' if capacity < 1024 or primary < 1024 else 'unknown',
                      '固定输入与输出额度',
                      f'上下文 {context}，单批目标 {target}，输出预留 {output}，完整请求可用 {capacity}，主审单元额度 {primary}。')
                check('input_count', 'unknown', '完整消息计数',
                      '此检查未重新调用计数器；额度为正不能证明实际请求一定能装入，需结合原失败计数。')
                return report

            session = diagnostic.get('session_id') or job.get('session_id')
            row = db.execute('''SELECT a.unit,length(a.raw),length(u.value)
                FROM review_batch_attempts a JOIN review_batch_units u ON u.job=a.job AND u.id=a.unit
                WHERE a.job=? AND a.session_id=?''', (job['id'], session)).fetchone()
            if not row:
                report['status'] = 'partial'
                check('session_binding', 'unknown', '失败会话', '未找到本任务中该会话的原始结果与单元，未借用其他会话。')
                return report
            if sum(size or 0 for size in row[1:]) > MAX_BYTES:
                raise CheckLimit()
            payload = db.execute('''SELECT a.raw,u.value FROM review_batch_attempts a
                JOIN review_batch_units u ON u.job=a.job AND u.id=a.unit
                WHERE a.job=? AND a.session_id=?''', (job['id'], session)).fetchone()
            raw, unit = decode(payload[0]), decode(payload[1])
            if unit.get('id') != row[0]:
                check('session_binding', 'confirmed', '单元记录', '单元内部编号与保存编号不一致，不能继续推断模型错误。')
                return report
            report.update(session_id=session, unit=row[0],
                          evidence_sha256=hashlib.sha256(payload[1].encode()).hexdigest())
            completion = raw.get('_review_completion') or {}
            finish = completion.get('finish_reason')
            reason = finish if finish in {'stop', 'length', 'tool_calls', 'content_filter', 'error'} else '未记录或未知'
            complete = completion.get('complete') is True
            check('completion', 'confirmed' if not complete or finish == 'length' else 'clear',
                  '输出完成状态', f'完整结束标记：{"是" if complete else "否"}；结束原因：{reason}。')
            if code == 'BATCH_OUTPUT_INCOMPLETE':
                if config_row:
                    config = decode(config_row[0])
                    maximum = number(config, 'model_max_output_tokens', 0) or 8192
                    thinking = '开启' if config.get('thinking_enabled') else '关闭'
                    check('output_configuration', 'clear', '任务原输出配置',
                          f'输出上限 {maximum}；思考请求{thinking}。这是任务保存配置，不代表服务实际接受额度。')
                check('output_budget', 'confirmed' if finish == 'length' else 'unknown', '截断依据',
                      '服务返回 length，存在输出额度耗尽证据；不能把已返回部分当作完整报告。' if finish == 'length' else
                      '未记录 length，不能只根据“未完整结束”断定输出额度太小，也可能是传输或执行中断。')
                return report

            from .batch_plan import _decode_output, _evidence_aliases, parse_report, parse_refutation
            try:
                parsed = _decode_output(raw)
            except ReviewError as error:
                kind = getattr(error, 'diagnostic', {}).get('reason')
                descriptions = {'json_syntax': '当前解析器仍无法解析为 JSON。',
                                'batch_thinking_unclosed': '开头思考标签未闭合。',
                                'batch_fence_invalid': '外围代码围栏不完整或不符合约定。',
                                'empty_result': '移除完整外围包装后没有正文。',
                                'result_not_object': 'JSON 顶层不是对象。',
                                'batch_result_not_text': '结果正文不是文本。'}
                check('json_format', 'confirmed', '结果格式', descriptions.get(kind, '当前解析器拒绝该输出，未提取任意中间片段。'))
                return report
            check('json_format', 'clear', '结果格式', '当前解析器能够读取完整 JSON 对象。')
            from .batch_locations import PROTOCOL as LOCATION_PROTOCOL
            if unit.get('reference_protocol') == LOCATION_PROTOCOL:
                check('provided_evidence', 'clear', '本单元源码范围',
                      f"共 {len(unit['evidence'])} 条固定源码证据；位置只在本单元解析，不查找最近行。")
                try:
                    if unit.get('candidates') is not None:
                        parse_refutation(raw, unit, unit['candidates'])
                    else:
                        parse_report(raw, {}, unit)
                except ReviewError as error:
                    reason = getattr(error, 'diagnostic', {}).get('reason')
                    labels = {'source_location_unknown': '位置不在本单元证据中。',
                              'source_location_ambiguous': '同一位置对应多条不同证据，不能猜选。',
                              'source_location_uncommentable': '位置可读取但不支持作为行内评论位置。',
                              'source_candidate_coverage': '反证没有恰好覆盖所有候选。'}
                    check('source_locations', 'confirmed', '源码位置与报告复验',
                          labels.get(reason, '正文、字段、完成状态或位置格式不符合约定，原错误保留。'))
                else:
                    check('source_locations', 'clear', '源码位置与报告复验', '当前解析器接受保存结果；不会因此自动改写原任务状态。')
                return report
            from .batch_references import PROTOCOL, mapping
            local = mapping(unit) if unit.get('reference_protocol') == PROTOCOL else {}
            aliases = {key: {row['id']} for key, row in local.items()} if local else _evidence_aliases(unit['evidence'])
            supplied = [entry.get('id') for entry in unit['evidence']]
            ends = [value for value in supplied[:1] + supplied[-1:]
                    if isinstance(value, str) and len(value) <= 180 and REFERENCE.fullmatch(value)]
            check('provided_evidence', 'clear', '本单元提供的证据',
                  f'共 {len(supplied)} 条；按提供顺序的首、末编号：' + ('、'.join(ends) or '无有效编号') + '。')
            items = parsed.get('checks' if unit.get('candidates') is not None else 'findings')
            unknown, ambiguous, aliases_used = set(), set(), 0
            malformed, bad_anchor = 0, 0
            if unit.get('candidates') is None:
                fields_ok = isinstance(parsed.get('summary'), str) and isinstance(parsed.get('limitations'), list) and isinstance(items, list)
                check('report_fields', 'clear' if fields_ok else 'confirmed', '报告顶层字段',
                      'summary、limitations、findings 的字段类型符合约定。' if fields_ok else
                      'summary、limitations、findings 存在缺失或类型错误。')
            if isinstance(items, list):
                if len(items) > 100:
                    raise CheckLimit()
                for item in items:
                    if time.monotonic() > deadline: raise CheckLimit()
                    refs = item.get('evidence_ids') if isinstance(item, dict) else None
                    if not isinstance(refs, list):
                        malformed += 1
                        continue
                    if not refs or any(not isinstance(ref, str) for ref in refs): malformed += 1
                    if unit.get('candidates') is None:
                        required = ('title', 'trigger', 'explanation', 'impact', 'recommendation')
                        if item.get('severity') not in ('P0', 'P1', 'P2', 'P3') or any(not isinstance(item.get(key), str) or not item[key].strip() for key in required):
                            malformed += 1
                        anchor = item.get('anchor_id')
                        if local and isinstance(anchor, str):
                            anchor = local.get(anchor, {}).get('anchor_id', '__invalid__')
                        referenced = {ident for ref in refs if isinstance(ref, str) for ident in aliases.get(ref, ())}
                        if 'anchor_id' not in item or (anchor is not None and (not isinstance(anchor, str) or not any(e.get('anchor_id') == anchor and e['id'] in referenced for e in unit['evidence']))):
                            bad_anchor += 1
                    if len(refs) > 1000:
                        raise CheckLimit()
                    for ref in refs:
                        if not isinstance(ref, str):
                            continue
                        matches = aliases.get(ref, set())
                        if not matches: unknown.add(ref)
                        elif len(matches) != 1: ambiguous.add(ref)
                        elif ref not in matches: aliases_used += 1
            check('finding_fields', 'unknown' if not isinstance(items, list) else 'confirmed' if malformed else 'clear', '意见字段',
                  f'发现 {malformed} 项字段或证据列表类型错误。' if isinstance(items, list) else
                  '意见列表缺失或类型无效，不能逐条检查字段。')
            if bad_anchor:
                check('anchor_binding', 'confirmed', '定位与引用', f'{bad_anchor} 条意见的定位编号缺失、越界或未被引用证据支持。')
            if unknown:
                safe = sorted(ref for ref in unknown if len(ref) <= 180 and REFERENCE.fullmatch(ref))
                text = '、'.join(safe[:8]) or '含非标准编号，未显示任意模型文本'
                check('current_unit_reference', 'confirmed', '当前单元证据',
                      f'{len(unknown)} 个不同引用不在当前单元：{text}。')
                found, scanned, matched_units, limited = set(), 0, 0, False
                for ident, size in db.execute('SELECT id,length(value) FROM review_batch_units WHERE job=? AND id<>? ORDER BY position', (job['id'], row[0])):
                    if scanned >= MAX_UNITS or consumed + (size or 0) > MAX_BYTES or time.monotonic() > deadline:
                        limited = True; break
                    other = decode(db.execute('SELECT value FROM review_batch_units WHERE job=? AND id=?', (job['id'], ident)).fetchone()[0])
                    matched = (unknown.intersection(mapping(other))
                               if other.get('reference_protocol') == PROTOCOL else set())
                    for index, evidence in enumerate(other.get('evidence', [])):
                        if index % 256 == 0 and time.monotonic() > deadline: raise CheckLimit()
                        matched.update(unknown.intersection((evidence.get('id'), evidence.get('anchor_id'))))
                    found.update(matched)
                    matched_units += bool(matched)
                    scanned += 1
                report['status'] = 'partial' if limited else report['status']
                check('other_unit_reference', 'confirmed' if found else 'unknown', '其他单元对照',
                      f'检查同任务其他单元 {scanned} 个，其中 {len(found)} 个引用可找到，匹配 {matched_units} 个其他单元；' +
                      ('达到检查上限，未检查范围不作结论。' if limited else '仅证明编号出现的位置，不能据此断言模型为何使用它。'))
            elif ambiguous:
                check('current_unit_reference', 'confirmed', '当前单元证据', f'{len(ambiguous)} 个引用在本单元存在多个对应位置，不能猜选。')
            else:
                label = '编号映射' if local else '定位别名'
                check('current_unit_reference', 'clear', '当前单元证据', f'未发现字符串引用越界；识别了 {aliases_used} 个本单元{label}。')
            try:
                if unit.get('candidates') is not None: parse_refutation(raw, unit, unit['candidates'])
                else: parse_report(raw, {}, unit)
            except ReviewError:
                check('current_parser', 'confirmed', '当前版本复验', '当前解析器仍拒绝该结果；原失败保持，不做自动修改。')
            else:
                check('current_parser', 'clear', '当前版本复验', '保存结果在当前解析器下通过；可能涉及版本差异，不能据此自动恢复为成功。')
            return report
    except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, CheckLimit):
        report['status'] = 'partial'
        check('available_records', 'unknown', '记录与检查上限', '原始记录缺失、格式异常或达到只读检查上限；原错误保持不变。')
        return report


def attach(store, job, diagnostic):
    if not supported(diagnostic):
        return diagnostic
    try:
        report = analyze(store, job, diagnostic)
    except Exception:
        report = {'version': 1, 'status': 'unavailable', 'summary': '自动检查暂不可用，原始错误已保留。', 'checks': []}
    return dict(diagnostic, automatic_checks=report)

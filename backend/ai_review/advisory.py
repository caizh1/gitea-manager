"""辅助意见的容错协议：文本质量与源码定位分开处理。"""
import hashlib
from .common import ReviewError, encode
from .evidence import root_location, source_evidence, TEXT_FIELDS


def normalize(row):
    if not isinstance(row, dict):
        raise ReviewError('没有可用的问题对象')
    if any(not isinstance(row.get(k), str) or not row[k].strip() or len(row[k]) > 3000
           for k in ('title', 'explanation')):
        raise ReviewError('建议缺少具体问题或自然语言原因')
    value = {k: row.get(k, '') if isinstance(row.get(k, ''), str) else '' for k in TEXT_FIELDS}
    if any(len(v) > 3000 for v in value.values()):
        raise ReviewError('建议文本超过长度限制')
    if value['severity'] not in ('P1', 'P2'):
        value['severity'] = 'P2'
    value['trace'] = [s for s in row.get('trace', []) if isinstance(s, str) and len(s) <= 1200][:8] if isinstance(row.get('trace'), list) else []
    value['support'] = [s for s in row.get('support', []) if isinstance(s, dict) and
                        isinstance(s.get('source_id'), str) and isinstance(s.get('quote'), str)
                        and 0 < len(s['quote']) <= 3000 and len(s['source_id']) <= 3000][:8] if isinstance(row.get('support'), list) else []
    return value


def parse(value):
    rows = value.get('findings') if isinstance(value, dict) else None
    if not isinstance(rows, list) or len(rows) > 6:
        raise ReviewError('模型没有返回可识别的建议列表')
    result, diagnostics = [], []
    for index, row in enumerate(rows):
        try:
            item = normalize(row)
            item['candidate_id'] = hashlib.sha256(encode(item).encode()).hexdigest()[:24]
            result.append(item)
        except ReviewError as exc:
            diagnostics.append({'index': index, 'reason': str(exc)})
    if rows and not result:
        raise ReviewError('本单元所有候选均缺少可用问题和原因')
    return result, diagnostics


def confirm(rows, value, context):
    checks = value.get('checks', []) if isinstance(value, dict) else []
    if not isinstance(checks, list):
        checks = []
    output, rejected = [], []
    for original in rows:
        matched = [c for c in checks if isinstance(c, dict) and c.get('candidate_id') == original['candidate_id']]
        check = matched[0] if len(matched) == 1 else {}
        verdict = check.get('verdict')
        reason = check.get('reason')
        reason = reason[:3000] if isinstance(reason, str) and reason.strip() else '反证结果不完整，需开发者核对'
        if verdict == 'rejected' and isinstance(check.get('reason'), str) and check['reason'].strip():
            rejected.append({'candidate_id': original['candidate_id'], 'verdict': 'rejected', 'reason': reason})
            continue
        complete = False
        try:
            final = normalize(check.get('finding'))
            complete = verdict in ('confirmed', 'uncertain')
        except ReviewError:
            final = normalize(original)
        final.update(candidate_id=original['candidate_id'], location_verified=complete and check.get('location_verified') is True,
                     verification=reason, assessment='confirmed' if complete and verdict == 'confirmed' else 'uncertain')
        proof, warning = source_evidence(final, context)
        # 引用列表仅为诊断，不能单独否决自然语言意见或真实锚点。
        anchor, fallback, root = root_location(final, original, context, require_support=False)
        output.append(dict(final, source_evidence=proof or [], evidence_warning=warning, location_evidence=root,
                           anchor=anchor, fallback_reason=fallback, quality_version=3))
    return output, rejected

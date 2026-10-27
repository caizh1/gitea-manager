"""模型只见本单元的一套编号；原始证据与评论定位由确定性映射恢复。"""
from .common import digest

PROTOCOL = 'local-evidence-v2'
RETRY_REASONS = frozenset({'batch_evidence_unknown', 'batch_anchor_unknown', 'batch_anchor_mismatch'})


def mapping(unit):
    rows = unit['evidence']
    scope = digest([row['id'] for row in rows])
    result = {'E' + digest([scope, row['id']])[:10]: row for row in rows}
    if len(result) != len(rows) or len({r['id'] for r in rows}) != len(rows):
        raise ValueError('本单元证据编号重复，不能建立唯一映射')
    return result


def payload(unit, candidates=None):
    """不向模型暴露全局 F/R 编号，也不更改持久化的证据或候选。"""
    local = mapping(unit)
    reverse = {row['id']: ident for ident, row in local.items()}
    anchors = {row['anchor_id']: ident for ident, row in local.items() if row.get('anchor_id')}
    rows = [dict({key: row.get(key) for key in ('path', 'hunk', 'kind', 'old', 'new', 'text')},
                 id=ident, anchor_id=ident if row.get('anchor_id') else None)
            for ident, row in local.items()]
    if unit.get('flow_version') == 'diff-batches-v3':
        for row, original in zip(rows, local.values()):
            if original.get('version'):
                row['version'] = original['version']
    converted = None
    if candidates is not None:
        converted = [dict({k: v for k, v in f.items() if k not in ('origins', 'source_units')}, evidence_ids=[reverse[ref] for ref in f['evidence_ids']],
                          anchor_id=anchors[f['anchor_id']] if f.get('anchor_id') else None)
                     for f in candidates]
    return rows, converted


def unchanged_report(original, corrected, field=None, locations=False):
    """引用纠正不能以删掉意见、降级或改写结论来制造成功。"""
    def content(value):
        result = dict(value)
        target = field or ('findings' if 'findings' in result else 'checks')
        references = ('path', 'side', 'line', 'related_locations') if locations else ('evidence_ids', 'anchor_id')
        result[target] = [{key: item for key, item in row.items() if key not in references}
                          for row in result[target]]
        return result
    try:
        return content(original) == content(corrected)
    except (KeyError, TypeError, AttributeError):
        return False

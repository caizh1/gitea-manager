"""校验模型证据的来源与根因语句成员关系；不把结构校验宣称为语义证明。"""
from .common import ReviewError
from .diff import locate
from .context import operations

TEXT_FIELDS = {'anchor_id', 'quote', 'severity', 'title', 'trigger', 'explanation', 'impact', 'recommendation',
               'operation_id', 'before_behavior', 'after_behavior'}
FIELDS = TEXT_FIELDS | {'trace', 'support'}


def validate_shape(row):
    if not isinstance(row, dict) or set(row) != FIELDS:
        raise ReviewError('模型候选缺少版本对比、状态序列或源码证据字段')
    if any(not isinstance(row[k], str) or len(row[k]) > 3000 for k in TEXT_FIELDS):
        raise ReviewError('模型候选文本字段无效')
    if row['severity'] not in ('P1', 'P2') or any(not row[k].strip() for k in TEXT_FIELDS-{'anchor_id','quote','operation_id'}):
        raise ReviewError('模型候选缺少可核对的行为说明')
    if not isinstance(row['trace'], list) or not 2 <= len(row['trace']) <= 8 or any(not isinstance(s,str) or not 1 <= len(s) <= 1200 for s in row['trace']):
        raise ReviewError('模型候选状态序列格式无效')
    if not isinstance(row['support'], list) or not 1 <= len(row['support']) <= 8:
        raise ReviewError('模型候选源码证据列表无效')
    for support in row['support']:
        if not isinstance(support, dict) or set(support) != {'source_id', 'quote'} or any(not isinstance(support[k],str) or not 1 <= len(support[k]) <= 3000 for k in support):
            raise ReviewError('模型候选源码引用格式无效')


def source_evidence(row, item):
    references=[s for symbol in item.get('changed_symbols',[]) for s in symbol.get('references',[])]
    sources = {s['source_id']: s for s in [item['before'], item['after'], *item['context'], *references]}
    verified = []
    for ref in row['support']:
        s = sources.get(ref['source_id'])
        if not s or not ref['quote'].strip() or s['code'].count(ref['quote']) != 1:
            return None, '源码证据不存在、引用不一致或片段存在歧义，未作为确定缺陷发布'
        verified.append(dict(ref, path=s['path'], version=s['version'], role=s['role']))
    required = {v for v in ('before','after') if item[v]['code'].strip()}
    supplied = {v['version'] for v in verified}
    if not required <= supplied:
        return None, '未同时提供修改前后的源码证据，无法核对是否由本次修改引入'
    return verified, ''


def root_location(row, original, item, require_support=True):
    anchors = {a['id']: a for a in item['anchors']}
    anchor, reason = locate(row, anchors, item['snapshot_id'])
    if not anchor:
        return None, reason, None
    root = next((o for o in item['operations'] if o['id'] == row['operation_id']), None)
    if not root or anchor['id'] not in root['anchor_ids'] or root['side'] != anchor['side']:
        return None, '根因语法操作与所选变更行不一致，已降级为普通意见', None
    if original['operation_id'] != row['operation_id'] or original['anchor_id'] != row['anchor_id']:
        return None, '两次判断对根因位置不一致，未猜测新的行内位置', None
    if require_support and not any(root['quote'] in r['quote'] for r in row['support']
               if r['source_id'] == item['after' if anchor['side']=='new' else 'before']['source_id']):
        return None, '根因操作未被该侧源码证据直接引用，已降级为普通意见', None
    # 多行表达式仅在该语法操作内恰有一条可评论变更行时可靠。
    if root['start'] != root['end'] and len(root['anchor_ids']) != 1:
        return None, '根因操作跨越多个变更行，无法唯一确认行内位置', None
    return anchor, '', root


def revalidate_location(finding, snapshot):
    """发布时从不可变文件重新生成操作，不能只信任持久化的模型确认标记。"""
    anchor = finding.get('anchor')
    file = next((f for f in snapshot['files'] if f['path'] == finding.get('path')), None)
    proof = finding.get('location_evidence')
    if not anchor or not file or not isinstance(proof, dict):
        return False
    if proof.get('kind') == 'native_snapshot':
        import hashlib
        side = proof.get('side')
        if side not in ('old', 'new') or proof != {
                'kind': 'native_snapshot', 'snapshot_id': snapshot['id'], 'path': file['path'],
                'side': side, 'line': anchor['line'], 'quote': anchor['quote'],
                'source_sha256': hashlib.sha256(file[side].encode()).hexdigest()}:
            return False
        from .diff import file_lines, quote_text
        lines = file_lines(file[side].encode())
        return (anchor == snapshot['anchors'].get(anchor['id']) and anchor['line'] <= len(lines)
                and quote_text(lines[anchor['line']-1]) == anchor['quote'])
    # 使用全部函数锚点之外的行只影响成员列表，不影响节点及版本身份；生成时按该函数范围过滤。
    own = [snapshot['anchors'][aid] for aid in file['anchors']
           if snapshot['anchors'][aid]['id'] in proof.get('anchor_ids', [])]
    roots = operations(snapshot, file, own)
    return any(o == proof and anchor['id'] in o['anchor_ids'] for o in roots)

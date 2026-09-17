"""用只读完整性标记核对原生时间线；空页不能作为读取结束依据。"""
from .rules import ApprovalError, positive

TYPES = {22: 'review', 25: 'change_target_branch', 29: 'pull_push'}


def validate_reference(value, repo_id, number, pr_id):
    if not isinstance(value, dict):
        raise ApprovalError('时间线完整性标记格式错误')
    if any(type(value.get(key)) is not int or value[key] != expected
           for key, expected in (('repo_id', repo_id), ('number', number), ('pr_id', pr_id))):
        raise ApprovalError('时间线完整性标记的仓库或 PR 身份不符')
    count, maximum = value.get('count'), value.get('max_id')
    if type(count) is not int or not 0 <= count <= 5000 or type(maximum) is not int or maximum < 0:
        raise ApprovalError('时间线规模超出已验证读取预算')
    events = value.get('events')
    if not isinstance(events, list) or len(events) > count or bool(count) != bool(maximum) or count > maximum:
        raise ApprovalError('时间线完整性标记的计数不一致')
    ids, ordinals = set(), set()
    for event in events:
        if not isinstance(event, dict) or type(event.get('type')) is not int or event['type'] not in TYPES:
            raise ApprovalError('时间线完整性标记包含未知事件')
        rid = positive(event.get('id'), '时间线事件 ID')
        ordinal = positive(event.get('ordinal'), '时间线原始位置')
        if rid in ids or ordinal in ordinals or rid > maximum or ordinal > count:
            raise ApprovalError('时间线完整性标记重复或越界')
        if type(event.get('review_id')) is not int or event['review_id'] < 0:
            raise ApprovalError('时间线审批关联不完整')
        if event['type'] == 22 and event['review_id'] <= 0:
            raise ApprovalError('正式审批事件缺少审批 ID')
        ids.add(rid); ordinals.add(ordinal)
    return {key: value[key] for key in ('repo_id','number','pr_id','count','max_id')} | {
        'events': [{key:e[key] for key in ('id','type','review_id','ordinal')} for e in sorted(events,key=lambda e:e['ordinal'])]}


def read_complete(reader, path, snapshot, *, repo_id, number, pr_id, reviews, page_size=50, with_reference=False):
    """标记由受信只读提供者生成；本函数不能用页面、Webhook 或 API 页头代替标记。"""
    if type(page_size) is not int or not 1 <= page_size <= 50:
        raise ApprovalError('原生时间线分页大小未确认')
    before = validate_reference(snapshot(), repo_id, number, pr_id)
    expected = {e['id']: e for e in before['events']}
    review_ids = {positive(r.get('id'), '原生审批 ID') for r in reviews}
    if any(e['type'] == 22 and e['review_id'] not in review_ids for e in expected.values()):
        raise ApprovalError('审批列表与时间线标记不一致，请重新核对')
    found, seen = {}, set()
    # 原生固定排序位置来自同一只读快照，可直接读取包含必要事件的页；过滤空页不会结束扫描。
    for page in sorted({(e['ordinal']-1)//page_size+1 for e in expected.values()}):
        data = reader.request(path, {'page': page, 'limit': page_size})
        if data is None:
            data = []  # 官方 1.26.4 的完全过滤页返回 null。
        if not isinstance(data, list) or len(data) > page_size or reader.total != len(data):
            raise ApprovalError('原生时间线分页响应格式不一致')
        for event in data:
            if not isinstance(event, dict):
                raise ApprovalError('原生时间线事件格式错误')
            eid = positive(event.get('id'), '原生时间线事件 ID')
            if eid in seen or eid > before['max_id']:
                raise ApprovalError('原生时间线重复或读取期间新增事件')
            seen.add(eid)
            if event.get('type') not in TYPES.values():
                if eid in expected:
                    raise ApprovalError('时间线事件类型与完整性标记不一致')
                continue
            proof = expected.get(eid)
            if not proof or event['type'] != TYPES[proof['type']] or (event['type'] == 'review' and event.get('review_id') != proof['review_id']):
                raise ApprovalError('时间线事件与只读标记不一致')
            found[eid] = event
    if found.keys() != expected.keys():
        raise ApprovalError('原生时间线漏页或漏事件，未使用不完整结果')
    after = validate_reference(snapshot(), repo_id, number, pr_id)
    if before != after:
        raise ApprovalError('读取期间时间线变化，未沿用旧完整性标记')
    events=[found[eid] for eid in sorted(found)]
    return (events,after) if with_reference else events

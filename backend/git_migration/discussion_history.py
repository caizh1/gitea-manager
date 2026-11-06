"""只读来源讨论快照与目标行为差异，不将历史状态变成目标合并限制。"""
from .normalize import identifier
from svn_migration.common import require, digest


def snapshot(thread, parent_kind, parent_id, inline):
    value = {'thread_id': str(thread['id']), 'parent_kind': parent_kind,
             'parent_source_id': str(parent_id), 'notes': []}
    if 'individual_note' in thread:
        value['individual_note'] = thread['individual_note']
    for note in thread['notes']:
        entry = {'source_id': identifier(note['id']), 'kind': 'code_comment' if inline else 'comment'}
        for key in ('resolvable', 'resolved', 'resolved_by', 'resolved_at'):
            if key in note:
                entry[key] = note[key]
        for key in ('resolvable', 'resolved'):
            require(key not in entry or type(entry[key]) is bool, '来源讨论状态格式无效')
        value['notes'].append(entry)
    require(value['notes'], '来源讨论线程为空，无法建立历史关联')
    return value


def differences(inventory):
    rows = []
    if inventory.get('kind') != 'gitlab':
        return rows
    for project in inventory.get('projects', []):
        enabled = any(d['category'] == '项目合并前置条件' and any(
            s.get('only_allow_merge_if_all_discussions_are_resolved') is True for s in d['rows'])
            for d in project['datasets'])
        if enabled:
            threads = [t for d in project['datasets'] if d['category'] == '合并请求讨论' for t in d['rows']]
            rows.append({'project_id': project['project']['id'], 'path': project['project']['path'],
                         'source_require_discussions': True, 'discussion_mode': 'gitea_native',
                         'threads': len(threads), 'unresolved': sum(any(
                             n.get('resolvable') is True and n.get('resolved') is False
                             for n in t.get('notes', [])) for t in threads),
                         'message': '来源要求全部讨论解决后合并；目标保留只读历史状态，采用 Gitea 原生合并规则。'})
    return rows


def compatibility(plan, inventory):
    rows = differences(inventory)
    fingerprint = digest({'inventory': plan['inventory_digest'], 'target': plan['target'],
                          'selection': plan['selection'], 'protocol': 8 if plan.get('preflight_protocol') == 8 else 7, 'differences': rows})
    ack = plan.get('compatibility_acknowledgement')
    return {'differences': rows, 'digest': fingerprint,
            'acknowledged': not rows or bool(ack and ack.get('digest') == fingerprint),
            'acknowledgement': ack if ack and ack.get('digest') == fingerprint else None}

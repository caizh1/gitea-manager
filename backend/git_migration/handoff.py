"""完整核验后的最终群组交接；所有不确定写入保留意图与稳定编号。"""
from urllib.parse import quote
from svn_migration.common import require
from .git_snapshot import remote_refs


def child_group(receiver, parent, segment, key, checkpoint, save):
    groups = checkpoint.setdefault('groups', {})
    path = parent['full_path'] + '/' + segment
    state = groups.setdefault(path, {})
    marker = segment + ' · 迁移 ' + key
    found, after = None, 0
    while True:
        rows = receiver.api('GET', '/governance/groups?parent_id=' + str(parent['id']) + '&after_id=' + str(after))
        require(isinstance(rows, list), '目标群组列表格式不符')
        for row in rows:
            if row['full_path'].casefold() == path.casefold():
                require(found is None, '目标群组路径冲突')
                found = row
        if len(rows) < 100:
            break
        next_id = max(row['id'] for row in rows)
        require(next_id > after, '目标群组分页未前进')
        after = next_id
    if found is None:
        require(not state.get('id'), '已登记的目标群组消失，不能重建替代')
        state['intent'] = marker
        save()
        found = receiver.api('POST', '/governance/groups', {'parent_id': parent['id'], 'path': segment,
                              'name': marker, 'visibility': 2})
    require(state.get('id') == found['id'] or state.get('intent') == marker and found.get('name') == marker,
            '目标已有同名群组，但不属于本迁移任务')
    require(found['full_path'] == path and found.get('parent_id') == parent['id'] and not found.get('archived'),
            '目标子群组身份已变化')
    state['id'] = found['id']
    save()
    return receiver.api('GET', '/governance/groups/' + str(found['id']))


def finish(receiver, selection, project, progress, checkpoint, save):
    require(not progress.get('pending') and not progress.get('gaps'), '仍有未完成的迁移资料，禁止最终交接')
    require(progress.get('code_verified') and progress.get('objects_verified'), '代码或原生对象尚未核验')
    receiver.verify_selected_scope(selection)
    group = receiver.api('GET', '/governance/groups/' + str(selection['id']))
    parts = project['path'].split('/')
    require(len(parts) >= 2 and all(p and p not in ('.', '..') for p in parts), '来源层级路径无效')
    for part in parts[:-1]:
        group = child_group(receiver, group, part, checkpoint['key'], checkpoint, save)
    name = parts[-1]
    final_path = group['full_path'] + '/' + name
    final = progress.setdefault('handoff', {'group_id': group['id'], 'path': final_path, 'name': name})
    require(final == {'group_id': group['id'], 'path': final_path, 'name': name}, '交接目标已变化')
    save()
    existing = receiver.repository(group['full_path'], name, missing=True)
    require(existing is None or existing['id'] == progress['repo_id'], '目标业务路径已占用，不覆盖现有仓库')
    if not progress.get('sealed'):
        receiver.api('POST', '/governance/migration-sessions/' + progress['key'] + '/seal',
                     {'objects': len(progress.get('objects', {})), 'assets': len(progress.get('assets', {}))})
        progress['sealed'] = True
        save()
    staging = progress['repo_name']
    source = receiver.repository(receiver.actor['login'], staging, missing=True)
    moved = receiver.repository(group['full_path'], staging, missing=True)
    repo = existing or moved or source
    require(repo is not None and repo['id'] == progress['repo_id'], '交接仓库无法按稳定编号定位')
    already_unarchived = bool(progress.get('unarchive_intent') and existing and not existing.get('archived'))
    if not already_unarchived:
        if source is not None:
            path = '/repos/' + quote(receiver.actor['login'], safe='') + '/' + quote(staging, safe='')
            receiver.api('PATCH', path, {'archived': True})
        reply = receiver.api('POST', '/governance/migration-sessions/' + progress['key'] + '/handoff',
                             {'owner_id': group['id'], 'name': name})
        require(reply.get('repo_id') == progress['repo_id'] and reply.get('archived') is True,
                '封存交接回执与预期不一致')
    repo = receiver.repository(group['full_path'], name)
    require(repo['id'] == progress['repo_id'] and repo['private'] and not repo.get('has_actions'), '交接后的仓库身份或运行状态不符')
    receiver.verify_selected_scope(selection)
    remote = receiver.remote(group['full_path'], name)
    actual, default = remote_refs(receiver.process.run(['git', 'ls-remote', '--symref', remote], env=receiver.environment(remote)))
    expected = dict(progress['code_refs'])
    if 'HEAD' in progress['snapshot']['refs']:
        expected['HEAD'] = progress['snapshot']['refs']['HEAD']
    require(actual == expected and default == progress['snapshot']['default_ref'], '群组转移后的代码引用不一致')
    from .rules import apply
    apply(receiver, repo, progress.get('rule_plan', []), progress.get('principal_map', {}), progress, save)
    from .packages import verify_published
    verify_published(receiver, repo, progress.get('package_files', []))
    # 先完成所有读回核对，最后解除归档，允许目标业务正常使用。
    desired_archive = bool(project.get('archived'))
    progress['unarchive_intent'] = not desired_archive
    save()
    receiver.api('PATCH', '/repos/' + quote(group['full_path'], safe='/') + '/' + quote(name, safe=''),
                 {'archived': desired_archive, 'description': project.get('description') or ''})
    repo = receiver.repository(group['full_path'], name)
    require(repo['id'] == progress['repo_id'] and repo['archived'] == desired_archive and not repo.get('has_actions'), '最终归档状态回读失败')
    progress['final_path'], progress['handed_off'] = final_path, True
    save()

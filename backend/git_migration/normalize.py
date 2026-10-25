"""将来源数据转换为接收端原生对象；每条数据都保留稳定来源标识。"""
from datetime import datetime
import json
import re
from urllib.parse import urlsplit

from svn_migration.common import require


def timestamp(value, optional=False):
    if value is None and optional:
        return 0
    require(isinstance(value, str) and value, '来源对象缺少时间，不能用当前时间替代')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(parsed.tzinfo is not None, '来源时间缺少时区')
        result = int(parsed.timestamp())
    except (ValueError, OverflowError):
        require(False, '来源时间格式无效')
    require(result > 0, '来源时间超出支持范围')
    return result


def identifier(value):
    require(type(value) is int and value > 0, '来源对象缺少稳定数字标识')
    return str(value)


def author(row, kind):
    actor = row.get('author' if kind == 'gitlab' else 'user')
    require(isinstance(actor, dict), '来源作者不存在，需明确已删除作者的处理方式')
    name = actor.get('username') or actor.get('login')
    require(isinstance(name, str) and name, '来源作者缺少用户名')
    ident = actor.get('id')
    require(type(ident) is int and ident >= 0, '来源作者标识无效')
    return {'author': name, 'author_id': ident}


def objects(inventory, kind, resolve_tag=None, resolve_diff=None):
    """这里只转换已支持的对象；返回待处理目录供执行门禁核对，不能将其忽略。"""
    require(kind in ('gitlab', 'gitea'), '来源类型无效')
    result, pending = [], []
    labels, milestones, seen = {}, {}, set()
    datasets = inventory['datasets']

    def append(value):
        key = (value['kind'], value['source_id'])
        require(key not in seen, '来源目录重复对象')
        seen.add(key)
        result.append(value)

    for dataset in datasets:
        category = dataset['category']
        if category not in ('标签', '里程碑'):
            continue
        for row in dataset['rows']:
            ident = identifier(row['id'])
            name = row.get('name') if category == '标签' else row.get('title')
            require(isinstance(name, str) and name, '来源标签或里程碑缺少名称')
            index = labels if category == '标签' else milestones
            require(name not in index, '同名标签或里程碑不能无损合并')
            index[name] = ident
            item = {'kind': 'label' if category == '标签' else 'milestone', 'source_id': ident,
                    'title': name, 'content': row.get('description') or ''}
            if category == '标签':
                item['color'] = '#' + row['color'].lstrip('#')
            else:
                item.update(state='open' if row['state'] in ('open', 'active') else row['state'], created=timestamp(row.get('created_at')),
                            updated=timestamp(row.get('updated_at')),
                            closed=timestamp(row.get('closed_at'), True),
                            deadline=timestamp(row.get('due_on') or (row.get('due_date') + 'T00:00:00Z' if row.get('due_date') else None), True))
            append(item)

    issue_ids = {}
    for dataset in datasets:
        if dataset['category'] not in ('议题', '议题与合并请求'):
            continue
        for row in dataset['rows']:
            if row.get('pull_request'):
                continue
            require(not row.get('confidential'), '来源包含保密议题，目标没有等价可见性，禁止迁入普通议题')
            number = row.get('iid') if kind == 'gitlab' else row.get('number')
            identifier(number)
            ident = identifier(row['id'])
            require(number not in issue_ids, '来源议题编号重复')
            issue_ids[number] = ident
            if row.get('assignees') or row.get('assignee'):
                pending.append({'category': '议题负责人映射', 'source_id': ident,
                                'reason': '需要将来源负责人映射为目标已有账号'})
            selected = []
            for label in row.get('labels', []):
                name = label if isinstance(label, str) else label['name']
                require(name in labels, '议题标签不在固定目录中')
                selected.append(labels[name])
            milestone = row.get('milestone')
            milestone_id = ''
            if milestone:
                require(milestone['title'] in milestones, '议题里程碑不在固定目录中')
                milestone_id = milestones[milestone['title']]
            state = row.get('state')
            require(state in ('opened', 'open', 'closed'), '议题状态不兼容')
            append({'kind': 'issue', 'source_id': ident, 'number': number,
                    'title': row['title'], 'content': row.get('description' if kind == 'gitlab' else 'body') or '',
                    'state': 'closed' if state == 'closed' else 'open', 'labels': selected,
                    'milestone': milestone_id, 'locked': bool(row.get('discussion_locked' if kind == 'gitlab' else 'is_locked')),
                    'created': timestamp(row.get('created_at')), 'updated': timestamp(row.get('updated_at')),
                    'closed': timestamp(row.get('closed_at'), True), **author(row, kind)})
    pull_ids = {}
    occupied = set(issue_ids)
    pulls = [row for dataset in datasets if dataset['category'] == ('合并请求详情' if kind == 'gitlab' else '合并请求') for row in dataset['rows']]
    for row in sorted(pulls, key=lambda row: row.get('iid') or row['number']):
        original_number = row.get('iid') or row['number']
        number = original_number if original_number not in occupied else max(occupied, default=0) + 1
        occupied.add(number)
        ident = identifier(row['id'])
        pull_ids[original_number] = ident
        if row.get('assignees') or row.get('assignee') or row.get('requested_reviewers') or row.get('reviewers'):
            pending.append({'category': '合并请求人员映射', 'source_id': ident,
                            'reason': '需要明确负责人及当前评审人的目标账号'})
        if kind == 'gitlab':
            refs = row.get('diff_refs') or {}
            head, base = refs.get('head_sha'), refs.get('base_sha')
            head_branch, base_branch = row['source_branch'], row['target_branch']
            require(row.get('source_project_id') == row.get('target_project_id'), '跨仓库合并请求需要来源仓库映射')
        else:
            head, base = row['head']['sha'], row['base']['sha']
            head_branch, base_branch = row['head']['ref'], row['base']['ref']
            require(row['head'].get('repo', {}).get('id') == row['base'].get('repo', {}).get('id'), '跨仓库合并请求需要来源仓库映射')
        require(head and base, '合并请求缺少完整的历史提交标识')
        selected = []
        for label in row.get('labels', []):
            name = label if isinstance(label, str) else label['name']
            require(name in labels, '合并请求标签不在固定目录中')
            selected.append(labels[name])
        milestone = row.get('milestone')
        milestone_id = milestones.get(milestone['title'], '') if milestone else ''
        require(not milestone or milestone_id, '合并请求里程碑不在固定目录中')
        content = row.get('description' if kind == 'gitlab' else 'body') or ''
        if number != original_number:
            content = f'【来源合并请求 !{original_number}；因目标统一编号规则迁入为 #{number}】\n\n' + content
        append({'kind': 'pull', 'source_id': ident, 'number': number, 'title': row['title'],
                'content': content, 'state': 'closed' if row['state'] in ('closed', 'merged') else 'open',
                'created': timestamp(row['created_at']), 'updated': timestamp(row['updated_at']),
                'closed': timestamp(row.get('closed_at') or row.get('merged_at'), True),
                'merged': timestamp(row.get('merged_at'), True), 'merge_commit': row.get('merge_commit_sha') or '',
                'head_commit': head, 'base_commit': base, 'head_branch': head_branch, 'base_branch': base_branch,
                'labels': selected, 'milestone': milestone_id, 'history': row, **author(row, kind)})
    for dataset in datasets:
        category = dataset['category']
        if category in ('标签', '里程碑', '议题', '议题与合并请求', '合并请求详情') or category == '合并请求' and (kind == 'gitea' or pulls):
            continue
        if kind == 'gitlab' and category in ('合并请求差异版本', '历史审批'):
            number = int(dataset['path'].split('/merge_requests/')[1].split('/')[0])
            require(number in pull_ids, '历史记录所属合并请求未迁移')
            for row in dataset['rows']:
                if category == '历史审批' and not row.get('approved_by'):
                    continue
                version = category == '合并请求差异版本'
                append({'kind': 'pull_version' if version else 'approval_snapshot',
                        'source_id': pull_ids[number] + ':' + (identifier(row['id']) if version else 'approvals'),
                        'parent_id': pull_ids[number], 'title': '合并请求历史版本' if version else '迁移时审批快照',
                        'created': timestamp(row['created_at']) if version else 0,
                        'commit': row.get('head_commit_sha') or '', 'history': row})
            continue
        if kind == 'gitea' and category in ('评审', '行内评审'):
            number = int(dataset['path'].split('/pulls/')[1].split('/')[0])
            require(number in pull_ids, '评审所属合并请求不在迁移目录中')
            for row in dataset['rows']:
                obj = {'source_id': identifier(row['id']), 'parent_kind': 'pull', 'parent_id': pull_ids[number],
                       'content': row.get('body') or '', 'history': row, **author(row, kind)}
                if category == '评审':
                    states = {'APPROVED': 'APPROVED', 'REQUEST_CHANGES': 'CHANGES_REQUESTED', 'COMMENT': 'COMMENTED'}
                    require(row['state'] in states, '来源评审状态未支持')
                    obj.update(kind='review', review_state=states[row['state']],
                               created=timestamp(row['submitted_at']), updated=timestamp(row['updated_at']))
                else:
                    line = row.get('position') or -int(row.get('original_position') or 0)
                    require(line, '历史行内评论缺少有效行位置，需要从差异版本恢复')
                    obj.update(kind='code_comment', path=row['path'], line=line,
                               review_id=identifier(row['pull_request_review_id']), resolved=bool(row.get('resolver')),
                               commit=row['commit_id'], patch=('diff --git a/{0} b/{0}\n--- a/{0}\n+++ b/{0}\n'.format(row['path']) + row['diff_hunk']) if row.get('diff_hunk') else '',
                               created=timestamp(row['created_at']), updated=timestamp(row['updated_at']))
                append(obj)
            continue
        if category == '发布' and resolve_tag is not None:
            for row in dataset['rows']:
                tag = row['tag_name']
                if kind == 'gitlab' and row.get('assets', {}).get('links'):
                    pending.append({'category': '发布外部附件', 'source_id': tag,
                                    'reason': '发布链接需要下载、摘要核验及目标附件关联'})
                append({'kind': 'release', 'source_id': identifier(row['id']) if kind == 'gitea' else tag,
                        'tag': tag, 'title': row.get('name') or tag,
                        'content': row.get('body' if kind == 'gitea' else 'description') or '',
                        'commit': resolve_tag(tag), 'target': row.get('target_commitish') or '',
                        'draft': bool(row.get('draft')), 'prerelease': bool(row.get('prerelease')),
                        'created': timestamp(row.get('created_at')), 'updated': timestamp(row.get('created_at')), **author(row, 'gitlab')})
            continue
        if category in ('流水线', 'Actions 流水线', '作业', 'Actions 作业'):
            is_job = category in ('作业', 'Actions 作业')
            for row in dataset['rows']:
                parent = row.get('pipeline', {}).get('id') if kind == 'gitlab' else row.get('run_id')
                commit = row.get('commit', {}).get('id') if is_job and kind == 'gitlab' else row.get('sha') or row.get('head_sha') or ''
                append({'kind': 'ci_job' if is_job else 'ci_pipeline', 'source_id': identifier(row['id']),
                        'title': row.get('name') or ('作业 ' if is_job else '流水线 ') + str(row['id']),
                        'parent_id': identifier(parent) if is_job and parent else '',
                        'state': row.get('conclusion') or row.get('status') or 'unknown',
                        'commit': commit or '', 'ref': row.get('ref') or row.get('head_branch') or '',
                        'created': timestamp(row.get('created_at') or row.get('started_at') or row.get('run_started_at')),
                        'history': row})
            continue
        if category not in ('议题评论', '议题讨论', '合并请求讨论'):
            if dataset['rows']:
                pending.append({'category': category, 'path': dataset['path'], 'count': len(dataset['rows'])})
            continue
        for row in dataset['rows']:
            notes = row.get('notes', []) if kind == 'gitlab' else [row]
            thread_position = next((n.get('position') for n in notes if n.get('position')), None)
            thread_review = None
            for note in notes:
                require(not note.get('confidential') and not note.get('internal'),
                        '来源包含保密或内部评论，目标没有等价可见性，禁止迁入普通评论')
                if kind == 'gitlab':
                    number = int(dataset['path'].split('/merge_requests/' if category == '合并请求讨论' else '/issues/')[1].split('/')[0])
                else:
                    path = urlsplit(note.get('issue_url') or note.get('pull_request_url') or '').path
                    require(('/issues/' in path or '/pulls/' in path) and path.rsplit('/', 1)[-1].isdigit(), '评论缺少所属议题地址')
                    number = int(path.rsplit('/', 1)[-1])
                parent_kind = 'pull' if category == '合并请求讨论' or kind == 'gitea' and number in pull_ids else 'issue'
                parents = pull_ids if parent_kind == 'pull' else issue_ids
                position = note.get('position') or thread_position
                if number not in parents:
                    pending.append({'category': '系统或合并请求评论', 'source_id': identifier(note['id'])})
                    continue
                if position:
                    if parent_kind != 'pull' or position.get('position_type') != 'text' or position.get('line_range') or resolve_diff is None:
                        pending.append({'category': '行内讨论位置', 'source_id': identifier(note['id']),
                                        'reason': '此讨论位置尚不能完整转换，保留隔离'})
                        continue
                    line = position.get('new_line') or -(position.get('old_line') or 0)
                    require(type(line) is int and line != 0, '行内讨论没有有效的源文件行号')
                    path = position.get('new_path') if line > 0 else position.get('old_path')
                    require(isinstance(path, str) and path, '行内讨论缺少文件路径')
                    patch = resolve_diff(position)
                    require(patch and '@@ ' in patch, '行内讨论对应差异不存在，不能猜测位置')
                    if thread_review is None:
                        thread_review = 'discussion-' + str(row['id'])
                        append({'kind': 'review', 'source_id': thread_review, 'parent_kind': 'pull',
                                'parent_id': parents[number], 'review_state': 'COMMENTED', 'content': '',
                                'created': timestamp(note['created_at']), 'updated': timestamp(note['updated_at']),
                                'history': row, **author(note, kind)})
                    append({'kind': 'code_comment', 'source_id': identifier(note['id']), 'parent_kind': 'pull',
                            'parent_id': parents[number], 'review_id': thread_review, 'content': note.get('body') or '',
                            'path': path, 'line': line, 'commit': position['head_sha'], 'patch': patch,
                            'resolved': bool(note.get('resolved', notes[0].get('resolved', False))),
                            'created': timestamp(note['created_at']), 'updated': timestamp(note['updated_at']),
                            'history': note, **author(note, kind)})
                    continue
                append({'kind': 'comment', 'source_id': identifier(note['id']), 'parent_id': parents[number], 'parent_kind': parent_kind,
                        'content': ('【来源系统事件】\n\n' if note.get('system') else '') + (note.get('body') or ''), 'created': timestamp(note.get('created_at')),
                        'updated': timestamp(note.get('updated_at')), **author(note, kind)})
    if kind == 'gitlab':
        uploads = sorted({match for obj in result for match in re.findall(r'/uploads/[0-9a-fA-F]{32}/[^\s)\]<>]+', obj.get('content', ''))})
        if uploads:
            pending.append({'category': 'GitLab 正文附件', 'count': len(uploads),
                            'reason': '正文包含来源上传文件，需要完成文件下载、摘要核验和链接替换'})
    return {'objects': result, 'pending': pending, 'gaps': inventory.get('gaps', [])}

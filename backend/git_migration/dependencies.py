"""自动固定跨仓库合并请求的依赖闭包，禁止把外部仓库静默改为同仓请求。"""
from svn_migration.common import require, encode
from .inventory import Inventory


def head_projects(item, kind):
    result = set()
    for dataset in item['datasets']:
        if kind == 'gitlab' and dataset['category'] == '触发作业':
            for trigger in dataset['rows']:
                downstream = trigger.get('downstream_pipeline')
                if downstream:
                    require(type(downstream.get('project_id')) is int and downstream['project_id'] > 0,
                            '下游流水线缺少所属项目，无法固定依赖')
                    result.add(downstream['project_id'])
            continue
        if dataset['category'] != ('合并请求详情' if kind == 'gitlab' else '合并请求'):
            continue
        for pull in dataset['rows']:
            head = pull.get('source_project_id') if kind == 'gitlab' else (pull.get('head', {}).get('repo') or {}).get('id')
            require(type(head) is int and head > 0, '合并请求的来源仓库已删除或不可读取，无法恢复真实跨仓库关系')
            result.add(head)
    return result


def inspect_closure(provider, projects, selected, tick=lambda: None):
    by_id = {p['id']: p for p in projects}
    require(set(selected) <= set(by_id), '所选项目不在固定发现范围')
    queue, result, visited, size = list(sorted(selected)), [], set(), 0
    while queue:
        ident = queue.pop(0)
        if ident in visited:
            continue
        require(ident in by_id, '跨仓库 PR 的依赖不在可读取发现范围，请重新发现并确认完整读取权限')
        visited.add(ident)
        require(len(visited) <= 100, '跨仓库依赖闭包超过一百个项目，需调整迁移批次')
        tick()
        item = Inventory(provider).inspect(by_id[ident])
        item['dependency'] = ident not in selected
        size += len(encode(item).encode())
        require(size <= 64 * 1024 * 1024, '本次依赖元数据容量超限')
        result.append(item)
        queue.extend(sorted(head_projects(item, provider.kind) - visited))
    return result

"""历史 CI 文件清单与只读留存，不注册运行器或执行迁入工作流。"""
from urllib.parse import quote
from .normalize import identifier, timestamp
from .assets import transfer_assets


def ci_assets(inventory, kind, base):
    result, pending = [], []
    project = inventory['project']
    prefix = base + ('/api/v4/projects/' + str(project['id']) if kind == 'gitlab' else '/api/v1/repos/' + project['path'])
    for dataset in inventory['datasets']:
        if dataset['category'] not in ('作业', 'Actions 作业'):
            continue
        for job in dataset['rows']:
            ident = identifier(job['id'])
            created = timestamp(job.get('created_at') or job.get('started_at'))
            if kind == 'gitlab' and job.get('erased_at'):
                pending.append({'category': '历史作业文件', 'source_id': ident, 'reason': '来源已擦除该作业日志或制品，不能恢复已不存在的数据'})
                continue
            metadata = {'source_id': 'job-log-' + ident, 'parent_kind': 'ci_job', 'parent_id': ident,
                        'name': 'job-' + ident + '.log', 'created': created}
            result.append({'url': prefix + ('/jobs/' + ident + '/trace' if kind == 'gitlab' else '/actions/jobs/' + ident + '/logs'), 'metadata': metadata})
            if kind == 'gitlab':
                archive = job.get('artifacts_file') or {}
                if archive.get('filename'):
                    result.append({'url': prefix + '/jobs/' + ident + '/artifacts', 'metadata': {
                        'source_id': 'job-artifact-' + ident, 'parent_kind': 'ci_job', 'parent_id': ident,
                        'name': archive['filename'], 'size': archive['size'], 'created': created}})
                for artifact in job.get('artifacts', []):
                    if artifact.get('file_type') not in ('archive', 'trace', 'metadata'):
                        pending.append({'category': '作业报告制品', 'source_id': ident,
                                        'reason': '报告制品 ' + str(artifact.get('filename')) + ' 需要独立下载验证'})
    return result, pending

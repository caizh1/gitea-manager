"""按作业与制品清单保存完整 CI 历史，不运行迁入的工作流。"""
from urllib.parse import quote
from .normalize import identifier, timestamp
from svn_migration.common import require


def ci_assets(inventory, kind, base):
    result, pending = [], []
    project = inventory['project']
    prefix = base + ('/api/v4/projects/' + str(project['id']) if kind == 'gitlab' else '/api/v1/repos/' + project['path'])
    for dataset in inventory['datasets']:
        if dataset['category'] in ('Actions 制品','Actions 历史制品'):
            for artifact in dataset['rows']:
                ident = identifier(artifact['id'])
                if artifact.get('expired'):
                    pending.append({'category': '已过期的历史 CI 制品', 'source_id': ident, 'reason': '源端声明已经过期，无法恢复已不存在的正文'})
                    continue
                result.append({'url': base + artifact['legacy_path'] if artifact.get('legacy_path') else prefix + '/actions/artifacts/' + ident + '/zip',
                    'web': bool(artifact.get('legacy_path')), 'metadata': {
                    'source_id': 'ci-artifact-' + ident, 'parent_kind': 'ci_artifact', 'parent_id': ident,
                    'name': artifact['name'] + '.zip', 'created': timestamp(artifact['created_at'])}})
            continue
        if dataset['category'] not in ('作业', 'Actions 作业'):
            continue
        for job in dataset['rows']:
            ident = identifier(job['id'])
            created = timestamp(job.get('created_at') or job.get('started_at'))
            if kind == 'gitlab' and job.get('erased_at'):
                pending.append({'category': '已擦除的历史作业文件', 'source_id': ident, 'reason': '来源已擦除该作业日志或制品，不能恢复已不存在的数据'})
                continue
            status = job.get('conclusion') or job.get('status')
            if status in ('running', 'pending', 'queued', 'preparing', 'waiting_for_resource'):
                pending.append({'category': '仍在变化的 CI 作业', 'source_id': ident, 'reason': '请先停止来源流水线，不能固定运行中的日志'})
                continue
            # 从未启动的手动或跳过作业没有可下载日志，仍保留原始状态。
            never_started = kind == 'gitlab' and not job.get('started_at') and (status in ('manual', 'skipped', 'created') or status == 'failed' and job.get('failure_reason') == 'runner_unsupported')
            if not never_started:
                result.append({'url': prefix + ('/jobs/' + ident + '/trace' if kind == 'gitlab' else '/actions/jobs/' + ident + '/logs'),
                    'metadata': {'source_id': 'job-log-' + ident, 'parent_kind': 'ci_job', 'parent_id': ident,
                                 'name': 'job-' + ident + '.log', 'created': created}})
            if kind == 'gitlab':
                artifacts = list(job.get('artifacts') or [])
                archive = job.get('artifacts_file') or {}
                if archive.get('filename') and not any(a.get('file_type') == 'archive' for a in artifacts):
                    artifacts.append({'file_type': 'archive', **archive})
                types = set()
                for artifact in artifacts:
                    file_type = artifact.get('file_type')
                    require(isinstance(file_type, str) and file_type.replace('_', '').isalnum() and file_type not in types,
                            'CI 作业制品类型缺失或重复，不能建立完整清单')
                    types.add(file_type)
                    # 网页原始文件入口可读取 API 不能导出的报告和索引，不重新生成报告。
                    url = (prefix + '/jobs/' + ident + '/artifacts' if file_type == 'archive' else
                           base + '/' + project['path'] + '/-/jobs/' + ident + '/artifacts/download?file_type=' + quote(file_type, safe=''))
                    result.append({'url': url, 'web': file_type != 'archive', 'metadata': {
                        'source_id': 'job-artifact-' + ident + '-' + file_type, 'parent_kind': 'ci_job', 'parent_id': ident,
                        'name': file_type + '-' + artifact['filename'], 'size': artifact['size'], 'created': created}})
    return result, pending

"""独立从来源重建对象与文件清单，不读取迁移检查点，也不调用转换器或盘点器。"""
import hashlib
import re
from urllib.parse import quote
from svn_migration.common import require, encode, digest
from .transport import SourceHTTPError


class Census:
    def __init__(self, provider, project):
        self.provider, self.project = provider, project
        self.objects, self.files, self.rules, self.gaps = set(), {}, set(), []

    def rows(self, path, **options):
        try:
            return self.provider.collection(path, **options)
        except SourceHTTPError as error:
            if error.http_status not in (403, 404):
                raise
            self.gaps.append({'path': path, 'http_status': error.http_status})
            return []

    def add(self, kind, ident):
        self.objects.add((kind, str(ident)))
        require(len(self.objects) <= 100000, '独立来源对象清单超过容量上限', 'capacity')

    def file(self, kind, parent, name, size=None, sha256=None, source_id=None):
        require(source_id, '独立来源文件缺少稳定身份')
        key = source_id
        value = {'parent_kind': kind, 'parent_id': str(parent), 'name': name}
        value['source_id'] = source_id
        if size is not None: value['size'] = size
        if sha256: value['sha256'] = sha256
        require(key not in self.files or self.files[key] == value, '独立来源文件身份重复且元数据不同')
        self.files[key] = value
        require(len(self.files) <= 100000, '独立来源文件清单超过容量上限', 'capacity')

    def run(self):
        if self.provider.kind == 'gitea': self.gitea()
        else: self.gitlab()
        value = {'schema': 1, 'kind': self.provider.kind, 'project_id': self.project['id'], 'objects': sorted(self.objects),
                 'files': sorted(self.files.values(), key=lambda row: encode(row)), 'rules': sorted(self.rules), 'gaps': self.gaps}
        require(len(encode(value).encode()) <= 32*1024*1024, '独立来源清单容量超限', 'capacity')
        return value

    def gitea(self):
        base = '/repos/' + quote(self.project['path'], safe='/')
        for route, kind, options in (('labels', 'label', {}), ('milestones', 'milestone', {'params': {'state': 'all'}})):
            for row in self.rows(base + '/' + route, **options): self.add(kind, row['id'])
        pulls = {p['number']: p for p in self.rows(base + '/pulls', params={'state': 'all'})}
        issues = {}
        for state in ('open', 'closed'):
            for row in self.rows(base + '/issues', params={'state': state, 'type': 'all'}): issues[row['number']] = row
        for number, listed in issues.items():
            row = self.provider.get(base + '/issues/' + str(number))
            kind = 'pull' if row.get('pull_request') or number in pulls else 'issue'
            ident = str(pulls[number]['id'] if kind == 'pull' else row['id'])
            self.add(kind, ident)
            # 议题详情与逐议题评论是独立于全仓评论列表的读取入口。
            for asset in row.get('assets') or []: self.file(kind, ident, asset['name'], asset['size'], source_id='attachment-'+str(asset['id']))
            for comment in self.rows(base + '/issues/' + str(number) + '/comments'):
                self.add('comment', comment['id'])
                for asset in comment.get('assets') or []: self.file('comment', comment['id'], asset['name'], asset['size'], source_id='attachment-'+str(asset['id']))
        for number, pull in pulls.items():
            self.add('pull', pull['id'])
            for review in self.rows(base + '/pulls/' + str(number) + '/reviews'):
                if review['state'] != 'REQUEST_REVIEW': self.add('review', review['id'])
                for comment in self.rows(base + '/pulls/' + str(number) + '/reviews/' + str(review['id']) + '/comments'):
                    self.add('code_comment', comment['id'])
                    for asset in self.rows(base + '/issues/comments/' + str(comment['id']) + '/assets'):
                        self.file('code_comment', comment['id'], asset['name'], asset['size'], source_id='attachment-'+str(asset['id']))
        for release in self.rows(base + '/releases'):
            self.add('release', release['id'])
            for asset in release.get('assets') or []: self.file('release', release['id'], asset['name'], asset['size'], source_id='attachment-'+str(asset['id']))
        for route, field in (('branch_protections', 'rule_name'), ('tag_protections', 'name_pattern')):
            for rule in self.rows(base + '/' + route, identity=field if route == 'branch_protections' else 'id'):
                self.rules.add(('branch' if route == 'branch_protections' else 'tag', rule[field]))
        runs = self.rows(base + '/actions/runs', key='workflow_runs')
        native_assets = self.rows(base + '/actions/artifacts', key='artifacts')
        for artifact in native_assets:
            self.add('ci_artifact', artifact['id'])
            if not artifact.get('expired'): self.file('ci_artifact', artifact['id'], artifact['name'] + '.zip', source_id='ci-artifact-'+str(artifact['id']))
        for run in runs:
            self.add('ci_pipeline', run['id'])
            for job in self.rows(base + '/actions/runs/' + str(run['id']) + '/jobs', key='jobs'):
                self.add('ci_job', job['id'])
                self.file('ci_job', job['id'], 'job-' + str(job['id']) + '.log', source_id='job-log-'+str(job['id']))
        if runs:
            from .gitea_web import authenticate_web, read_run
            from .providers import version_tuple
            authenticate_web(self.provider)
            for run in runs:
                index = run['id'] if version_tuple(self.provider.version) >= (1,27,0) else run.get('run_number') or run['id']
                value = read_run(self.provider, self.project['path'], index)
                for artifact in value['artifacts'] or []:
                    if any(a.get('name') == artifact['name'] and (a.get('workflow_run') or {}).get('id') == run['id'] for a in native_assets): continue
                    ident = int(hashlib.sha256((str(run['id'])+'/'+artifact['name']).encode()).hexdigest()[:15],16)
                    self.add('ci_artifact', ident)
                    if artifact.get('status') != 'expired': self.file('ci_artifact', ident, artifact['name']+'.zip', source_id='ci-artifact-'+str(ident))
        owner = self.project['path'].split('/')[0]
        for package in self.rows('/packages/' + quote(owner, safe='')):
            if (package.get('repository') or {}).get('id') != self.project['id']: continue
            if package['type'] == 'container': continue
            self.add('package', package['id'])
            route = '/packages/' + quote(owner, safe='') + '/' + '/'.join(quote(str(package[k]), safe='') for k in ('type','name','version')) + '/files'
            for file in self.rows(route): self.file('package', package['id'], file['name'], file['size'], file.get('sha256'), source_id='package-file-'+str(file['id']))

    def gitlab(self):
        base = '/projects/' + str(self.project['id'])
        for route, kind in (('labels','label'), ('milestones','milestone')):
            for row in self.rows(base+'/'+route): self.add(kind, row['id'])
        def discussions(path, parent_kind, parent):
            for thread in self.rows(path+'/discussions', identity='id'):
                notes = thread.get('notes') or []
                position = any(n.get('position') for n in notes)
                if position: self.add('review', 'discussion-'+str(thread['id']))
                for note in notes:
                    kind = 'code_comment' if position else 'comment'
                    self.add(kind, note['id'])
                    self.uploads(kind, note['id'], note.get('body') or '')
        for issue in self.rows(base+'/issues', params={'scope':'all','state':'all'}):
            self.add('issue', issue['id']); self.uploads('issue', issue['id'], issue.get('description') or '')
            discussions(base+'/issues/'+str(issue['iid']), 'issue', issue['id'])
        for pull in self.rows(base+'/merge_requests', params={'scope':'all','state':'all'}):
            path = base+'/merge_requests/'+str(pull['iid'])
            self.add('pull', pull['id']); self.uploads('pull', pull['id'], pull.get('description') or '')
            discussions(path, 'pull', pull['id'])
            for version in self.rows(path+'/versions'): self.add('pull_version', str(pull['id'])+':'+str(version['id']))
            approval = self.provider.get(path+'/approvals')
            if approval.get('approved_by'): self.add('approval_snapshot', str(pull['id'])+':approvals')
        for release in self.rows(base+'/releases', identity='tag_name'):
            self.add('release', release['tag_name']); self.uploads('release', release['tag_name'], release.get('description') or '')
        for route, kind in (('protected_branches','branch'), ('protected_tags','tag')):
            for rule in self.rows(base+'/'+route, identity='name'): self.rules.add((kind, rule['name']))
        roots = self.rows(base+'/pipelines')
        jobs = {j['id']:j for j in self.rows(base+'/jobs', params={'include_retried':'true'})}
        queue, visited = [p['id'] for p in roots] + [j['pipeline']['id'] for j in jobs.values() if j.get('pipeline')], set()
        while queue:
            ident = queue.pop()
            if ident in visited: continue
            visited.add(ident)
            require(len(visited) <= 10000, '独立流水线清单超过容量上限', 'capacity')
            self.add('ci_pipeline', ident)
            for job in self.rows(base+'/pipelines/'+str(ident)+'/jobs', params={'include_retried':'true'}): jobs[job['id']] = job
            for trigger in self.rows(base+'/pipelines/'+str(ident)+'/bridges'):
                self.add('ci_trigger', trigger['id'])
                downstream = trigger.get('downstream_pipeline')
                if downstream and downstream['project_id'] == self.project['id']: queue.append(downstream['id'])
        for job in jobs.values():
            ident = job['id']; self.add('ci_job', ident)
            never = not job.get('started_at') and (job['status'] in ('manual','skipped','created') or job['status'] == 'failed' and job.get('failure_reason') == 'runner_unsupported')
            if not never and not job.get('erased_at'): self.file('ci_job',ident,'job-'+str(ident)+'.log',source_id='job-log-'+str(ident))
            artifacts = list(job.get('artifacts') or [])
            archive = job.get('artifacts_file') or {}
            if archive.get('filename') and not any(a.get('file_type') == 'archive' for a in artifacts): artifacts.append({'file_type':'archive',**archive})
            for file in artifacts: self.file('ci_job', ident, file['file_type']+'-'+file['filename'], file['size'],source_id='job-artifact-'+str(ident)+'-'+file['file_type'])
        for package in self.rows(base+'/packages'):
            self.add('package', package['id'])
            for file in self.rows(base+'/packages/'+str(package['id'])+'/package_files'):
                self.file('package', package['id'], file['file_name'], file['size'], file.get('file_sha256'),source_id='package-file-'+str(file['id']))

    def uploads(self, kind, parent, content):
        paths = set(re.findall(r'/uploads/[0-9a-fA-F]{32}/[^\s/\)\]<>"?]+', content))
        from urllib.parse import unquote
        for path in paths:
            ident = 'upload-' + hashlib.sha256((kind+':'+str(parent)+':'+path).encode()).hexdigest()
            self.file(kind, parent, unquote(path.rsplit('/',1)[-1]), source_id=ident)


def capture(provider, project):
    return Census(provider, project).run()


def verify_plan(manifest, normalized, assets, rule_plan):
    require(not manifest.get('gaps'), '独立来源清单存在读取缺口，禁止交接', 'manifest_gap')
    expected = {tuple(row) for row in manifest['objects']}
    actual = {(row['kind'], row['source_id']) for row in normalized['objects'] if row['kind'] != 'merge_policy'}
    require(expected == actual, '独立来源对象清单与转换结果不一致：缺少 ' + str(len(expected-actual)) + '，多出 ' + str(len(actual-expected)), 'manifest_mismatch')
    planned = {}
    for asset in assets:
        row = asset['metadata']; key = row['source_id']
        require(key not in planned, '迁移文件计划重复，不能证明完整性')
        planned[key] = (row, asset.get('expected_sha256'))
    expected_files = {row['source_id']:row for row in manifest['files']}
    require(set(planned) == set(expected_files), '独立来源文件清单与传输计划不一致', 'manifest_mismatch')
    for key, source in expected_files.items():
        target, sha = planned[key]
        require(all(source[k] == target.get(k) for k in ('parent_kind','parent_id','name')), '独立来源文件所属对象或名称与计划不同', 'manifest_mismatch')
        require('size' not in source or source['size'] == target.get('size'), '独立来源文件大小与计划不同', 'manifest_mismatch')
        require(not source.get('sha256') or source['sha256'] == sha, '独立来源文件摘要与计划不同', 'manifest_mismatch')
    actual_rules = {('tag' if row.get('_tag') else 'branch', row.get('rule_name') or row.get('name_pattern')) for row in rule_plan}
    # GitLab 标签的原始名称与目标正则并存，避免把语法转换当成同名缺失。
    expected_rules = {(kind, '/^'+re.escape(name).replace(r'\*','.*')+'$/' if kind == 'tag' and manifest['kind'] == 'gitlab' else name)
                      for kind, name in manifest['rules']}
    require(actual_rules == expected_rules, '独立来源规则清单与转换结果不一致', 'manifest_mismatch')
    return {'schema':1, 'source_sha256':digest(manifest), 'objects':len(expected), 'files':len(expected_files), 'rules':len(actual_rules)}

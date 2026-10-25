"""按实际接口收集来源清单；接口缺失始终作为缺口，不推断成空数据。"""
import hashlib
from urllib.parse import quote

from svn_migration.common import encode, require
from .transport import SourceHTTPError


class Inventory:
    def __init__(self, provider):
        self.provider = provider
        self.datasets = []
        self.gaps = []
        self.objects = 0
        self.bytes = 0

    def collect(self, category, path, **kwargs):
        try:
            rows = self.provider.collection(path, **kwargs)
        except SourceHTTPError as error:
            if error.http_status not in (403, 404):
                raise
            if error.http_status == 404 and category == '审批规则' and getattr(self.provider, 'enterprise', None) is False:
                self.datasets.append({'category': category, 'path': path, 'rows': [],
                                      'capability': '来源元信息确认社区版，不提供企业审批规则接口'})
                return []
            self.gaps.append({'category': category, 'path': path, 'http_status': error.http_status,
                              'reason': '接口不存在或没有读取权限，不能认定为空'})
            return []
        self.objects += len(rows)
        self.bytes += len(encode(rows).encode())
        require(self.bytes <= 32 * 1024 * 1024, '仓库元数据超过本次盘点容量', 'capacity')
        require(self.objects <= 100000, '单仓库盘点对象超过上限，请分批规划', 'capacity')
        self.datasets.append({'category': category, 'path': path, 'rows': rows,
                              'sha256': hashlib.sha256(encode(rows).encode()).hexdigest()})
        return rows

    def inspect(self, project):
        require(type(project.get('id')) is int and project['id'] > 0, '项目标识无效')
        if self.provider.kind == 'gitlab':
            self.gitlab(project)
        else:
            self.gitea(project)
        return {'project': project, 'datasets': self.datasets, 'gaps': self.gaps,
                'object_count': self.objects, 'migration_verified': False,
                'boundary': '固定接口元数据及旧版制品摘要；其余 Git、LFS、Wiki、附件和日志仍需执行核验，尚未证明来源停写及目标兼容'}

    def gitlab(self, project):
        base = '/projects/' + str(project['id'])
        current = self.provider.get(base)
        require(current.get('id') == project['id'] and current.get('path_with_namespace') == project['path'],
                '来源项目身份或路径已变化，请重新发现', 'stale')
        require(bool(current.get('archived')) == bool(project.get('archived')),
                '来源归档状态已变化，请重新发现', 'stale')
        if self.provider.kind == 'gitlab':
            self.collect('Wiki 页面', base + '/wikis', params={'with_content': '1'}, identity='slug')
        elif current.get('has_wiki'):
            self.collect('Wiki 页面', base + '/wiki/pages', identity='html_url')
        self.collect('标签', base + '/labels')
        self.collect('里程碑', base + '/milestones')
        self.collect('发布', base + '/releases', identity='tag_name')
        issues = self.collect('议题', base + '/issues', params={'scope': 'all', 'state': 'all'})
        for issue in issues:
            number = issue.get('iid')
            require(type(number) is int and number > 0, '议题缺少项目内编号', 'source_format')
            self.collect('议题讨论', base + '/issues/' + str(number) + '/discussions')
            self.collect('议题关系', base + '/issues/' + str(number) + '/links')
        pulls = self.collect('合并请求', base + '/merge_requests', params={'scope': 'all', 'state': 'all'})
        for pull in pulls:
            number = pull.get('iid')
            require(type(number) is int and number > 0, '合并请求缺少项目内编号', 'source_format')
            path = base + '/merge_requests/' + str(number)
            self.object('合并请求详情', path)
            self.collect('合并请求讨论', path + '/discussions')
            self.collect('合并请求差异版本', path + '/versions')
            # 审批接口返回对象；与列表接口不同，单独处理并保留缺口。
            self.object('历史审批', path + '/approvals')
        self.collect('分支保护', base + '/protected_branches', identity='name')
        self.collect('标签保护', base + '/protected_tags', identity='name')
        self.collect('审批规则', base + '/approval_rules')
        self.collect('流水线', base + '/pipelines', params={'order_by': 'id', 'sort': 'asc'})
        self.collect('作业', base + '/jobs', params={'include_retried': 'true'})
        packages = self.collect('制品包', base + '/packages', params={'order_by': 'created_at', 'sort': 'asc'})
        for package in packages:
            if package.get('status','default') not in ('default','deprecated'):
                self.gaps.append({'category':'制品处理状态','path':base+'/packages/'+str(package['id']),'reason':'来源制品仍在处理或不可安装，不能认定为正式版本'})
            files = self.collect('制品文件', base + '/packages/' + str(package['id']) + '/package_files')
            self.pin_package_hashes(project, package, files)
            self.datasets[-1]['sha256'] = hashlib.sha256(encode(files).encode()).hexdigest()
            if package.get('package_type') == 'npm':
                self.object('制品索引', base + '/packages/npm/' + quote(package['name'], safe=''))
        self.collect('容器镜像仓库', base + '/registry/repositories')

    def object(self, category, path):
        try:
            row = self.provider.get(path)
        except SourceHTTPError as error:
            if error.http_status not in (403, 404):
                raise
            self.gaps.append({'category': category, 'path': path, 'http_status': error.http_status,
                              'reason': '接口不存在或没有读取权限，不能认定为空'})
            return
        require(isinstance(row, dict), '来源对象接口格式不兼容', 'source_format')
        self.objects += 1
        self.bytes += len(encode(row).encode())
        require(self.objects <= 100000 and self.bytes <= 32 * 1024 * 1024,
                '仓库元数据超过本次盘点容量', 'capacity')
        self.datasets.append({'category': category, 'path': path, 'rows': [row],
                              'sha256': hashlib.sha256(encode(row).encode()).hexdigest()})

    def gitea(self, project):
        parts = project['path'].split('/')
        require(len(parts) == 2 and all(parts), '开源 Gitea 仓库路径无效')
        base = '/repos/' + '/'.join(quote(part, safe='') for part in parts)
        current = self.provider.get(base)
        require(current.get('id') == project['id'] and current.get('full_name') == project['path'],
                '来源项目身份或路径已变化，请重新发现', 'stale')
        require(bool(current.get('archived')) == bool(project.get('archived')),
                '来源归档状态已变化，请重新发现', 'stale')
        if self.provider.kind == 'gitlab':
            self.collect('Wiki 页面', base + '/wikis', params={'with_content': '1'}, identity='slug')
        elif current.get('has_wiki'):
            self.collect('Wiki 页面', base + '/wiki/pages', identity='html_url')
        self.collect('标签', base + '/labels')
        self.collect('里程碑', base + '/milestones', params={'state': 'all'})
        releases = self.collect('发布', base + '/releases')
        for release in releases:
            self.collect('发布附件', base + '/releases/' + str(release['id']) + '/assets')
        issues = self.collect('议题与合并请求', base + '/issues', params={'state': 'all', 'type': 'all'})
        self.collect('议题评论', base + '/issues/comments')
        for issue in issues:
            number = issue.get('number')
            require(type(number) is int and number > 0, '议题缺少项目内编号', 'source_format')
            self.collect('议题附件', base + '/issues/' + str(number) + '/assets')
        pulls = self.collect('合并请求', base + '/pulls', params={'state': 'all'})
        for pull in pulls:
            number = pull.get('number')
            require(type(number) is int and number > 0, '合并请求缺少项目内编号', 'source_format')
            path = base + '/pulls/' + str(number)
            reviews = self.collect('评审', path + '/reviews')
            for review in reviews:
                self.collect('行内评审', path + '/reviews/' + str(review['id']) + '/comments')
        self.collect('分支保护', base + '/branch_protections', identity='rule_name')
        self.collect('标签保护', base + '/tag_protections')
        runs = self.collect('Actions 流水线', base + '/actions/runs', key='workflow_runs')
        self.collect('Actions 作业', base + '/actions/jobs', key='jobs')
        artifacts = self.collect('Actions 制品', base + '/actions/artifacts', key='artifacts')
        self.legacy_artifacts(project, runs, artifacts)
        packages = self.collect('所有者制品包', '/packages/' + quote(parts[0], safe=''))
        if packages:
            linked = [p for p in packages if (p.get('repository') or {}).get('id') == project['id']]
            self.objects -= len(packages) - len(linked)
            self.datasets[-1]['rows'] = linked
            self.datasets[-1]['sha256'] = hashlib.sha256(encode(linked).encode()).hexdigest()
            if any(not p.get('repository') for p in packages):
                self.gaps.append({'category': '制品归属', 'path': '/packages/' + parts[0],
                              'reason': '来源所有者存在未关联仓库的制品，需要单独指定目标归属'})
            for package in linked:
                path = '/packages/' + quote(parts[0], safe='') + '/' + '/'.join(quote(str(package[k]), safe='') for k in ('type', 'name', 'version')) + '/files'
                self.collect('制品文件', path)
                if package.get('type') == 'npm':
                    protocol = '/api/packages/' + quote(parts[0], safe='') + '/npm/' + quote(package['name'], safe='')
                    value, _ = self.provider.http.request('GET', protocol, **self.provider.credentials())
                    require(isinstance(value, dict), '来源制品索引格式无效')
                    self.bytes += len(encode(value).encode()); self.objects += 1
                    require(self.bytes <= 32*1024*1024 and self.objects <= 100000, '制品索引超过盘点容量', 'capacity')
                    self.datasets.append({'category': '制品索引', 'path': protocol, 'rows': [value]})

    def pin_package_hashes(self, project, package, files):
        """旧版 GitLab 多种协议没有 SHA256；固定正文，并核对已有弱摘要。"""
        import tempfile
        from pathlib import Path
        from flask import has_app_context
        from .store import root
        from .files import SourceFiles
        from .gitlab_web import authenticate_web
        missing=[f for f in files if not f.get('file_sha256')]
        if not missing:return
        authenticate_web(self.provider)
        folder=root() if has_app_context() else Path('work/git-migration/source-hashes')
        folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        with tempfile.TemporaryDirectory(prefix='package-hash-',dir=folder) as directory:
            downloader=SourceFiles(self.provider,directory,tick=self.provider.http.tick)
            for file in missing:
                url=self.provider.http.base+'/'+project['path']+'/-/package_files/'+str(file['id'])+'/download'
                local=downloader.download(url,expected_size=file['size'],follow_same_origin=True)
                hashes={name:hashlib.new(name) for name in ('sha1','md5') if file.get('file_'+name)}
                with Path(local['file']).open('rb') as stream:
                    for block in iter(lambda:stream.read(1024*1024),b''):
                        for h in hashes.values():h.update(block)
                require(all(h.hexdigest()==file['file_'+name] for name,h in hashes.items()),'来源制品正文与已有摘要不一致')
                file['file_sha256']=local['sha256']

    def legacy_artifacts(self,project,runs,artifacts):
        """REST 只列出新版上传制品；逐流水线读取网页清单，补齐旧协议已确认的文件。"""
        if not runs:return
        from .gitea_web import authenticate_web, read_run
        from .normalize import timestamp
        authenticate_web(self.provider)
        rows=[]
        for run in runs:
            self.provider.http.tick()
            from .providers import version_tuple
            index=run['id'] if version_tuple(self.provider.version)>=(1,27,0) else run.get('run_number') or run['id']
            path='/'+project['path']+'/actions/runs/'+str(index)+'/jobs/0'
            value=read_run(self.provider,project['path'],index)
            require(isinstance(value,dict) and 'artifacts' in value and (value['artifacts'] is None or isinstance(value['artifacts'],list)), '旧版 Actions 文件清单缺失')
            identity=value.get('state',{}).get('run',{})
            require(identity.get('repoId')==project['id'] and identity.get('link')=='/'+project['path']+'/actions/runs/'+str(index), '旧版 Actions 返回了不同流水线')
            for item in value['artifacts'] or []:
                if any(a.get('name')==item['name'] and (a.get('workflow_run') or {}).get('id')==run['id'] for a in artifacts):continue
                ident=int(hashlib.sha256((str(run['id'])+'/'+item['name']).encode()).hexdigest()[:15],16)
                created=run.get('created_at') or run.get('started_at') or run.get('run_started_at');timestamp(created)
                rows.append({**item,'id':ident,'created_at':created,'run_id':run['id'],'expired':item.get('status')=='expired',
                             'legacy_path':'/'+project['path']+'/actions/runs/'+str(index)+'/artifacts/'+quote(item['name'],safe='')})
        self.objects+=len(rows);self.bytes+=len(encode(rows).encode())
        require(self.bytes<=32*1024*1024 and self.objects<=100000,'旧版 CI 制品清单容量超限','capacity')
        self.datasets.append({'category':'Actions 历史制品','path':'/'+project['path']+'/actions/legacy-artifacts','rows':rows})

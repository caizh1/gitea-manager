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
                'boundary': '只包含接口元数据；尚未下载核对 Git、LFS、Wiki、附件、日志和制品内容，也未证明源端停写与目标兼容'}

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
            self.collect('制品文件', base + '/packages/' + str(package['id']) + '/package_files')
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
        self.collect('Actions 流水线', base + '/actions/runs', key='workflow_runs')
        self.collect('Actions 作业', base + '/actions/jobs', key='jobs')
        self.collect('Actions 制品', base + '/actions/artifacts', key='artifacts')
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

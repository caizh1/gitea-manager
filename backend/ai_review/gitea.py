"""Gitea 审核适配器；不跟随重定向，不记录远端响应正文。"""
import base64
import hashlib
import re
import itertools
import tempfile
import shutil
import time
from urllib.parse import quote

import requests

from .common import ReviewError, AccessRevoked, Stale, PublishUnknown, WriteRejected, endpoint, safe_path, valid_sha


class Gitea:
    def __init__(self, url, token, repo_name=None, repo_id=None, guard=None):
        from .host_policy import gitea_address
        self.url = endpoint(gitea_address(url), 'AI_REVIEW_GITEA_HOSTS')
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({'Authorization': 'token ' + token, 'Accept': 'application/json'})
        self.repo_name, self.repo_id, self.guard = repo_name, repo_id, guard
        self.page_limit = None
        self.response_total = None
        if repo_name:
            safe_path(repo_name)
            if len(repo_name.split('/')) != 2:
                raise ReviewError('仓库名称需要所有者和仓库两部分')
        self.prefix = '/repos/' + quote(repo_name, safe='/') if repo_name else ''

    def close(self):
        self.session.close()

    def check_active(self):
        if self.guard:
            self.guard()
        deadline = getattr(self, 'deadline_at', None)
        if deadline is not None and time.time() >= deadline:
            raise ReviewError('原生审查总期限已到，停止读取 Gitea')

    def request_timeout(self, maximum=30):
        deadline = getattr(self, 'deadline_at', None)
        return maximum if deadline is None else max(.1, min(maximum, deadline-time.time()))

    def request(self, method, path, data=None, params=None, raw=False, missing=False, limit=4*1024*1024):
        if method not in ('GET', 'HEAD') and getattr(self, 'access_authorize', None):
            self.access_authorize('write')
        self.check_active()
        try:
            with self.session.request(method, self.url + '/api/v1' + path, json=data, params=params,
                                      timeout=(min(5, self.request_timeout()), self.request_timeout()), allow_redirects=False, stream=True) as response:
                if response.status_code == 404 and missing:
                    return None
                if response.status_code in (401,403):
                    raise AccessRevoked(f'Gitea 凭据或权限已失效：HTTP {response.status_code}')
                if not 200 <= response.status_code < 300:
                    if method not in ('GET', 'HEAD') and response.status_code >= 500:
                        raise PublishUnknown('Gitea 写入返回服务错误，需要核对实际结果')
                    if method not in ('GET', 'HEAD') and 400 <= response.status_code < 500:
                        raise WriteRejected(f'Gitea 明确拒绝写入：HTTP {response.status_code}')
                    raise ReviewError(f'Gitea 请求失败：HTTP {response.status_code}')
                parts, size = [], 0
                for part in response.iter_content(65536):
                    self.check_active()
                    size += len(part)
                    if size > limit:
                        raise ReviewError('Gitea 响应超过安全上限，未使用截断数据')
                    parts.append(part)
                content = b''.join(parts)
                total = response.headers.get('X-Total-Count', '')
                self.response_total = int(total) if isinstance(total,str) and total.isdigit() else None
                if raw:
                    return content
                if not content:
                    return None
                import json
                return json.loads(content)
        except requests.RequestException:
            if method not in ('GET', 'HEAD'):
                raise PublishUnknown('Gitea 写入响应不确定，需要核对实际结果') from None
            raise ReviewError('Gitea 连接失败或超时') from None
        except (ValueError, UnicodeError):
            raise ReviewError('Gitea 返回了无效 JSON') from None

    def diff_patches(self, number):
        """累计 Diff 落入临时文件，再逐文件解析，不按 PR 总字节截断。"""
        from .diff import parse_diff, Patch
        self.check_active()
        try:
            with self.session.get(self.url+'/api/v1'+self.prefix+f'/pulls/{number}.diff',
                                  timeout=(min(15,self.request_timeout()),self.request_timeout()),allow_redirects=False,stream=True) as response:
                if response.status_code in (401,403):raise AccessRevoked('读取 PR 补丁权限失效')
                if response.status_code != 200:raise ReviewError(f'读取 PR 补丁失败：HTTP {response.status_code}')
                with tempfile.TemporaryFile() as spool:
                    received=0
                    for part in response.iter_content(65536):
                        self.check_active()
                        free=shutil.disk_usage(tempfile.gettempdir()).free
                        if free < len(part)+64*1024*1024:
                            raise ReviewError(f'补丁磁盘空间不足：已接收 {received} 字节，可用 {free} 字节，需预留 67108864 字节')
                        spool.write(part);received+=len(part)
                    spool.seek(0);current=bytearray();size=0;over=False;line_start=True
                    def complete():
                        if not over:return parse_diff(bytes(current))
                        # 只恢复已收到的文件头；未完整校验的补丁绝不生成锚点。
                        header=bytes(current).split(b'@@ ',1)[0]
                        try:records=parse_diff(header)
                        except ReviewError:records=[Patch()]
                        for item in records:item.unsupported=f'单文件补丁 {size} 字节超过 67108864 字节内存保护；本文件未审查'
                        return records
                    while True:
                        chunk=spool.readline(65536)
                        if not chunk:break
                        if line_start and chunk.startswith(b'diff --git ') and size:
                            yield from complete();current.clear();size=0;over=False
                        size+=len(chunk)
                        if size<=64*1024*1024:current.extend(chunk)
                        elif not over:
                            over=True
                            current[:]=bytes(current).split(b'@@ ',1)[0][:65536]
                        line_start=chunk.endswith(b'\n')
                    if size:yield from complete()
        except requests.RequestException:
            raise ReviewError('读取 PR 补丁连接失败或中断') from None

    def version(self):
        version = self.request('GET', '/version').get('version')
        if version not in ('1.26.4','1.26.4+review.1') and not re.fullmatch(r'1\.27\.3(?:\+[0-9A-Za-z.-]+)?', version or ''):
            raise ReviewError('当前发布适配器允许 Gitea 1.26.4（含 review.1）及增强版 1.27.3（待内网验收），其他版本需先完成兼容测试')
        return version

    def verify_review_scopes(self):
        """1.26.4 先验证 Token scope，再处理不存在的 0 号资源与空正文。

        两个固定请求均不能生成评论。仅接受该版本已实测的拒绝结果，
        不把普通 GET 成功误认为具备发表评论所需的 Token 范围。
        """
        for path, expected, scope in (('/pulls/0/reviews', 404, 'write:repository'),
                                      ('/issues/0/comments', 422, 'write:issue')):
            try:
                with self.session.post(self.url+'/api/v1'+self.prefix+path, json={},
                                       timeout=(5, 15), allow_redirects=False, stream=True) as response:
                    status = response.status_code
            except requests.RequestException:
                raise ReviewError('机器人 Token 权限检查连接失败或超时，未保存') from None
            if status == 403:
                raise ReviewError(f'机器人 Token 缺少 {scope} 范围；账号仍需保持仓库只读')
            if status != expected:
                raise ReviewError(f'无法确认机器人 Token 的 {scope} 范围（HTTP {status}），未保存')

    def repository(self):
        repo = self.request('GET', self.prefix)
        if repo.get('id') != self.repo_id or repo.get('full_name') != self.repo_name:
            raise Stale('仓库标识或名称已变化，请重新配置')
        return repo

    def pr(self, number):
        value = self.request('GET', self.prefix + '/pulls/' + str(number))
        if value.get('base', {}).get('repo', {}).get('id') != self.repo_id:
            raise ReviewError('PR 不属于配置的目标仓库')
        return value

    def file(self, path, sha):
        if path is None:
            return b''
        maximum = getattr(self, 'max_file_bytes', 128*1024)
        value = self.request('GET', self.prefix + '/contents/' + quote(safe_path(path), safe='/'),
                             params={'ref': valid_sha(sha)}, missing=True, limit=maximum*2+65536)
        if value is None:
            raise ReviewError('固定版本中缺少所需文件')
        if (not isinstance(value, dict) or value.get('type') != 'file' or value.get('encoding') != 'base64'
                or type(value.get('size')) is not int or not 0 <= value['size'] <= maximum):
            raise ReviewError('文件类型或大小不支持可靠读取')
        try:
            content = base64.b64decode(value.get('content', ''), validate=True)
        except ValueError:
            raise ReviewError('文件内容编码无效') from None
        sha = hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()
        if sha != value.get('sha') or len(content) != value['size']:
            raise ReviewError('文件内容未通过 Git 对象校验')
        return content

    def file_index(self, sha):
        if getattr(self,'max_file_bytes',0)>128*1024:
            value=self.context_tree(sha)
            if not value['complete']:raise ReviewError('固定版本目录索引未完成，可按需继续查找头文件')
            return list(value['files'])
        cache = getattr(self, '_file_indexes', {})
        if sha not in cache:
            value = self.request('GET', self.prefix+'/git/trees/'+valid_sha(sha), params={'recursive':True}, limit=4*1024*1024)
            if not isinstance(value, dict) or value.get('truncated') is not False or not isinstance(value.get('tree'), list):
                raise ReviewError('固定版本目录索引不完整，不能推测头文件路径')
            cache[sha] = [safe_path(r['path']) for r in value['tree'] if r.get('type') == 'blob']
            self._file_indexes = cache
        return cache[sha]

    def context_tree(self, sha):
        """目录分页与代码读取分离，明确报告尚未枚举的范围。"""
        import time
        rows={};total=None;seen=0;tree_sha=None;paths=set();started=time.monotonic()
        for page in itertools.count(1):
            value=self.request('GET',self.prefix+'/git/trees/'+valid_sha(sha),
                               params={'recursive':True,'page':page,'per_page':100},limit=2*1024*1024)
            if not isinstance(value,dict) or not isinstance(value.get('tree'),list):
                raise ReviewError('仓库目录返回格式无效')
            count=value.get('total_count')
            if type(count) is not int or count<0 or total is not None and count!=total:
                raise ReviewError('仓库目录分页计数不一致')
            current_sha=valid_sha(value.get('sha'))
            if tree_sha is not None and tree_sha!=current_sha:raise ReviewError('仓库目录分页对象发生变化')
            tree_sha=current_sha
            total=count;entries=value['tree'];seen+=len(entries)
            for row in entries:
                if not isinstance(row,dict):raise ReviewError('仓库目录条目格式无效')
                path=safe_path(row.get('path'))
                if path in paths:raise ReviewError('仓库目录分页重复，停止错误完整性判断')
                paths.add(path)
                if row.get('type')=='blob' and row.get('mode') in ('100644','100755'):
                    rows[path]={'sha':valid_sha(row['sha']),'size':row.get('size',0)}
            if seen>total:raise ReviewError('仓库目录条目数超过声明总数')
            if seen>=total:
                return {'files':rows,'complete':True}
            if not entries:break
        return {'files':rows,'complete':False}

    def pages(self, path, limit=50, max_pages=100):
        if path.endswith('/timeline'):
            raise ReviewError('Gitea 时间线分页会过滤原始记录，不能作为完整定位依据')
        if re.search(r'/issues/[1-9][0-9]*/comments$',path):
            # 1.26.4 普通评论接口忽略分页参数，直接返回完整集合。
            rows = self.request('GET',path)
            if not isinstance(rows,list) or self.response_total != len(rows):
                raise ReviewError('Gitea 普通评论集合未通过完整性校验')
            return rows
        if self.page_limit is None:
            settings = self.request('GET','/settings/api')
            value = settings.get('max_response_items')
            if type(value) is not int or value < 1:
                raise ReviewError('Gitea 未返回可靠的分页大小限制')
            self.page_limit = min(value,50)
        limit = min(limit,self.page_limit)
        result = []
        for page in range(1, max_pages+1):
            rows = self.request('GET', path, params={'page': page, 'limit': limit})
            if rows is None and self.response_total is not None:
                rows = []  # 当前页可能全部是对该身份不可见的待提交 Review。
            if not isinstance(rows, list):
                raise ReviewError('Gitea 分页结果格式无效')
            result.extend(rows)
            if (self.response_total is not None and page*limit>=self.response_total
                    or self.response_total is None and len(rows)<limit):
                return result
        raise ReviewError('Gitea 分页超过上限，不能把部分列表当成完整结果')


def version_of(pr):
    if pr.get('state') != 'open' or pr.get('merged'):
        raise Stale('PR 已关闭或合并')
    return {'base': valid_sha(pr['base']['sha']), 'head': valid_sha(pr['head']['sha']),
            'merge_base': valid_sha(pr['merge_base'])}


def assert_version(client, number, snapshot):
    current = version_of(client.pr(number))
    if any(current[key] != snapshot[key] for key in current):
        raise Stale('PR 代码版本已更新，禁止发布旧定位结果')
    return current

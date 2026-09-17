"""Gitea 1.26.4 专用适配器；不跟随重定向，不记录远端响应正文。"""
import base64
import hashlib
import re
from urllib.parse import quote

import requests

from .common import ReviewError, Stale, PublishUnknown, WriteRejected, endpoint, safe_path, valid_sha


class Gitea:
    def __init__(self, url, token, repo_name=None, repo_id=None, guard=None):
        self.url = endpoint(url, 'AI_REVIEW_GITEA_HOSTS')
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

    def request(self, method, path, data=None, params=None, raw=False, missing=False, limit=4*1024*1024):
        if self.guard:
            self.guard()
        try:
            with self.session.request(method, self.url + '/api/v1' + path, json=data, params=params,
                                      timeout=(5, 30), allow_redirects=False, stream=True) as response:
                if response.status_code == 404 and missing:
                    return None
                if not 200 <= response.status_code < 300:
                    if method not in ('GET', 'HEAD') and response.status_code >= 500:
                        raise PublishUnknown('Gitea 写入返回服务错误，需要核对实际结果')
                    if method not in ('GET', 'HEAD') and 400 <= response.status_code < 500:
                        raise WriteRejected(f'Gitea 明确拒绝写入：HTTP {response.status_code}')
                    raise ReviewError(f'Gitea 请求失败：HTTP {response.status_code}')
                parts, size = [], 0
                for part in response.iter_content(65536):
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

    def version(self):
        version = self.request('GET', '/version').get('version')
        if version not in ('1.26.4','1.26.4+review.1'):
            raise ReviewError('当前发布适配器支持 Gitea 1.26.4（含现有 review.1 版本），其他版本需先完成兼容测试')
        return version

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
        value = self.request('GET', self.prefix + '/contents/' + quote(safe_path(path), safe='/'),
                             params={'ref': valid_sha(sha)}, missing=True, limit=400000)
        if value is None:
            raise ReviewError('固定版本中缺少所需文件')
        if (not isinstance(value, dict) or value.get('type') != 'file' or value.get('encoding') != 'base64'
                or value.get('size', 999999) > 128*1024):
            raise ReviewError('文件类型或大小不支持可靠读取')
        try:
            content = base64.b64decode(value.get('content', ''), validate=False)
        except ValueError:
            raise ReviewError('文件内容编码无效') from None
        sha = hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()
        if sha != value.get('sha') or len(content) != value['size']:
            raise ReviewError('文件内容未通过 Git 对象校验')
        return content

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

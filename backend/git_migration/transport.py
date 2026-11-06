"""限定来源、限制响应体并拒绝隐式重定向的迁移读取通道。"""
import json
import time
from urllib.parse import urlsplit

import requests

from svn_migration.common import MigrationError, require, source_url


class SourceHTTPError(MigrationError):
    def __init__(self, status):
        messages = {401: '来源认证失效，请检查凭据或提供访问令牌',
                    403: '来源拒绝读取，不能将权限不足视为空数据',
                    404: '来源接口或对象不存在，需要核对版本与读取范围',
                    429: '来源正在限流，请稍后重新盘点'}
        super().__init__(messages.get(status, f'来源接口返回 HTTP {status}'), 'source_http', 400)
        self.http_status = status


class Transport:
    def __init__(self, base, *, verify=True, tick=lambda: None, session=None,
                 max_bytes=16 * 1024 * 1024, deadline_seconds=1800):
        self.base = source_url(base, ('https', 'http'))
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.verify = verify
        self.tick = tick
        self.max_bytes = max_bytes
        self.deadline = time.monotonic() + deadline_seconds

    def close(self):
        self.session.close()

    def request(self, method, path, *, params=None, data=None, headers=None, auth=None):
        require(method in ('GET', 'POST'), '读取通道不允许此操作')
        require(isinstance(path, str) and path.startswith('/') and not path.startswith('//')
                and not urlsplit(path).scheme and not urlsplit(path).netloc
                and not any(c in path for c in ('\\', '\r', '\n', '\x00'))
                and '?' not in path and '#' not in path,
                '来源接口路径无效')
        require(method != 'POST' or path == '/oauth/token', '来源写入仅允许认证令牌交换')
        self.tick()
        require(time.monotonic() < self.deadline, '来源读取超过时间预算，请缩小范围后重试', 'capacity')
        response = None
        try:
            response = self.session.request(method, self.base + path, params=params, data=data,
                                            headers=headers or {}, auth=auth, verify=self.verify,
                                            timeout=(10, 30), allow_redirects=False, stream=True)
            if not 200 <= response.status_code < 300:
                if 300 <= response.status_code < 400:
                    raise MigrationError('来源发生重定向，请填写最终实例地址；未向跳转地址发送凭据', 'redirect')
                raise SourceHTTPError(response.status_code)
            content = bytearray()
            for chunk in response.iter_content(65536):
                self.tick()
                require(time.monotonic() < self.deadline, '来源读取超过时间预算', 'capacity')
                content.extend(chunk)
                require(len(content) <= self.max_bytes, '来源单次响应过大，已停止读取', 'capacity')
            try:
                value = json.loads(content)
            except (ValueError, UnicodeDecodeError):
                raise MigrationError('来源未返回有效 JSON，请核对实例地址和反向代理配置', 'source_format') from None
            return value, dict(response.headers)
        except requests.RequestException:
            # requests 异常可能包含令牌、带认证查询的 URL 或代理配置，不能直接返回。
            raise MigrationError('无法读取来源，请检查网络、证书和连接地址', 'connection') from None
        finally:
            if response is not None:
                response.close()

    def collection(self, path, *, params=None, headers=None, auth=None, key=None,
                   page_key='page', size_key='per_page', limit=100000, credentials=None, identity='id', paginated=True):
        rows, identities, seen_pages = [], set(), set()
        page = 1
        total = None
        received = 0
        while True:
            require(page not in seen_pages and len(seen_pages) < 10000,
                    '来源分页没有前进或超出读取上限', 'pagination')
            seen_pages.add(page)
            query = dict(params or {})
            if paginated:
                query.update({page_key: page, size_key: 100})
            current_auth = credentials() if credentials else {'headers': headers, 'auth': auth}
            value, response_headers = self.request('GET', path, params=query, **current_auth)
            entries = value.get(key) if key and isinstance(value, dict) else value
            require(isinstance(entries, list), '来源列表接口格式不兼容', 'source_format')
            received += len(json.dumps(entries, ensure_ascii=False).encode())
            require(received <= 32 * 1024 * 1024, '来源列表超过本次元数据容量，请缩小范围', 'capacity')
            if key and isinstance(value, dict):
                require(value.get('ok') is not False, '来源列表返回失败状态', 'source_format')
            h = {name.lower(): value for name, value in response_headers.items()}
            raw_total = h.get('x-total', h.get('x-total-count'))
            if raw_total is not None:
                require(str(raw_total).isdigit(), '来源分页总数无效', 'pagination')
                count = int(raw_total)
                require(total is None or total == count, '读取期间来源列表数量变化，请重新盘点', 'source_changed')
                total = count
                require(total <= limit, '来源列表超过本次读取上限', 'capacity')
            for row in entries:
                ident = row.get(identity) if isinstance(row, dict) else None
                require((type(ident) is int and ident > 0) or (isinstance(ident, str) and bool(ident)),
                        '来源对象缺少稳定 ID', 'source_format')
                require(ident not in identities, '来源分页重复对象，不能确认完整性', 'pagination')
                identities.add(ident)
                rows.append(row)
                require(len(rows) <= limit, '来源列表超过本次读取上限', 'capacity')
            if not paginated:
                require(not h.get('x-next-page'), '来源接口描述与分页响应不一致', 'pagination')
                break
            if not entries:
                break
            if 'x-next-page' in h:
                next_page = h['x-next-page']
                if not next_page:
                    break
                require(str(next_page).isdigit() and int(next_page) > page,
                        '来源下一页编号无效', 'pagination')
                page = int(next_page)
            else:
                # 不用 len < 请求页大小 推断结束：服务端可能静默降低页大小。
                page += 1
        require(total is None or len(rows) == total, '来源列表数量与分页总数不一致', 'pagination')
        return rows

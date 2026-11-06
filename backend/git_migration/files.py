"""来源二进制流式下载；凭据只发往已登记的来源，不跟随跳转。"""
import hashlib
from pathlib import Path
import shutil
import time
from urllib.parse import urlsplit, urljoin
import requests

from svn_migration.common import MigrationError, require
from .transport import SourceHTTPError


class SourceFiles:
    def __init__(self, provider, folder, tick=lambda: None):
        self.provider = provider
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.tick = tick

    def download(self, url, *, expected_size=None, expected_sha256=None, max_size=32 * 1024 ** 3, follow_same_origin=False):
        base = urlsplit(self.provider.http.base)
        target = urlsplit(url)
        require(target.scheme in ('http', 'https') and target.netloc == base.netloc
                and target.scheme == base.scheme and not target.username and not target.password
                and (target.path == base.path or target.path.startswith(base.path.rstrip('/') + '/'))
                and not target.fragment and not any(c in url for c in '\r\n\x00'),
                '附件地址不属于已登记来源，不能向其他地址转发凭据')
        require(expected_size is None or type(expected_size) is int and 0 <= expected_size <= max_size,
                '附件大小超出支持范围')
        key = hashlib.sha256(url.encode()).hexdigest()
        output, partial = self.folder / key, self.folder / (key + '.partial')
        require(not output.exists(), '附件快照已存在，请使用已记录回执核对，不能覆盖')
        credentials = self.provider.credentials()
        response = None
        try:
            destination = url
            for attempt in range(6):
                response = self.provider.http.session.get(destination, **credentials, stream=True,
                    allow_redirects=False, verify=self.provider.http.verify, timeout=(10, 60))
                if not follow_same_origin or response.status_code not in (301, 302, 303, 307, 308):
                    break
                require(attempt < 5, '附件跳转次数超限')
                destination = urljoin(destination, response.headers.get('Location', ''))
                parsed = urlsplit(destination)
                require(parsed.scheme == base.scheme and parsed.netloc == base.netloc
                        and not parsed.username and not parsed.password and not parsed.fragment
                        and parsed.path.startswith(base.path.rstrip('/') + '/')
                        and not any(c in destination for c in '\r\n\x00'), '附件跳转越出已登记来源，拒绝转发凭据')
                response.close()

            if response.status_code != 200:
                raise SourceHTTPError(response.status_code)
            sha, size = hashlib.sha256(), 0
            deadline = time.monotonic() + 7200
            with partial.open('xb') as stream:
                for chunk in response.iter_content(1024 * 1024):
                    self.tick()
                    require(time.monotonic() < deadline, '附件下载超过两小时执行预算', 'timeout')
                    require(shutil.disk_usage(self.folder).free >= 4 * 1024 ** 3, '附件工作盘剩余不足 4 GiB', 'disk')
                    size += len(chunk)
                    require(size <= max_size and (expected_size is None or size <= expected_size), '来源附件超出声明大小')
                    sha.update(chunk)
                    stream.write(chunk)
            require(expected_size is None or size == expected_size, '来源附件内容长度与清单不符')
            require(expected_sha256 is None or sha.hexdigest() == expected_sha256, '来源附件摘要与清单不符')
            partial.rename(output)
            return {'file': str(output), 'size': size, 'sha256': sha.hexdigest()}
        except requests.RequestException:
            raise MigrationError('来源附件读取中断，未形成完整文件回执', 'connection') from None
        finally:
            if response is not None:
                response.close()

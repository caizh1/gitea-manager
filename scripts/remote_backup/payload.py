"""已知长度的大文件 multipart 流，避免一次性载入备份正文。"""
from pathlib import Path


def archive_budget(sizes, future_backups=0):
    """保留已有产物，并为下一轮四份临时副本及后续归档预留实际空间。"""
    if future_backups < 0:
        raise ValueError('后续备份次数不能为负数')
    largest = max(sizes, default=0)
    return max(10 * 1024**3, largest * (4 + future_backups) + 6 * 1024**3)


class MultipartArchive:
    def __init__(self, path, boundary):
        self.path = Path(path)
        self.prefix = ('--%s\r\nContent-Disposition: form-data; name="file"; filename="offline.zip"\r\n'
                       'Content-Type: application/zip\r\n\r\n' % boundary).encode()
        self.suffix = ('\r\n--%s--\r\n' % boundary).encode()

    def __len__(self):
        return len(self.prefix) + self.path.stat().st_size + len(self.suffix)

    def __iter__(self):
        yield self.prefix
        with self.path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                yield block
        yield self.suffix

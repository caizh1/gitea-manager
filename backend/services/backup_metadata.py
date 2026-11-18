"""备份 JSON 保持原格式，通过文件流写入归档。"""
from contextlib import contextmanager
import errno
import io
import json
import os


class MetadataResourceError(RuntimeError):
    """写入或解析备份元数据时系统资源不足或发生 I/O 故障。"""


def write_json(stream, value):
    text = io.TextIOWrapper(stream, encoding='utf-8', newline='\n')
    try:
        json.dump(value, text, ensure_ascii=False)
        text.flush()
    finally:
        text.detach()


@contextmanager
def metadata_io(action, size=None):
    try:
        yield
    except (OSError, MemoryError) as error:
        if isinstance(error, MemoryError):
            resource = '内存不足'
        elif error.errno in (errno.ENOSPC, errno.EDQUOT):
            resource = '磁盘可用空间或配额不足'
        else:
            resource = os.strerror(error.errno) if error.errno is not None else str(error)
        amount = size() if callable(size) else size
        detail = f'（{amount} 字节）' if amount is not None else ''
        raise MetadataResourceError(f'{action}{detail}失败：{resource}') from error


def write_zip_json(archive, name, value):
    with metadata_io(f'写入 {name}'):
        with archive.open(name, 'w', force_zip64=True) as stream:
            write_json(stream, value)


def read_zip_json(archive, name):
    with metadata_io(f'读取 {name}', archive.getinfo(name).file_size):
        with archive.open(name) as stream:
            return json.load(stream)

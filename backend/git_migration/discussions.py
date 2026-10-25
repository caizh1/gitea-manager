"""从固定 Git 对象恢复历史行内讨论的差异，不读取目标当前分支来猜测位置。"""
import re
from pathlib import PurePosixPath
from svn_migration.common import require


def historical_diff(process, directory, position):
    base, head = position.get('base_sha'), position.get('head_sha')
    require(all(isinstance(value, str) and re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', value)
                for value in (base, head)), '历史讨论缺少有效的 Git 提交')
    paths = list(dict.fromkeys([position.get('old_path'), position.get('new_path')]))
    require(all(isinstance(path, str) and path and not path.startswith('/')
                and '..' not in PurePosixPath(path).parts and '\\' not in path and '\x00' not in path
                for path in paths), '历史讨论文件路径无效')
    output = process.run(['git', '--literal-pathspecs', 'diff', '--no-ext-diff', '--no-textconv', '--no-color',
                          '--unified=3', base, head, '--', *paths], cwd=directory)
    require(len(output) <= 4 * 1024 * 1024, '历史讨论差异超过接收容量')
    try:
        return output.decode('utf-8')
    except UnicodeDecodeError:
        require(False, '历史讨论差异不是有效 UTF-8，不能无损转换')

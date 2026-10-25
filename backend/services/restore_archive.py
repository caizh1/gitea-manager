"""安全解压并保留普通权限，尤其是不会被 Gitea 重建的自定义 Hook。"""
import hashlib
import io
from pathlib import PurePosixPath
import stat
import tarfile


def validate_ssh_archive(raw):
    if len(raw) > 2 * 1024 * 1024:
        raise RuntimeError('SSH 主机身份归档超出支持上限')
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        seen = set()
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if (path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != 'ssh'
                    or not (member.isfile() or member.isdir()) or str(path) in seen):
                raise RuntimeError('SSH 主机身份归档包含不安全条目')
            if path.name.startswith('ssh_host_') and path.name.endswith('_key') and member.mode & 0o077:
                raise RuntimeError('SSH 主机私钥权限不安全，拒绝生成灾备包')
            seen.add(str(path))


def validate_members(members):
    seen = set()
    for entry in members:
        path = PurePosixPath(entry.filename)
        mode = entry.external_attr >> 16
        if (path.is_absolute() or '..' in path.parts or '\\' in entry.filename
                or str(path) in seen or str(path) == '.'
                or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
            raise RuntimeError('备份包含不安全路径、重复路径或特殊文件，拒绝恢复')
        seen.add(str(path))


def restore_modes(workspace, members):
    # 目录权限最后处理，避免只读目录阻断其子文件解压；不恢复特殊权限位。
    for entry in sorted(members, key=lambda x: len(PurePosixPath(x.filename).parts), reverse=True):
        mode = (entry.external_attr >> 16) & 0o777
        if entry.create_system == 3 and mode:
            (workspace / entry.filename).chmod(mode)


def custom_hooks(root):
    result = {}
    generated = {'pre-receive', 'update', 'post-receive', 'proc-receive'}
    for hooks in root.glob('**/*.git/hooks'):
        for path in hooks.rglob('*'):
            if not path.is_file():
                continue
            relative = path.relative_to(hooks)
            if str(relative) in generated or (len(relative.parts) == 2
                    and relative.parts[0] in {name + '.d' for name in generated} and relative.name == 'gitea'):
                continue
            result[str(path.relative_to(root))] = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mode & 0o777)
    return result


def verify_hooks(helper, hooks):
    from services.restore_secrets import read_file
    for path, (digest, mode) in hooks.items():
        raw, member = read_file(helper, '/data/git/repositories/' + path)
        if hashlib.sha256(raw).hexdigest() != digest or member.mode & 0o777 != mode:
            raise RuntimeError('恢复后的自定义 Hook 内容或权限不一致：' + path)

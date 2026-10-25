"""修改真实有效归档，验证遗漏正文、权限变化和损坏会在导入前被拒绝。"""
import hashlib
import io
import json
import shutil
import time
import zipfile

from .errors import CheckFailure
from .payload import MultipartArchive


def synthetic_archive(extra=()):
    """只用于导入安全边界的合成归档；正向对照具备完整清单和空停写快照。"""
    snapshot = json.dumps({'版本': 1, '数据库仓库': [], 'Git仓库': {}}, ensure_ascii=False).encode()
    entries = [('manager-snapshot.json', snapshot, 0o100600),
               ('safety-proof.txt', '合成归档导入安全边界，不代表真实备份验收'.encode(), 0o100600), *extra]
    body = io.BytesIO()
    with zipfile.ZipFile(body, 'w') as archive:
        files = {}
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = mode << 16
            archive.writestr(info, data)
            files[name] = {'摘要': hashlib.sha256(data).hexdigest(), '权限': mode, '系统': 3}
        manifest = {'版本': 2, '准入': {}, '数据表': {}, '文件': files,
                    '来源名称': '合成归档导入安全对照', 'API令牌': 'remote-lab-only-unusable-token'}
        archive.writestr('manager-manifest.json', json.dumps(manifest, ensure_ascii=False))
    return body.getvalue()


def rewrite(source, target, victim, mode):
    if mode == 'truncate':
        # 从真实完整归档流式复制，删除 ZIP 尾部；不能用几字节假头替代真实截断。
        remaining = source.stat().st_size - 4096
        if remaining <= 0:
            raise CheckFailure('真实归档小于截断样本下限')
        with source.open('rb') as incoming, target.open('wb') as outgoing:
            while remaining:
                block = incoming.read(min(1024 * 1024, remaining))
                if not block:
                    raise CheckFailure('截断样本复制时源文件长度变化')
                outgoing.write(block)
                remaining -= len(block)
        target.chmod(0o600)
        return
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(target, 'w') as changed:
        for info in original.infolist():
            selected = info.filename in victim if isinstance(victim, set) else info.filename == victim
            if selected and mode == 'missing':
                continue
            if selected and mode == 'permissions':
                info.external_attr ^= 0o100 << 16
            with original.open(info.filename) as incoming, changed.open(info, 'w', force_zip64=True) as outgoing:
                if selected and mode == 'bytes':
                    first = incoming.read(1)
                    if not first:
                        raise CheckFailure('待修改正文为空，不能宣称实际字节损坏')
                    outgoing.write(bytes([first[0] ^ 1]))
                shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
            if selected and mode == 'volume':
                # 只膨胀中央目录的申报体积；实际正文不膨胀，避免消耗宿主巨量空间。
                info.file_size = 0xF0000000
    target.chmod(0o600)


def run(lab, modes=None):
    from .scenarios import download_backup, malformed_archives
    lab.login()
    if 'downloaded_backup' not in lab.state:
        download_backup(lab, lab.state['backup_id'])
    from pathlib import Path
    source = Path(lab.state['downloaded_backup'])
    if not source.exists():
        raise CheckFailure('需要真实有效备份作为变异基线')
    with zipfile.ZipFile(source) as archive:
        paths = [info.filename for info in archive.infolist() if not info.is_dir()]
    specs = [('真实完整 ZIP 尾部截断', lambda p: True, 'truncate'),
             ('缺少 SQL', lambda p: p.endswith('gitea-db.sql'), 'missing'),
             ('缺少仓库 pack', lambda p: p.startswith('repos/') and p.endswith('.pack'), 'missing'),
             ('缺少软件包正文', lambda p: p.startswith('data/packages/'), 'missing'),
             ('缺少 LFS 正文', lambda p: p.startswith('data/lfs/'), 'missing'),
             ('缺少 Actions 制品', lambda p: p.startswith('data/actions_artifacts/'), 'missing'),
             ('缺少 Actions 日志', lambda p: p.startswith('data/actions_log/'), 'missing'),
             ('真实正文单字节修改', lambda p: p.startswith('repos/') and p.endswith('/HEAD'), 'bytes'),
             ('正文权限与清单不同', lambda p: p.startswith('repos/') and p.endswith('/HEAD'), 'permissions')]
    evidence = []
    for index, (label, match, mode) in enumerate(specs):
        if modes is not None and mode not in modes:
            continue
        with lab.case('archive-partial', label):
            lab.ensure_space()
            victim = next((path for path in paths if match(path)), None)
            if not victim:
                raise CheckFailure('有效备份不包含待破坏样本：' + label)
            target = lab.root / 'backups' / ('mutant-%s-%s.zip' % (index, time.time_ns()))
            rewrite(source, target, victim, mode)
            digest = hashlib.file_digest(target.open('rb'), 'sha256').hexdigest()
            boundary = 'remote-lab-corruption'
            response = lab.manager_api('POST', '/backups/import', data=MultipartArchive(target, boundary),
                headers={'Content-Type': 'multipart/form-data; boundary=' + boundary}, expected=None, timeout=1800)
            entry = {'说明': label, '变异对象': '真实归档尾部 4096 字节' if mode == 'truncate' else victim,
                     '源归档字节': source.stat().st_size, '变异归档字节': target.stat().st_size,
                     '变异归档SHA256': digest, 'HTTP状态': response.status_code}
            evidence.append(entry)
            target.with_suffix('.json').write_text(json.dumps(entry, ensure_ascii=False, indent=2) + '\n')
            if response.status_code not in (400, 409, 422):
                raise CheckFailure('损坏的真实备份未被拒绝；保留变异归档')
            target.unlink()
    (lab.root / ('archive-mutations-' + str(time.time_ns()) + '.json')).write_text(json.dumps({'说明': '真实归档变异验证，成功拒绝后仅删除临时变异副本', '结果': evidence}, ensure_ascii=False, indent=2) + '\n')
    if modes is not None:
        return
    malformed_archives(lab)
    oversized_volume(lab, source, paths)


def oversized_volume(lab, source, paths):
    """验证申报解压体积的预检；不执行真实超大解压，不能冒充物理盘耗尽。"""
    from . import oracle
    from .network import control
    from .errors import EnvironmentBlocked
    with lab.case('archive-partial', '申报解压体积超出可用空间时拒绝且原目标不变'):
        lab.ensure_space()
        before = oracle.snapshot(lab, 'b')
        target = lab.root / 'backups' / ('oversized-' + str(time.time_ns()) + '.zip')
        selected = {path for path in paths if path.startswith('repos/')}
        rewrite(source, target, selected, 'volume')
        with zipfile.ZipFile(target) as archive:
            declared = sum(item.file_size for item in archive.infolist())
        # Manager 临时盘在 Docker VM；只有申报值确实超过它的空间才认为命中。
        space = control(lab, 'manager-space')['证据']
        if declared * 2 + 64 * 1024**2 <= space['空闲字节']:
            raise EnvironmentBlocked('申报体积未超过实际 Manager 临时盘空间，未命中超限预检，不执行恢复')
        boundary = 'remote-lab-volume'
        response = lab.manager_api('POST', '/backups/import', data=MultipartArchive(target, boundary),
            headers={'Content-Type': 'multipart/form-data; boundary=' + boundary}, expected=None, timeout=1800)
        evidence = {'说明': '仅修改申报体积，不实际产生超大正文；不代表真实物理磁盘故障',
            '申报解压字节': declared, 'Manager临时盘': space,
            '变异归档字节': target.stat().st_size, '导入HTTP状态': response.status_code}
        imported = None
        if response.status_code == 201:
            imported = response.json()['id']
            task = lab.restore('b', imported)
            evidence.update(导入ID=imported, 恢复ID=task['id'], 任务状态=task['status'], 错误=lab.scrub(task.get('error_msg', '')))
            safe = task['status'] == 'failed' and '临时空间不足' in task.get('error_msg', '')
        else:
            safe = response.status_code in (400, 409, 422)
        evidence['差异'] = oracle.compare(before, oracle.snapshot(lab, 'b'))
        target.with_suffix('.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
        if not safe or evidence['差异']:
            raise CheckFailure('异常解压体积没有明确拒绝或原目标发生变化，保留变异归档')
        if imported is not None:
            lab.manager_api('DELETE', '/backups/%s' % imported)
        target.unlink()

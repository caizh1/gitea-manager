#!/usr/bin/env python3
"""隔离验证真实来源、原生对象写入、重试及目标代码重新克隆。"""
import argparse
import importlib.util
import json
from pathlib import Path
import secrets
import socket
import sys
import time
from types import SimpleNamespace
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from git_migration.providers import Provider
from git_migration.transport import Transport
from git_migration.inventory import Inventory
from git_migration.normalize import objects
from git_migration.git_snapshot import GitSnapshot
from git_migration.target import Receiver
from svn_migration.common import digest, MigrationError
from git_migration.assets import plan_assets, transfer_assets
from git_migration.discussions import historical_diff
from git_migration.packages import package_assets, verification_plan
from git_migration import rules
from svn_migration.engine import Process

spec = importlib.util.spec_from_file_location('source_fixture', Path(__file__).with_name('git-migration-source-acceptance.py'))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
docker = fixture.docker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--project')
    parser.add_argument('--handoff', action='store_true')
    parser.add_argument('--initialize-only', action='store_true', help='只初始化独立接收端，后续由完整 Manager 链路写入')
    parser.add_argument('--source-state', required=True, type=Path)
    parser.add_argument('--resume-state', type=Path)
    args = parser.parse_args()
    source = json.loads(args.source_state.read_text())
    details = json.loads(docker('inspect', source['name']))[0]
    if details['Config']['Labels'].get('io.gitea-manager.test') != 'git-migration-source':
        raise RuntimeError('仅接受本项目隔离来源夹具')
    if args.resume_state:
        state = json.loads(args.resume_state.read_text())
        folder = args.resume_state.parent
        details = json.loads(docker('inspect', state['name']))[0]
        if details['Config']['Labels'].get('io.gitea-manager.test') != 'git-migration-receiver' or details['Image'] != json.loads(docker('image', 'inspect', args.image))[0]['Id']:
            raise RuntimeError('接收端标签或镜像与指定版本不符')
    else:
        name = 'gm-git-receiver-' + time.strftime('%Y%m%d%H%M%S') + '-' + secrets.token_hex(2)
        folder = ROOT / 'work' / 'git-migration' / name
        folder.mkdir(parents=True, mode=0o700)
        with socket.socket() as port_probe:
            port_probe.bind(('127.0.0.1',0)); port=port_probe.getsockname()[1]
        state = {'说明': '独立接收端验收资源与凭据，禁止发布', 'name': name, 'password': secrets.token_urlsafe(28),'base':'http://127.0.0.1:'+str(port)}
        save_secret(folder / 'resources.json', state)
        label = 'io.gitea-manager.test=git-migration-receiver'
        docker('volume', 'create', '--label', label, name)
        docker('run', '-d', '--platform', 'linux/amd64', '--name', name, '--label', label,
               '--memory', '768m', '--cpus', '2', '-v', name + ':/data', '-p', '127.0.0.1:'+str(port)+':3000',
               '-e', 'GITEA__database__DB_TYPE=sqlite3', '-e', 'GITEA__security__INSTALL_LOCK=true',
               '-e', 'GITEA__service__DISABLE_REGISTRATION=true', '-e', 'GITEA__server__LFS_START_SERVER=true',
               '-e', 'GITEA__server__ROOT_URL='+state['base']+'/', args.image)
        state['base'] = 'http://127.0.0.1:' + docker('port', name, '3000/tcp').rsplit(':', 1)[1]
        save_secret(folder / 'resources.json', state)
    for attempt in range(120):
        try:
            if requests.get(state['base'] + '/api/v1/version', timeout=2).status_code == 200:
                break
        except requests.RequestException:
            pass
        time.sleep(1)
    else:
        raise RuntimeError('接收端未在两分钟内就绪')
    if not state.get('token'):
        docker('exec', '--user', 'git', state['name'], 'gitea', 'admin', 'user', 'create',
               '--admin', '--username', 'migration-admin', '--password', state['password'],
               '--email', 'migration-admin@example.invalid', '--must-change-password=false')
        response = requests.post(state['base'] + '/api/v1/users/migration-admin/tokens',
            auth=('migration-admin', state['password']), json={'name': '隔离迁移验收', 'scopes': ['all']}, timeout=30)
        if response.status_code != 201:
            raise RuntimeError('接收端初始化令牌失败，HTTP ' + str(response.status_code))
        state['token'] = response.json()['sha1']
        save_secret(folder / 'resources.json', state)
    if args.initialize_only:
        print('独立接收端已初始化：' + str(folder / 'resources.json'), flush=True)
        return
    run_folder = folder / ('run-' + secrets.token_hex(8))
    run_folder.mkdir(mode=0o700)
    source_http = Transport(source['base'])
    provider = Provider(source_http, source['kind'], 'root' if source['kind'] == 'gitlab' else 'source-admin', source['password'])
    target = Receiver(SimpleNamespace(gitea_url=state['base'], api_token=state['token']),
                      Process(run_folder / 'commands', secrets=(state['token'],)), run_folder)
    results = []
    try:
        discovery = provider.discover()
        target.check()
        selection = None
        root_checkpoint = {'key': secrets.token_hex(16)}
        if args.handoff:
            group = target.api('POST', '/governance/groups', {'parent_id': 0, 'path': 'migration-' + root_checkpoint['key'][:12], 'name': '迁移验收接收群组', 'visibility': 2})
            selection = target.selected_scope(group['id'])
        for project in discovery['projects']:
            if args.project and project['path'] != args.project:
                continue
            local = run_folder / str(project['id'])
            inventory = Inventory(provider).inspect(project)
            snapshot = GitSnapshot(provider, project, local).capture()
            checkpoint = {'snapshot': snapshot}
            def save(value):
                (local / 'checkpoint.json').write_text(json.dumps(value, ensure_ascii=False, indent=2))
            key = secrets.token_hex(16)
            normalized = objects(inventory, source['kind'], resolve_tag=lambda tag: target.process.run(
                ['git', 'rev-parse', '--verify', 'refs/tags/' + tag + '^{commit}'], cwd=local / 'repository.git').decode().strip(),
                resolve_diff=lambda position: historical_diff(target.process, local / 'repository.git', position))
            assets = plan_assets(inventory, source['kind'], normalized, key, provider.http.base, target.base)
            assets.extend(package_assets(inventory, source['kind'], normalized, provider.http.base))
            checkpoint['package_files'] = verification_plan(normalized, assets)
            mapping = rules.validate_mapping({k: ['migration-admin'] for k in rules.requirements({'projects': [inventory]})},
                                             rules.requirements({'projects': [inventory]}), target)
            checkpoint['rule_plan'] = rules.convert(inventory, source['kind'], mapping, normalized)
            checkpoint['principal_map'] = mapping
            repo = target.stage(key, digest([snapshot, inventory]), checkpoint, save)
            target.transfer_code(snapshot, local, repo, checkpoint, save, [o for o in normalized['objects'] if o['kind'] == 'pull'])
            target.import_objects(key, normalized['objects'], checkpoint, save)
            first = dict(checkpoint['objects'])
            target.import_objects(key, normalized['objects'], checkpoint, save)
            if first != checkpoint['objects']:
                raise RuntimeError('重复导入产生了不同回执')
            wiki = next((d for d in inventory['datasets'] if d['category'] == 'Wiki 页面'), None)
            if wiki is not None:
                try:
                    target.transfer_wiki(provider, project, key, repo, local / 'wiki', checkpoint, save)
                    normalized['pending'] = [p for p in normalized['pending'] if p['category'] != 'Wiki 页面']
                except MigrationError as error:
                    if error.code != 'git_read' or wiki['rows']:
                        raise
                    normalized['pending'].append({'category': 'Wiki 历史', 'reason': '空页面的 Wiki Git 不可读，无法排除历史资料'})
            transfer_assets(provider, target, key, assets, local / 'files', checkpoint, save)
            target.verify_code(snapshot, repo, local / 'target-clone', checkpoint, save)
            checkpoint['key'] = key
            checkpoint['pending'], checkpoint['gaps'] = normalized['pending'], normalized['gaps']
            if selection:
                from git_migration.handoff import finish
                GitSnapshot(provider, project, local / 'source-final').verify_refs(snapshot)
                if checkpoint.get('wiki_snapshot'):
                    GitSnapshot(provider, project, local / 'wiki-final', wiki=True).verify_refs(checkpoint['wiki_snapshot'])
                finish(target, selection, project, checkpoint, root_checkpoint, lambda: save(checkpoint))
            results.append({'来源': project['path'], '目标': checkpoint.get('final_path', repo['full_name']), '最终交接': bool(checkpoint.get('handed_off')), '原生对象数': len(first),
                            'LFS对象数': len(snapshot['lfs_objects']), '附件数': len(assets), '重复导入一致': True,
                            '来源尚待支持': normalized['pending'], '来源读取缺口': normalized['gaps']})
    finally:
        source_http.close()
        target.close()
    if not results:
        raise RuntimeError('未选择到真实验收仓库')
    output = {'说明': '代表性真实样本写入与交接验收，不代表全范围、生产或人工节省指标', '结果': '通过',
              '镜像': args.image, '项目': results, '目标地址': state['base']}
    (run_folder / 'acceptance.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
    print('真实写入与目标回读验收通过：' + str(run_folder / 'acceptance.json'), flush=True)


def save_secret(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)


if __name__ == '__main__':
    main()

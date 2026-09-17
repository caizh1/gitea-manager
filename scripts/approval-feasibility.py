#!/usr/bin/env python3
"""外接审批第一阶段反证：只创建独立官方实例，不接受已有实例地址。

此程序是验收工具，不是生产审批服务。同步点刻意放在最后一次校验之后、
Git 写入之前，用真实 REST 操作改变审批或组成员，检验严格时序要求。
退出码：0 表示本次反证未触发；2 表示发现强制接入阻断；1 表示实验未完成。
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import traceback
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler


ROOT = Path(__file__).resolve().parents[1]
LABEL = 'com.gitea-manager.approval-feasibility'
STYLES = ('merge', 'rebase', 'rebase-merge', 'squash', 'fast-forward-only')
SCENARIOS = ('正常批准', '提前撤回', '预接收后撤回', '更新检查后撤回', '更新检查后移出组')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('隔离 API 出现重定向，已停止，未转发凭据')


def write_json(path, data):
    temp = path.with_name(path.name + '.写入中')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


class Probe:
    def __init__(self):
        self.run_id = time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(4)
        self.project = 'gm-approval-probe-' + self.run_id
        self.folder = ROOT / 'work' / ('审批可行性-' + self.run_id)
        self.folder.mkdir(parents=True, mode=0o700)
        self.control = self.folder / '同步点'
        self.control.mkdir(mode=0o777)
        self.control.chmod(0o777)  # 仅供隔离容器 git 用户写入；父目录保持 0700。
        self.compose_path = self.folder / '隔离环境.json'
        self.report_path = self.folder / '可行性结果.json'
        self.password = secrets.token_urlsafe(32)
        self.container = None
        self.base = None
        self.teams = {}
        self.report = {'说明': '官方镜像独立实例第一阶段反证，不代表完整审批功能或生产验收。',
                       '运行标识': self.run_id, '状态': '准备中', '检查': [], '阻断': [],
                       '未执行': ['生产接入矩阵', '网页与自动合并入口', '代码更新竞争',
                                 '影子和强制模式各24小时', '现场负载与人工精力指标']}
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.save()

    def save(self):
        write_json(self.report_path, self.report)

    def run(self, args, *, input=None, timeout=60):
        result = subprocess.run(args, input=input, capture_output=True, text=True,
                                cwd=ROOT, timeout=timeout)
        if result.returncode:
            # 不输出含口令的命令参数或未经筛选的容器配置。
            detail = result.stderr.replace(self.password, '[已隐藏]')[-900:]
            raise RuntimeError(f'{args[0]} 执行失败（{result.returncode}）：{detail}')
        return result.stdout

    def compose(self, *args):
        return self.run(['docker', 'compose', '-p', self.project, '-f',
                         str(self.compose_path), *args], timeout=180)

    def verify_owned(self):
        info = json.loads(self.run(['docker', 'inspect', self.container]))[0]
        if info['Config']['Labels'].get(LABEL) != self.run_id:
            raise RuntimeError('实例标记不匹配，拒绝操作')
        return info

    def execute(self, *args, input=None):
        self.verify_owned()
        return self.run(['docker', 'exec', '-i', '--user', 'git', self.container,
                         *args], input=input)

    def api(self, method, path, data=None, user='probe-admin', expected=(200, 201, 204)):
        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('API 路径无效')
        headers = {'Content-Type': 'application/json', 'Authorization': 'Basic ' +
                   base64.b64encode((user + ':' + self.password).encode()).decode()}
        req = Request(self.base + '/api/v1' + quote(path, safe='/?=&'), method=method, headers=headers,
                      data=json.dumps(data).encode() if data is not None else None)
        try:
            response = self.opener.open(req, timeout=35)
        except HTTPError as exc:
            response = exc
        with response:
            body = response.read()
            value = json.loads(body) if body else None
            if response.code not in expected:
                raise RuntimeError(f'{method} {path} 返回 {response.code}：{str(value)[:500]}')
            return response.code, value

    def get(self, path):
        return self.api('GET', path)[1]

    def start(self):
        network_ids = self.run(['docker', 'network', 'ls', '-q']).split()
        networks = json.loads(self.run(['docker', 'network', 'inspect', *network_ids]))
        used = [ipaddress.ip_network(c['Subnet']) for n in networks
                for c in (n.get('IPAM', {}).get('Config') or []) if c.get('Subnet')]
        subnet = next((str(candidate) for candidate in ipaddress.ip_network('10.253.240.0/20').subnets(new_prefix=28)
                       if not any(n.version == 4 and candidate.overlaps(n) for n in used)), None)
        if not subnet:
            raise RuntimeError('没有可用的独立实验网段，未改动已有网络')
        self.report['隔离网段'] = subnet
        images = {}
        for name in ('gitea/gitea:1.26.4', 'postgres:17.11-bookworm'):
            info = json.loads(self.run(['docker', 'image', 'inspect', name]))[0]
            if info['Architecture'] != 'amd64' or info['Os'] != 'linux':
                raise RuntimeError('本阶段要求本机已有 Linux amd64 官方镜像')
            digest = next((v for v in info.get('RepoDigests', [])
                           if v.startswith(name.split(':')[0] + '@sha256:')), None)
            if not digest:
                raise RuntimeError('镜像缺少官方仓库摘要，未启动实验')
            images[name] = {'摘要': digest, '本地ID': info['Id']}
        self.report['镜像'] = images
        pg_env = {'POSTGRES_DB': 'gitea', 'POSTGRES_USER': 'gitea',
                  'POSTGRES_PASSWORD': self.password}
        gitea_env = {
            'GITEA__database__DB_TYPE': 'postgres', 'GITEA__database__HOST': 'postgres:5432',
            'GITEA__database__NAME': 'gitea', 'GITEA__database__USER': 'gitea',
            'GITEA__database__PASSWD': self.password, 'GITEA__security__INSTALL_LOCK': 'true',
            'GITEA__service__DISABLE_REGISTRATION': 'true',
            'GITEA__server__ROOT_URL': 'http://127.0.0.1:3000/',
            'GITEA__repository__DEFAULT_BRANCH': 'main',
        }
        config = {'x-说明': '仅此运行创建的隔离实验；随机回环端口；禁止替换为生产卷。',
                  'services': {
                      'postgres': {'image': images['postgres:17.11-bookworm']['摘要'],
                                   'platform': 'linux/amd64', 'pull_policy': 'never',
                                   'labels': {LABEL: self.run_id}, 'environment': pg_env,
                                   'volumes': ['postgres-data:/var/lib/postgresql/data'],
                                   'mem_limit': '256m', 'cpus': 0.5,
                                   'healthcheck': {'test': ['CMD-SHELL', 'pg_isready -U gitea'],
                                                   'interval': '2s', 'timeout': '2s', 'retries': 60}},
                      'gitea': {'image': images['gitea/gitea:1.26.4']['摘要'],
                                'platform': 'linux/amd64', 'pull_policy': 'never',
                                'labels': {LABEL: self.run_id}, 'environment': gitea_env,
                                'depends_on': {'postgres': {'condition': 'service_healthy'}},
                                'ports': ['127.0.0.1::3000'],
                                'volumes': ['gitea-data:/data', str(self.control) + ':/approval-probe'],
                                'mem_limit': '768m', 'cpus': 1.0}},
                  'volumes': {'postgres-data': {}, 'gitea-data': {}},
                  'networks': {'default': {'ipam': {'config': [{'subnet': subnet}]}}}}
        write_json(self.compose_path, config)
        self.compose_path.chmod(0o600)
        self.compose('up', '-d', '--no-build', '--pull', 'never')
        self.container = self.compose('ps', '-q', 'gitea').strip()
        info = self.verify_owned()
        self.report['实例'] = {'容器': self.container, '初始启动时间': info['State']['StartedAt']}
        if not info['NetworkSettings']['Ports'].get('3000/tcp'):
            raise RuntimeError('独立回环端口未发布，实验未就绪')
        port = info['NetworkSettings']['Ports']['3000/tcp'][0]['HostPort']
        self.base = 'http://127.0.0.1:' + port
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                with self.opener.open(self.base + '/api/v1/version', timeout=2) as response:
                    version = json.load(response)['version']
                if version != '1.26.4':
                    raise RuntimeError('运行版本不是官方 1.26.4')
                break
            except (URLError, OSError):
                time.sleep(0.5)
        else:
            raise RuntimeError('隔离实例未就绪')
        self.report['实例'] = {'容器': self.container, '地址': self.base, '版本': version,
                               '初始启动时间': info['State']['StartedAt']}
        self.execute('gitea', '--config', '/data/gitea/conf/app.ini', 'admin', 'user',
                     'create', '--username', 'probe-admin', '--password', self.password,
                     '--email', 'probe-admin@example.invalid', '--admin',
                     '--must-change-password=false')
        self.report['状态'] = '运行中'
        self.save()
        print('隔离官方实例已就绪：' + self.base, flush=True)

    def fixtures(self):
        for name in ('author', 'alice', 'bob', 'carol', 'dave', 'spare'):
            self.api('POST', '/admin/users', {'username': name, 'password': self.password,
                     'email': name + '@example.invalid', 'must_change_password': False,
                     'send_notify': False})
        self.api('POST', '/orgs', {'username': 'approval-probe', 'visibility': 'private'})
        for name, members in (('Writers', ('author', 'alice', 'bob', 'carol', 'dave', 'spare')),
                              ('Committer1', ('alice', 'spare')), ('Committer2', ('bob',))):
            _, team = self.api('POST', '/orgs/approval-probe/teams', {
                'name': name, 'description': '隔离审批反证样本', 'permission': 'write',
                'includes_all_repositories': True, 'units_map': {'repo.code': 'write',
                                                                                'repo.pulls': 'write'}})
            self.teams[name] = team['id']
            for member in members:
                self.api('PUT', f'/teams/{team["id"]}/members/{member}')

    def record(self, result):
        self.report['检查'].append(result)
        if result['结论'] == '阻断':
            self.report['阻断'].append(result['编号'])
        self.save()
        print(result['编号'] + ' ' + result['场景'] + '：' + result['结论'], flush=True)

    def snapshot(self, endpoint):
        reviews = self.get(endpoint + '/reviews?limit=50')
        if len(reviews) >= 50:
            raise RuntimeError('固定样本超出单页上限，不能使用不完整审批列表')
        valid = {r['user']['id'] for r in reviews
                 if r['state'] == 'APPROVED' and not r['dismissed'] and r['official']}
        groups = {}
        for name in ('Committer1', 'Committer2'):
            members = self.get(f'/teams/{self.teams[name]}/members?limit=50')
            if len(members) >= 50:
                raise RuntimeError('固定样本团队超出单页上限')
            groups[name] = len(valid & {m['id'] for m in members})
        return {'有效总票数': len(valid), '各组票数': groups,
                '满足规则': len(valid) >= 3 and all(v >= 1 for v in groups.values()),
                '采集单调时间': time.monotonic(),
                '审批': [{'ID': r['id'], '用户': r['user']['login'], '状态': r['state'],
                          '已撤回': r['dismissed'], '原生有效': r['official'],
                          '提交': r['commit_id']} for r in reviews]}

    def install_probe(self, repo_name, case_dir):
        repo_dir = '/data/git/repositories/approval-probe/' + repo_name + '.git'
        hashes = {}
        for phase in ('pre-receive', 'update'):
            wrapper = repo_dir + '/hooks/' + phase
            hashes[phase] = self.execute('sha256sum', wrapper).split()[0]
            # 只记录明确列出的非秘密变量；不采集整个环境或凭据。
            script = '''#!/bin/sh
# 隔离实验同步点：不是生产门禁。等待超时一律拒绝。
set -eu
phase=PHASE
folder=FOLDER
if [ "$phase" = "pre-receive" ]; then
  cat > "$folder/$phase.引用"
else
  printf '%s %s %s\\n' "$2" "$3" "$1" > "$folder/$phase.引用"
fi
printf '%s\\n%s\\n%s\\n' "${GITEA_REPO_ID:-}" "${GITEA_PR_ID:-}" "${GITEA_PR_INDEX:-}" > "$folder/$phase.身份"
date -u +%Y-%m-%dT%H:%M:%SZ > "$folder/$phase.已进入"
deadline=$(($(date +%s) + 10))
wait_file() {
  while [ ! -f "$1" ]; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo '审批反证同步点超时，拒绝实验合并' >&2
      exit 1
    fi
    sleep 0.05
  done
}
wait_file "$folder/$phase.判定"
if [ "$(cat "$folder/$phase.判定")" != "允许" ]; then
  echo '审批反证：指定组票数不满足规则' >&2
  exit 1
fi
date -u +%Y-%m-%dT%H:%M:%SZ > "$folder/$phase.已检查"
wait_file "$folder/$phase.继续"
exit 0
'''.replace('PHASE', phase).replace('FOLDER', '/approval-probe/' + case_dir.name)
            # 暂存目录在 hooks/*.d 之外；仅向新建且带隔离标记的仓库安装。
            stage = repo_dir + '/approval-probe-' + phase
            target = repo_dir + '/hooks/' + phase + '.d/zz-approval-feasibility'
            self.execute('sh', '-c', 'cat > "$1" && chmod 755 "$1" && mv "$1" "$2"',
                         '安装隔离同步点', stage, target, input=script)
            if self.execute('sha256sum', wrapper).split()[0] != hashes[phase]:
                raise RuntimeError('原生 Hook 包装器发生变化，停止实验')
        return repo_dir, hashes

    def wait_file(self, path, future):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if path.exists():
                return
            if future.done():
                raise RuntimeError('合并在到达预期同步点前结束：' + str(future.result()))
            time.sleep(0.02)
        raise RuntimeError('同步点等待超时：' + path.name)

    def case(self, style, scenario, index):
        name = f'probe-{index:02d}'
        path = '/repos/approval-probe/' + name
        self.api('POST', '/orgs/approval-probe/repos', {'name': name, 'private': True,
                 'auto_init': True, 'default_branch': 'main', 'readme': 'Default',
                 'description': '隔离审批可行性样本：' + scenario})
        self.api('PATCH', path, {'allow_merge_commits': True, 'allow_rebase': True,
                 'allow_rebase_explicit': True, 'allow_squash_merge': True,
                 'allow_fast_forward_only_merge': True, 'allow_manual_merge': False})
        self.api('POST', path + '/branches', {'new_branch_name': 'feature', 'old_ref_name': 'main'}, user='author')
        _, commit = self.api('POST', path + '/contents/验收样本.txt', {
            'branch': 'feature', 'message': '测试：审批时序样本',
            'content': base64.b64encode(('审批样本：' + scenario + '\n').encode()).decode()}, user='author')
        head = commit['commit']['sha']
        _, pr = self.api('POST', path + '/pulls', {'title': '审批时序反证：' + scenario,
                                                'head': 'feature', 'base': 'main'}, user='author')
        endpoint = path + '/pulls/' + str(pr['number'])
        reviews = {}
        for user in ('alice', 'bob', 'carol', 'dave'):
            _, review = self.api('POST', endpoint + '/reviews', {'event': 'APPROVED',
                                'commit_id': head, 'body': '隔离样本审批通过'}, user=user)
            reviews[user] = review['id']
        self.api('POST', path + '/branch_protections', {'rule_name': 'main', 'priority': 1,
                 'enable_push': False, 'required_approvals': 3,
                 'block_admin_merge_override': True, 'block_on_rejected_reviews': True})
        baseline_rule = self.get(path + '/branch_protections')
        case_dir = self.control / name
        case_dir.mkdir(mode=0o777)
        case_dir.chmod(0o777)
        repo_dir, wrapper_hashes = self.install_probe(name, case_dir)
        deadline = time.monotonic() + 30
        while not self.get(endpoint)['mergeable']:
            if time.monotonic() >= deadline:
                raise RuntimeError('PR 基础可合并性未就绪')
            time.sleep(0.1)
        initial = self.snapshot(endpoint)
        if not initial['满足规则']:
            raise RuntimeError('初始审批样本不满足规则')
        base_before = self.execute('git', '--git-dir=' + repo_dir, 'rev-parse', 'refs/heads/main').strip()
        result = {'编号': f'可行性{index:02d}', '方式': style, '场景': scenario,
                  'PR': pr['number'], '仓库': name, '期望合入提交': head,
                  '初始审批': initial, '初始目标引用': base_before, '同步点': []}

        def mutate():
            before = time.monotonic()
            if scenario == '更新检查后移出组':
                self.api('DELETE', f'/teams/{self.teams["Committer1"]}/members/alice')
            else:
                self.api('POST', endpoint + f'/reviews/{reviews["alice"]}/dismissals',
                         {'message': '隔离反证：合并写入前撤回指定组唯一审批', 'priors': False})
            snap = self.snapshot(endpoint)
            if snap['满足规则'] or snap['有效总票数'] < 3:
                raise RuntimeError('变更后样本不符合反证条件')
            actual = self.execute('git', '--git-dir=' + repo_dir, 'rev-parse', 'refs/heads/main').strip()
            if actual != base_before:
                raise RuntimeError('变更确认之前目标引用已写入，实验时序无效')
            result['变更'] = {'开始单调时间': before, '完成单调时间': time.monotonic(),
                              '变更后审批': snap, '写入前目标引用': actual}

        if scenario == '提前撤回':
            mutate()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.api, 'POST', endpoint + '/merge',
                                 {'do': style, 'head_commit_id': head}, 'author',
                                 (200, 403, 405, 409, 500))
            try:
                for phase in ('pre-receive', 'update'):
                    self.wait_file(case_dir / (phase + '.已进入'), future)
                    snapshot = self.snapshot(endpoint)
                    write_json(case_dir / (phase + '.校验快照.json'), snapshot)
                    (case_dir / (phase + '.判定')).write_text('允许' if snapshot['满足规则'] else '拒绝')
                    event = {'阶段': phase, '校验': snapshot,
                             '实际Git输入': (case_dir / (phase + '.引用')).read_text(),
                             '原生身份': (case_dir / (phase + '.身份')).read_text().splitlines()}
                    result['同步点'].append(event)
                    if not snapshot['满足规则']:
                        break
                    self.wait_file(case_dir / (phase + '.已检查'), future)
                    if ((scenario == '预接收后撤回' and phase == 'pre-receive') or
                        (scenario in ('更新检查后撤回', '更新检查后移出组') and phase == 'update')):
                        mutate()
                    event['解除同步点单调时间'] = time.monotonic()
                    (case_dir / (phase + '.继续')).write_text('继续执行')
                status, body = future.result(timeout=35)
            finally:
                # 异常路径只让未决实验拒绝，不将其放行以便退出。
                for phase in ('pre-receive', 'update'):
                    decision = case_dir / (phase + '.判定')
                    if not decision.exists():
                        decision.write_text('拒绝')
        final_pr = self.get(endpoint)
        final = self.snapshot(endpoint)
        base_after = self.execute('git', '--git-dir=' + repo_dir, 'rev-parse', 'refs/heads/main').strip()
        result.update({'合并HTTP': status, '合并响应': body, 'PR已合并': final_pr['merged'],
                       '最终目标引用': base_after, '最终审批': final,
                       '原生规则保持': self.get(path + '/branch_protections') == baseline_rule,
                       '原生Hook保持': all(self.execute('sha256sum', repo_dir + '/hooks/' + phase).split()[0] == digest
                                          for phase, digest in wrapper_hashes.items())})
        changed = base_after != base_before
        if not result['原生规则保持'] or not result['原生Hook保持']:
            raise RuntimeError('实验期间原生配置变化')
        if scenario == '正常批准':
            if status != 200 or not changed or not final_pr['merged']:
                raise RuntimeError('正向对照未合并，不能使用后续反证结论')
            result['结论'] = '通过'
        elif scenario in ('提前撤回', '预接收后撤回'):
            if status == 200 or changed or final_pr['merged']:
                raise RuntimeError('负向对照未能阻断，需排查实验实现')
            result['结论'] = '通过'
        else:
            if changed != bool(final_pr['merged']):
                raise RuntimeError('PR 与 Git 结果不一致，实验需单独调查')
            result['结论'] = '阻断' if changed and not final['满足规则'] else '通过'
            if result['结论'] == '阻断':
                received = result['同步点'][-1]['实际Git输入'].split()
                if base_after != received[1]:
                    raise RuntimeError('目标引用不是本次 Hook 检查的更新，不能归因')
                result['原因'] = '两次外接检查均通过后，原生审批或组修改已完成，但 Git 仍写入；缺少与原生修改共享的原子边界。'
        self.record(result)
        if scenario == '更新检查后移出组':
            self.api('PUT', f'/teams/{self.teams["Committer1"]}/members/alice')

    def stop(self):
        # 仅停止本次新建实例，保留其卷和首次失败现场；从不停止其他容器。
        if self.container:
            info = self.verify_owned()
            instance = self.report.setdefault('实例', {'容器': self.container})
            instance['实验结束启动时间'] = info['State']['StartedAt']
            instance['实验期间未重启'] = (
                info['State']['StartedAt'] == instance.get('初始启动时间'))
            self.save()
        if self.compose_path.exists():
            self.compose('stop', '--timeout', '10')
            self.report['隔离实例已停止'] = True
            self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--styles', nargs='+', choices=STYLES, default=list(STYLES),
                        help='待测合并方式；每次都创建新的独立环境')
    args = parser.parse_args()
    probe = Probe()
    code = 1
    try:
        probe.start()
        probe.fixtures()
        index = 0
        for style in args.styles:
            for scenario in SCENARIOS:
                index += 1
                probe.case(style, scenario, index)
        probe.report['状态'] = '强制接入阻断' if probe.report['阻断'] else '本阶段反证未触发'
        code = 2 if probe.report['阻断'] else 0
    except Exception as exc:
        probe.report['状态'] = '实验未完成'
        probe.report['错误'] = str(exc).replace(probe.password, '[已隐藏]')
        probe.report['错误位置'] = [{'文件': Path(frame.filename).name, '行': frame.lineno,
                                    '函数': frame.name} for frame in traceback.extract_tb(exc.__traceback__)]
        print('实验未完成：' + probe.report['错误'], flush=True)
    finally:
        probe.save()
        try:
            probe.stop()
        except Exception as exc:
            probe.report['停止错误'] = str(exc).replace(probe.password, '[已隐藏]')
            probe.save()
            code = 1
        print('中文证据：' + str(probe.report_path), flush=True)
    return code


if __name__ == '__main__':
    raise SystemExit(main())

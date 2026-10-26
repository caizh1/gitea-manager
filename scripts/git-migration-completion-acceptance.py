#!/usr/bin/env python3
"""在真实接收端逐项验证人员、附件、流水线依赖及全部合并条件，保留失败证据。"""
import argparse
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import secrets
import time
import requests

spec = importlib.util.spec_from_file_location('advanced', Path(__file__).with_name('git-migration-advanced-acceptance.py'))
advanced = importlib.util.module_from_spec(spec)
spec.loader.exec_module(advanced)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('manager-state', 'target-state', 'fixture'):
        parser.add_argument('--' + key, type=Path, required=True)
    parser.add_argument('--run-id', type=int, required=True)
    args = parser.parse_args()
    manager, target, fixture = (json.loads(p.read_text()) for p in (args.manager_state, args.target_state, args.fixture))
    info = json.loads(advanced.docker('inspect', target['name']))[0]
    assert info['Config']['Labels'].get('io.gitea-manager.test') == 'git-migration-receiver'
    run = advanced.read_run(manager['name'], args.run_id)
    assert run['status'] == 'succeeded'
    checkpoint = json.loads(run['checkpoint_json'])
    root = checkpoint[str(fixture['project']['id'])]
    base = target['base'] + '/api/v1'
    client = requests.Session()
    client.trust_env = False
    client.auth = ('migration-admin', target['password'])
    client.get(target['base'] + '/user/login', timeout=30)
    login = client.post(target['base'] + '/user/login', headers={'Origin': target['base']},
                        data={'user_name': 'migration-admin', 'password': target['password']}, allow_redirects=False, timeout=30)
    assert login.status_code in (302, 303), '隔离接收端网页认证失败'
    outsider = requests.Session()
    outsider.trust_env = False
    rows = []
    report = {'说明': '固定真实来源的全部合并变体与负向门禁验收，不代表生产、全版本或人工节省指标',
              '来源': fixture['kind'], '场景': rows, '合并矩阵': [], '结果': '执行中'}
    output = args.manager_state.parent / ('completion-acceptance-' + str(args.run_id) + '.json')

    def save():
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')

    def api(method, path, data=None, auth=None, expected=(200, 201, 204)):
        r = client.request(method, base + path, json=data, auth=auth, timeout=120)
        assert r.status_code in expected, '原生接口失败：' + path + ' HTTP ' + str(r.status_code) + ' ' + r.text[:250]
        return r.json() if r.content else None

    def record(name):
        rows.append({'场景': name, '结果': '通过'})
        save()

    def db(sql):
        return json.loads(advanced.docker('exec', target['name'], 'sqlite3', '-json', '/data/gitea/gitea.db', sql) or '[]')

    def merge(path, style, allowed, name):
        # 等待原生异步可合并性计算，拒绝断言不能把暂时的 checking 状态算作门禁通过。
        deadline = time.monotonic() + 90
        while True:
            pull = api('GET', path + '/pulls/1')
            if pull['merged']:
                raise AssertionError('请求已被意外合并：' + name)
            if pull.get('mergeable'):
                break
            assert time.monotonic() < deadline, '原生冲突计算未完成：' + name
            time.sleep(0.5)
        before = api('GET', path + '/branches/main')['commit']['id']
        r = client.post(base + path + '/pulls/1/merge', json={'Do': style, 'merge_when_checks_succeed': False}, timeout=120)
        if allowed:
            assert r.status_code in (200, 201, 204), '合并失败：' + name + ' HTTP ' + str(r.status_code) + ' ' + r.text[:250]
            actual = api('GET', path + '/pulls/1')
            assert actual['merged'], '合并响应成功但原生请求未合并'
            return before, api('GET', path + '/branches/main')['commit']['id']
        assert r.status_code in (403, 405, 409, 422), '门禁未拒绝：' + name + ' HTTP ' + str(r.status_code)
        assert not api('GET', path + '/pulls/1')['merged']
        assert api('GET', path + '/branches/main')['commit']['id'] == before
        record(name)

    try:
        path = '/repos/' + root['final_path']
        assignee = target['test_users']['migration-assignee']['id']
        reviewer = target['test_users']['migration-reviewer']['id']
        issue = next(i for i in api('GET', path + '/issues?state=all&type=issues') if i['title'] == '完整性与人员映射')
        pull = api('GET', path + '/pulls/1')
        assert {u['id'] for u in issue['assignees']} == {assignee}
        assert {u['id'] for u in pull['assignees']} == {assignee}
        assert {u['id'] for u in pull['requested_reviewers']} == {reviewer}
        reviews = api('GET', path + '/pulls/1/reviews')
        assert any(r['state'] == 'REQUEST_REVIEW' and r['user']['id'] == reviewer for r in reviews)
        record('负责人和当前评审请求落入指定已有账号，评审请求没有伪造成批准')
        for file in fixture['completeness']['comment_files']:
            asset = root['assets'][file['source_id']]
            r = client.get(target['base'] + '/attachments/' + asset['uuid'], timeout=60)
            assert r.status_code == 200 and hashlib.sha256(r.content).hexdigest() == file['sha256']
        if fixture['kind'] == 'gitea':
            comments = api('GET', path + '/issues/' + str(issue['number']) + '/comments')
            text = '\n'.join(c['body'] for c in comments)
            assets = [root['assets'][f['source_id']] for f in fixture['completeness']['comment_files'][:2]]
            assert len({a['uuid'] for a in assets}) == 2 and all(a['uuid'] in text for a in assets)
            record('两个同名评论附件与行内附件均完整下载，评论链接分别指向目标文件')
        expected_lfs = {v['oid'] if isinstance(v, dict) else v for v in fixture['lfs']['oids']}
        actual_lfs = {v['oid'] for v in root['snapshot']['lfs_objects']}
        assert expected_lfs <= actual_lfs and root['code_verified']
        record('两个历史 LFS 对象包含在目标重新克隆与重新下载核验中')
        for row in (v for k, v in checkpoint.items() if k.isdigit()):
            for asset in row.get('assets', {}).values():
                suffix = '/attachments/' + asset['uuid'] if asset['parent_kind'] in ('issue', 'pull', 'comment', 'code_comment', 'release') else '/' + row['final_path'] + '/migration-history/assets/' + str(asset['id'])
                rejected = outsider.get(target['base'] + suffix, allow_redirects=False, timeout=30)
                assert rejected.status_code in (302, 303, 401, 403, 404), '私有迁入文件被未登录用户读取'
            histories = db('SELECT kind, source_id, parent_id, data FROM governance_migration_history WHERE repo_id=' + str(row['repo_id']))
            source_ids = {(r['kind'], r['source_id']) for r in histories}
            assert {(k.split(':', 1)[0], k.split(':', 1)[1]) for k in row['objects'] if k.startswith(('ci_pipeline:', 'ci_job:', 'ci_trigger:'))} <= source_ids
            for h in histories:
                if h['kind'] == 'ci_job':
                    assert ('ci_pipeline', h['parent_id']) in source_ids
                if h['kind'] == 'ci_trigger':
                    raw = json.loads(h['data'])
                    assert ('ci_pipeline', h['parent_id']) in source_ids
                    downstream = raw.get('downstream_pipeline')
                    if downstream:
                        actual = checkpoint[str(downstream['project_id'])]
                        assert raw['target_repository_id'] == actual['repo_id']
                        assert 'ci_pipeline:' + str(downstream['id']) in actual['objects']
        record('全部迁入文件均拒绝未登录读取')
        if fixture['kind'] == 'gitlab':
            jobs = {str(j['作业编号']) for j in fixture['ci_hierarchy']['jobs']}
            actual = {k.split(':', 1)[1] for v in checkpoint.values() if isinstance(v, dict) for k in v.get('objects', {}) if k.startswith('ci_job:')}
            assert jobs <= actual
            assert sum(k.startswith('ci_trigger:') for v in checkpoint.values() if isinstance(v, dict) for k in v.get('objects', {})) >= 3
            record('全部三级及跨项目流水线、触发作业、五个真实 C 作业与下游目标仓库关系完整')
            # 先解决讨论，以便每个 CI 拒绝都有独立可达因果链；最后重新打开讨论单独验证。
            comments = [c for r in reviews for c in api('GET', path + '/pulls/1/reviews/' + str(r['id']) + '/comments')]
            assert comments, '缺少迁入的未解决讨论'
            for comment in comments:
                api('POST', path + '/pulls/comments/' + str(comment['id']) + '/resolve')
            head = pull['head']['sha']
            ci_auth = ('migration-ci', target['test_users']['migration-ci']['password'])
            other_auth = ('migration-assignee', target['test_users']['migration-assignee']['password'])

            def status(sha, state, auth, suffix=''):
                api('POST', path + '/statuses/' + sha, {'state': state, 'context': 'migration/gitlab-pipeline' + suffix, 'description': '隔离迁移验收'}, auth=auth)

            merge(path, 'merge', False, '历史成功记录不能替代当前提交的 CI 回执')
            status(head, 'success', other_auth)
            merge(path, 'merge', False, '普通代码写入者伪造同名成功回执被拒绝')
            status(pull['base']['sha'], 'success', ci_auth)
            merge(path, 'merge', False, '可信账号的旧提交成功不能批准当前提交')
            status(head, 'failure', ci_auth)
            merge(path, 'merge', False, '可信 CI 失败阻断合并')
            status(head, 'success', ci_auth, '/skipped')
            merge(path, 'merge', False, '来源不允许跳过流水线，跳过回执被拒绝')
            status(head, 'success', ci_auth)
            status(head, 'pending', ci_auth)
            merge(path, 'merge', False, '重新运行的等待状态不能复用此前成功')
            status(head, 'success', ci_auth)
            api('POST', path + '/pulls/comments/' + str(comments[0]['id']) + '/unresolve')
            merge(path, 'merge', False, '当前 CI 成功但仍有未解决讨论时阻断合并')
            for comment in comments:
                api('POST', path + '/pulls/comments/' + str(comment['id']) + '/resolve')
        merge(path, 'merge', True, '跨仓库请求满足迁入约束后真实合并')
        record('跨仓库请求满足迁入约束后真实合并')
        for case in fixture['completeness']['merge_cases']:
            row = checkpoint[str(case['project_id'])]
            route = '/repos/' + row['final_path']
            config = api('GET', route)
            if fixture['kind'] == 'gitlab':
                allowed = (['fast-forward-only'] if case['method'] == 'ff' else ['merge']) if case['squash'] != 'always' else []
                if case['squash'] != 'never':
                    allowed.append('squash')
                default = 'squash' if case['squash'] in ('always', 'default_on') else allowed[0]
            else:
                allowed, default = [case['style']], case['style']
            assert config['default_merge_style'] == default
            for style, field in (('merge', 'allow_merge_commits'), ('rebase', 'allow_rebase'), ('rebase-merge', 'allow_rebase_explicit'), ('squash', 'allow_squash_merge'), ('fast-forward-only', 'allow_fast_forward_only_merge')):
                assert bool(config[field]) == (style in allowed)
            wrong = next(s for s in ('merge', 'rebase', 'squash', 'fast-forward-only') if s not in allowed)
            merge(route, wrong, False, '禁止来源未允许的合并方式：' + str(case['project_id']))
            if fixture['kind'] == 'gitlab' and case['method'] in ('ff', 'rebase_merge'):
                api('POST', route + '/contents/base-advance.txt', {
                    'branch': 'main', 'content': base64.b64encode('推进目标分支以验证线性历史约束\n'.encode()).decode(),
                    'message': '隔离验收：推进目标分支'})
                merge(route, case['style'], False, '来源分支落后时拒绝线性合并：' + str(case['project_id']))
                api('POST', route + '/pulls/1/update?style=rebase')
            before, after = merge(route, case['style'], True, '全部合并方式组合：' + str(case['project_id']))
            commit = api('GET', route + '/git/commits/' + after)
            expected_parents = 2 if case['style'] in ('merge', 'rebase-merge') or fixture['kind'] == 'gitlab' and case['method'] != 'ff' else 1
            assert len(commit['parents']) == expected_parents, '合并提交图与来源语义不一致'
            if expected_parents == 2:
                assert commit['parents'][0]['sha'] == before
                if case['style'] == 'squash':
                    squash = api('GET', route + '/git/commits/' + commit['parents'][1]['sha'])
                    assert len(squash['parents']) == 1 and squash['parents'][0]['sha'] == before
            report['合并矩阵'].append({'来源项目': case['project_id'], '方法': case['method'], '压缩策略': case['squash'], '实际方式': case['style'], '提交父节点数': expected_parents, '结果': '真实合并与提交图一致'})
            save()
        assert len(report['合并矩阵']) == len(fixture['completeness']['merge_cases'])
        report['结果'] = '通过'
        save()
        print('完整迁移补齐验收通过：' + str(output))
    except BaseException as error:
        report['结果'] = '失败'
        report['错误'] = str(error)
        save()
        raise


if __name__ == '__main__':
    main()

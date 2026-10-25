#!/usr/bin/env python3
"""向标记的隔离 GitLab 创建真实议题、合并请求、Wiki、发布和通用制品。"""
import argparse
import json
from pathlib import Path
import secrets
import subprocess
import requests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-state', required=True, type=Path)
    args = parser.parse_args()
    state = json.loads(args.source_state.read_text())
    info = json.loads(subprocess.check_output(['docker', 'inspect', state['name']]))[0]
    if state['kind'] != 'gitlab' or info['Config']['Labels'].get('io.gitea-manager.test') != 'git-migration-source':
        raise RuntimeError('只能使用标记的隔离 GitLab 来源')
    session = requests.Session()
    session.trust_env = False
    reply = session.post(state['base'] + '/oauth/token', data={'grant_type': 'password', 'username': 'root',
                         'password': state['password'], 'scope': 'api'}, timeout=60)
    reply.raise_for_status()
    session.headers['Authorization'] = 'Bearer ' + reply.json()['access_token']
    def api(method, path, **kwargs):
        result = session.request(method, state['base'] + '/api/v4' + path, timeout=60, **kwargs)
        if not 200 <= result.status_code < 300:
            raise RuntimeError('夹具请求失败：' + method + ' ' + path + ' HTTP ' + str(result.status_code))
        return result.json() if result.content and result.headers.get('Content-Type', '').startswith('application/json') else None
    name = 'rich-migration-' + secrets.token_hex(4)
    project = api('POST', '/projects', json={'name': name, 'visibility': 'private', 'initialize_with_readme': True, 'default_branch': 'main'})
    base = '/projects/' + str(project['id'])
    label = api('POST', base + '/labels', json={'name': '中文缺陷', 'color': '#ff5500'})
    milestone = api('POST', base + '/milestones', json={'title': '历史里程碑', 'due_date': '2026-12-31'})
    upload = api('POST', base + '/uploads', files={'file': ('evidence.txt', '来源附件\n'.encode(), 'text/plain')})
    issue = api('POST', base + '/issues', json={'title': '历史议题', 'description': '问题正文\n' + upload['markdown'], 'labels': label['name'], 'milestone_id': milestone['id']})
    api('POST', base + '/issues/' + str(issue['iid']) + '/notes', json={'body': '历史普通讨论'})
    commit = api('POST', base + '/repository/commits', json={'branch': 'feature', 'start_branch': 'main', 'commit_message': '添加迁移验收内容', 'actions': [{'action': 'create', 'file_path': 'feature.txt', 'content': '新功能\n'}]})
    pull = api('POST', base + '/merge_requests', json={'source_branch': 'feature', 'target_branch': 'main', 'title': '历史合并请求', 'description': '合并请求原始正文'})
    api('POST', base + '/merge_requests/' + str(pull['iid']) + '/notes', json={'body': '合并请求普通讨论'})
    api('POST', base + '/wikis', json={'title': 'Home', 'content': 'Wiki 原始内容\n', 'format': 'markdown'})
    api('POST', base + '/releases', json={'tag_name': 'v1.0-fixture', 'ref': 'feature', 'name': '历史发布', 'description': '发布原始说明'})
    api('PUT', base + '/packages/generic/fixture/1.0/package.txt', data='真实制品内容\n'.encode(), headers={'Content-Type': 'application/octet-stream'})
    output = args.source_state.parent / (name + '.json')
    output.write_text(json.dumps({'说明': '真实隔离 GitLab 复杂来源，所有资源保留用于验收', 'project': project,
                                 'issue': issue['iid'], 'pull': pull['iid'], 'commit': commit['id']}, ensure_ascii=False, indent=2) + '\n')
    print('真实 GitLab 复杂来源已创建：' + str(output))


if __name__ == '__main__':
    main()

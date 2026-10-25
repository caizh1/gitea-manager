#!/usr/bin/env python3
"""为已标记的隔离 Gitea 来源创建真实协作与附件验收仓库。"""
import argparse
import base64
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
    container = json.loads(subprocess.check_output(['docker', 'inspect', state['name']]))[0]
    if container['Config']['Labels'].get('io.gitea-manager.test') != 'git-migration-source' or state['kind'] != 'gitea':
        raise RuntimeError('只能向已标记的隔离 Gitea 来源建立夹具')
    client = requests.Session()
    client.trust_env = False
    client.auth = ('source-admin', state['password'])
    name = 'rich-migration-' + secrets.token_hex(4)
    base = state['base'] + '/api/v1'
    def api(method, path, **kwargs):
        reply = client.request(method, base + path, timeout=60, **kwargs)
        if not 200 <= reply.status_code < 300:
            raise RuntimeError('夹具请求失败：' + method + ' ' + path + ' HTTP ' + str(reply.status_code))
        return reply.json() if reply.content else None
    repo = api('POST', '/user/repos', json={'name': name, 'private': True, 'auto_init': True,
                'default_branch': 'main', 'readme': 'Default', 'description': '迁移端到端协作夹具'})
    path = '/repos/source-admin/' + name
    api('PATCH', path, json={'has_actions': False})
    label = api('POST', path + '/labels', json={'name': '中文标签', 'color': 'ff5500'})
    milestone = api('POST', path + '/milestones', json={'title': '历史里程碑', 'description': '保留历史计划', 'due_on': '2026-12-31T00:00:00Z'})
    issue = api('POST', path + '/issues', json={'title': '需要附件的问题', 'body': '原始问题正文', 'labels': [label['id']], 'milestone': milestone['id']})
    api('POST', path + '/issues/' + str(issue['number']) + '/comments', json={'body': '来源讨论与中文正文'})
    attachment = api('POST', path + '/issues/' + str(issue['number']) + '/assets', files={'attachment': ('evidence.txt', '真实附件内容\n'.encode(), 'text/plain')})
    api('PATCH', path + '/issues/' + str(issue['number']), json={'body': '原始问题正文\n\n[附件](' + attachment['browser_download_url'] + ')'})
    commit = api('POST', path + '/contents/feature.txt', json={'content': base64.b64encode('新功能\n'.encode()).decode(),
                  'branch': 'main', 'new_branch': 'feature', 'message': '添加验收功能'})
    pull = api('POST', path + '/pulls', json={'base': 'main', 'head': 'feature', 'title': '历史合并请求', 'body': '保留讨论与代码差异'})
    api('POST', path + '/issues/' + str(pull['number']) + '/comments', json={'body': '合并请求的普通评论'})
    api('POST', path + '/pulls/' + str(pull['number']) + '/reviews', json={'event': 'COMMENT', 'body': '历史评审意见',
             'comments': [{'path': 'feature.txt', 'new_position': 1, 'body': '行内历史评论'}]})
    release = api('POST', path + '/releases', json={'tag_name': 'v1.0-fixture', 'target_commitish': 'feature',
                  'name': '历史发布', 'body': '发布说明', 'draft': False, 'prerelease': False})
    api('POST', path + '/releases/' + str(release['id']) + '/assets', files={'attachment': ('release.txt', b'release fixture\n', 'text/plain')})
    api('POST', path + '/wiki/new', json={'title': 'Home', 'content_base64': base64.b64encode('Wiki 历史内容\n'.encode()).decode(), 'message': '创建迁移 Wiki'})
    package = name + '-package'
    response = client.put(state['base'] + '/api/packages/source-admin/generic/' + package + '/1.0/package.txt',
                          data='通用制品验收内容\n'.encode(), timeout=60)
    if response.status_code != 201:
        raise RuntimeError('来源制品上传失败，HTTP ' + str(response.status_code))
    api('POST', '/packages/source-admin/generic/' + package + '/-/link/' + name)
    output = args.source_state.parent / (name + '.json')
    output.write_text(json.dumps({'说明': '真实隔离协作夹具，后续迁移必须核对全部对象', 'repo': repo,
                                 'issue': issue['number'], 'pull': pull['number'], 'release': release['id'],
                                 'commit': commit['commit']['sha']}, ensure_ascii=False, indent=2) + '\n')
    print('真实协作夹具已创建：' + str(output))


if __name__ == '__main__':
    main()

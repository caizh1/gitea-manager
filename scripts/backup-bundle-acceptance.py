#!/usr/bin/env python3
"""构造含父子组、审批规则、改名仓库和 LFS 的真实离线包，并销毁源实例。"""
import hashlib
import importlib.util
import json
import tarfile
import secrets
from pathlib import Path
import requests
import paramiko

spec = importlib.util.spec_from_file_location('phase1', '/scripts/backup-phase1-acceptance.py')
p1 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p1)
m = p1.m
from services.disaster_bundle import export
from services.restore_secrets import read_file, put_file
from services.backup_consistency import database_sql


def main():
    oauth_secret = secrets.token_urlsafe(32).encode()
    def create(image, suffix, **kwargs):
        if image.startswith('gitea/gitea:'):
            kwargs.setdefault('environment', {}).update({'GITEA__server__LFS_START_SERVER': 'true',
                'GITEA__actions.artifacts__PATH': '/data/actions-external',
                'GITEA__oauth2__JWT_SECRET_URI': 'file:/external-config/oauth-jwt', 'GITEA__oauth2__JWT_SECRET': ''})
            kwargs['volumes'] = {m.marker + '-external': {'bind': '/external-config', 'mode': 'rw'}}
        container = p1.create(image, suffix, **kwargs)
        if image.startswith('gitea/gitea:'):
            m.client.containers.run(container.attrs['Image'], entrypoint=['sh', '-ec'],
                command=['mkdir -p /data/actions-external; chown 1000:1000 /data/actions-external'],
                volumes_from=[container.id], network_mode='none', remove=True)
            put_file(container, '/external-config/oauth-jwt', oauth_secret, 0o600, 1000, 1000)
        return container
    m.create = create
    try:
        with m.app.app_context():
            m.db.create_all()
            source, src, password = m.node('source')
            key = paramiko.RSAKey.generate(3072)
            key_path = Path('/evidence/disaster-ssh-key')
            key.write_private_key_file(str(key_path))
            key_path.chmod(0o600)
            p1.api(source, 'POST', '/user/keys', json={'title': '灾备验收公钥', 'key': key.get_name() + ' ' + key.get_base64()})
            host_key = read_file(src, '/data/ssh/ssh_host_ed25519_key.pub')[0].decode().split()[:2]
            repo = p1.api(source, 'POST', '/user/repos', json={'name': 'before-rename', 'auto_init': True, 'private': True}).json()
            p1.api(source, 'PATCH', '/repos/progressadmin/before-rename', json={'name': 'after-rename'})
            parent = p1.api(source, 'POST', '/governance/groups', json={'name': '灾备父组', 'path': 'disaster-parent', 'visibility': 2}).json()
            child = p1.api(source, 'POST', '/governance/groups', json={'name': '灾备子组', 'path': 'child', 'parent_id': parent['id'], 'visibility': 2}).json()
            p1.api(source, 'POST', '/governance/repositories/' + str(repo['id']) + '/approval-rules',
                json={'rule': {'name': '灾备审批规则', 'required': 1, 'user_ids': [1], 'branch_mode': 'all', 'enabled': True, 'locked': True}})
            content = '灾备 LFS 对象：恢复后须逐字节一致。'.encode()
            oid = hashlib.sha256(content).hexdigest()
            response = requests.post(source.gitea_url + '/progressadmin/after-rename.git/info/lfs/objects/batch',
                auth=('progressadmin', password), headers={'Accept': 'application/vnd.git-lfs+json', 'Content-Type': 'application/vnd.git-lfs+json'}, json={'operation': 'upload', 'transfers': ['basic'], 'objects': [{'oid': oid, 'size': len(content)}]}, timeout=30)
            response.raise_for_status()
            action = response.json()['objects'][0]['actions']['upload']
            response = requests.put(action['href'], headers=action.get('header'), data=iter([content]), timeout=30)
            response.raise_for_status()
            artifact = '离线恢复 Actions 制品正文'.encode()
            put_file(src, '/data/actions-external/review-proof.bin', artifact, 0o600, 1000, 1000)
            database_sql(m.client.containers.get(source.pg_container), source,
                "INSERT INTO action_artifact (run_id,run_attempt_id,repo_id,owner_id,storage_path,file_size,artifact_path,artifact_name,status,expired_unix) VALUES (1,1,1,1,'review-proof.bin'," + str(len(artifact)) + ",'review-proof.bin','灾备制品',2,4102444800)")
            backup = m.backup(source, 'bundle.zip')
            export(backup, '/evidence/disaster-bundle.tar')
            with tarfile.open('/evidence/disaster-bundle.tar') as archive:
                assert all(member.mode & 0o077 == 0 for member in archive.getmembers()), '离线包内敏感文件对其他用户可读'
            expected = {'SSH主机公钥': ' '.join(host_key), '父组': parent['id'], '子组': child['id'], '仓库': repo['id'], 'LFS摘要': oid, 'LFS大小': len(content),
                'Actions摘要': hashlib.sha256(artifact).hexdigest(), 'OAuth密钥摘要': hashlib.sha256(oauth_secret).hexdigest()}
            Path('/evidence/bundle-expected.json').write_text(json.dumps(expected, ensure_ascii=False, indent=2))
            m.note('完整离线包已生成，包含改名仓库、父子组、审批规则和 LFS 对象', **expected)
    finally:
        for container in reversed(m.owned):
            try: container.remove(force=True, v=True)
            except Exception: pass
        m.client.volumes.get(m.marker + '-external').remove()


if __name__ == '__main__':
    main()

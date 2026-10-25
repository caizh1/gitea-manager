"""复用真实备份验收，在耗时边界用独立连接检查登录和请求时隙写入。"""
import importlib.util
from pathlib import Path
import sqlite3
import time
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('backup_acceptance', Path(__file__).with_name('backup-ssh-stream-acceptance.py'))
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
from services import backup_actions, restore_secrets

def competing_write(stage, server):
    # 强制读取与生产调用相同的 ORM 配置，暴露未提交记录的自动刷写。
    _ = server.pg_user
    with sqlite3.connect(str(a.m.root/'manager.db'), timeout=.2) as connection:
        connection.execute('INSERT INTO access_login_throttle(key,window_at,attempts) VALUES (?,0,1)',
                           (stage+str(time.time_ns()),))
        connection.execute('INSERT OR REPLACE INTO policy_request_slots(server_id,next_at) VALUES (?,0)', (server.id,))
    a.m.note('真实备份期间独立登录与请求时隙写入通过', 阶段=stage, 连接模式=server.connection_mode)

secret = restore_secrets.capture_backup_secret
artifacts = backup_actions.append_artifacts

def secret_check(server, backup):
    competing_write('密钥处理', server)
    return secret(server, backup)

def artifacts_check(path, source, database, server, raw):
    competing_write('制品处理', server)
    return artifacts(path, source, database, server, raw)

if __name__ == '__main__':
    with patch.object(restore_secrets, 'capture_backup_secret', secret_check), \
         patch.object(backup_actions, 'append_artifacts', artifacts_check):
        a.main()

#!/usr/bin/env python3
"""仅启动全新临时数据库的改密验收服务；不接触现有实例。"""
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
state = Path(tempfile.mkdtemp(prefix='account-security-', dir=ROOT / 'work'))
os.environ.update(DATABASE_URL='sqlite:///' + str(state / 'manager.db'),
    SECRET_KEY=secrets.token_urlsafe(48), INIT_PASSWORD='admin123',
    BACKUP_DIR=str(state / 'backups'), AI_REVIEW_DB=str(state / 'review.db'),
    ACCESS_KEY_FILE=str(state / 'access.key'), DISABLE_BACKGROUND_TASKS='1',
    AI_REVIEW_HOST_POLICY_FILE='', INTEGRATION_ENABLED='false',
    GM_UPGRADE_MAINTENANCE='0', GM_UPGRADE_MAINTENANCE_FILE=str(state / 'maintenance'))
sys.path.insert(0, str(ROOT / 'backend'))
from app import create_app
from werkzeug.serving import make_server

app = create_app()
server = make_server('127.0.0.1', 0, app, threaded=True)
print('验收服务：' + json.dumps({'端口': server.server_port, '目录': str(state)}, ensure_ascii=False), flush=True)
server.serve_forever()

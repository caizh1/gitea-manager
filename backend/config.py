import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, '..', 'data')

DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:////app/data/gitea-manager.db')
BACKUP_DIR = os.environ.get('BACKUP_DIR', os.path.join(DATA_DIR, 'backups'))
SSH_KEY_PATH = os.environ.get('SSH_KEY_PATH', os.path.expanduser('~/.ssh/id_rsa'))
SSH_KEY_PASSPHRASE = os.environ.get('SSH_KEY_PASSPHRASE', None)
SECRET_KEY = os.environ.get('SECRET_KEY', 'change-me-in-production')
INIT_PASSWORD = os.environ.get('INIT_PASSWORD', 'admin123')
RESTORE_LONG_JOB_TIMEOUT_SECONDS = int(os.environ.get('RESTORE_LONG_JOB_TIMEOUT_SECONDS', '7200'))
RESTORE_JOB_POLL_SECONDS = int(os.environ.get('RESTORE_JOB_POLL_SECONDS', '5'))
RESTORE_SSH_RECONNECT_GRACE_SECONDS = int(os.environ.get('RESTORE_SSH_RECONNECT_GRACE_SECONDS', '300'))
RESTORE_DIAGNOSTIC_INTERVAL_SECONDS = int(os.environ.get('RESTORE_DIAGNOSTIC_INTERVAL_SECONDS', '30'))

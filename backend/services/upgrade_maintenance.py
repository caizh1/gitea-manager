"""升级期间统一暂停写入与任务领取，校验通过后移除持久标记即可恢复。"""
import os
from pathlib import Path


def active():
    if os.environ.get('GM_UPGRADE_MAINTENANCE') == '1': return True
    value = os.environ.get('GM_UPGRADE_MAINTENANCE_FILE')
    if not value: return False
    try: return Path(value).exists()
    except OSError: return True

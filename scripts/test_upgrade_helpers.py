"""升级数据门禁回归：覆盖真实旧表字段、WAL、旧列与文件保留边界。"""
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('volume', Path(__file__).with_name('upgrade_volume.py'))
volume = importlib.util.module_from_spec(spec); spec.loader.exec_module(volume)


class UpgradeChecks(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name); self.path = self.root / 'manager.db'
        self.connection = sqlite3.connect(self.path); self.addCleanup(self.connection.close)
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.executescript("CREATE TABLE scheduled_tasks(id INTEGER, last_status TEXT); INSERT INTO scheduled_tasks VALUES (1,'success'); CREATE TABLE settings(key TEXT,value TEXT); INSERT INTO settings VALUES ('密码','原有值');")

    def test_reads_uncheckpointed_wal(self):
        self.connection.execute("UPDATE settings SET value='已提交到 WAL'"); self.connection.commit()
        snapshot = volume.database(self.path)
        self.assertEqual(snapshot['tables']['settings']['rows'], 1)
        self.assertNotEqual(snapshot['tables']['settings']['sha256'], volume.hashlib.sha256(b'').hexdigest())

    def test_schedule_uses_last_status(self):
        self.assertEqual(volume.database(self.path)['busy'], [])
        self.connection.execute("UPDATE scheduled_tasks SET last_status='running'"); self.connection.commit()
        self.assertEqual(volume.database(self.path)['busy'], [{'table':'scheduled_tasks','count':1}])

    def test_old_rows_and_columns_survive_migration(self):
        old = volume.database(self.path)
        self.connection.executescript('ALTER TABLE settings ADD COLUMN new_field TEXT; CREATE TABLE new_table(id INTEGER);')
        self.assertEqual(volume.database(self.path, old)['tables'], old['tables'])

    def test_changed_secret_is_detected(self):
        old = volume.database(self.path)
        self.connection.execute("UPDATE settings SET value='意外重置'"); self.connection.commit()
        self.assertNotEqual(volume.database(self.path, old)['tables']['settings'], old['tables']['settings'])

    def test_backup_files_excluded_from_copy_but_recorded(self):
        (self.root/'backups').mkdir(); (self.root/'backups/历史.zip').write_bytes(b'123456')
        before = volume.inventory(self.root, 'backups')
        self.assertEqual(before['preserved'][0][:2], ['backups/历史.zip',6])
        (self.root/'backups/历史.zip').write_bytes(b'changed')
        self.assertNotEqual(volume.inventory(self.root, 'backups')['preserved'], before['preserved'])

    def test_link_cannot_escape_backup_boundary(self):
        (self.root/'外部目录').symlink_to('/tmp', target_is_directory=True)
        with self.assertRaises(RuntimeError): volume.inventory(self.root, '')


if __name__ == '__main__': unittest.main()

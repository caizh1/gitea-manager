"""中断任务收尾必须有进程重启证据，且只能影响指定任务。"""
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('finish_backup', Path(__file__).with_name('finish-interrupted-backup.py'))
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


class InterruptedBackupTests(unittest.TestCase):
    def test_refuses_live_thread_and_preserves_other_tasks(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); path=root/'manager.db'
            (root/'diagnostic').mkdir()
            (root/'diagnostic'/'status.json').write_text(json.dumps({'开始时间':100}))
            with sqlite3.connect(path) as c:
                c.executescript('''CREATE TABLE backups(id INTEGER PRIMARY KEY,status,diagnostic_id,completed_at,error_msg,
                    commit_snapshot_status,progress_stage,progress_label,progress_detail,progress_updated_at);
                    CREATE TABLE backup_step_logs(backup_id,status,completed_at,detail);
                    INSERT INTO backups(id,status,diagnostic_id) VALUES(133,'running','diagnostic'),(134,'running','diagnostic');
                    INSERT INTO backup_step_logs(backup_id,status) VALUES(133,'running'),(134,'running');''')
            with self.assertRaisesRegex(RuntimeError,'无法证明'):
                tool.finish(path,root,133,99)
            with sqlite3.connect(path) as c:
                self.assertEqual(c.execute('SELECT status FROM backups WHERE id=133').fetchone()[0],'running')
            self.assertEqual(tool.finish(path,root,133,200)['状态'],'failed')
            with sqlite3.connect(path) as c:
                self.assertEqual(c.execute('SELECT id,status FROM backups ORDER BY id').fetchall(),[(133,'failed'),(134,'running')])
                self.assertEqual(c.execute('SELECT backup_id,status FROM backup_step_logs ORDER BY backup_id').fetchall(),[(133,'failed'),(134,'running')])
            self.assertEqual(tool.finish(path,root,133,200)['状态'],'failed')
            with self.assertRaises(sqlite3.OperationalError):
                tool.finish(root/'missing.db',root,133,200)
            self.assertFalse((root/'missing.db').exists())


if __name__ == '__main__':
    unittest.main()

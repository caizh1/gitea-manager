"""故障记录的真实子进程、脱敏、容量及权限验收，不创建业务资源。"""
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import time
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend/services'))
from diagnostics import Report, CommandFailure, register_secret, SOURCE_LIMIT


class DiagnosticsTests(unittest.TestCase):
    def test_failure_preserves_primary_and_redacts_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            report=Report(folder,'故障注入验收')
            register_secret('isolated-secret-abc123')
            try:
                with report.stage('模拟镜像解包失败'):
                    report.command([sys.executable,'-c',"import sys; print('failed to unmount: device or resource busy isolated-secret-abc123',file=sys.stderr); sys.exit(17)"],label='镜像创建',timeout=2)
            except CommandFailure as exc:report.fail(exc)
            report.fail(RuntimeError('附加采集失败'))
            archive=report.finish('失败')
            with tarfile.open(archive) as bundle:
                text='\n'.join(bundle.extractfile(m).read().decode() for m in bundle if m.isfile())
            self.assertNotIn('isolated-secret-abc123',text)
            self.assertIn('device or resource busy',text)
            self.assertIn('模拟镜像解包失败',text)
            self.assertEqual(report.error['步骤'],'模拟镜像解包失败')
            self.assertEqual(archive.stat().st_mode & 0o777,0o600)
            self.assertEqual(report.directory.stat().st_mode & 0o777,0o700)
            for p in report.directory.rglob('*'):
                if p.is_file():self.assertEqual(p.stat().st_mode & 0o777,0o600)

    def test_timeout_is_bounded_and_evidence_survives(self):
        with tempfile.TemporaryDirectory() as folder:
            report=Report(folder,'启动挂死注入')
            start=time.monotonic()
            with self.assertRaisesRegex(CommandFailure,'超时'):
                report.command([sys.executable,'-u','-c',"import time; print('开始检查'); time.sleep(60)"],label='执行器启动',timeout=.3)
            self.assertLess(time.monotonic()-start,4)
            self.assertIn('开始检查',next((report.directory/'logs').glob('*stdout*')).read_text())

    def test_output_limits_keep_first_and_last(self):
        with tempfile.TemporaryDirectory() as folder:
            report=Report(folder,'日志截断验收')
            report.command([sys.executable,'-c',"print('FIRST'+('x'*4000000)+'LAST')"],label='大量输出',timeout=5)
            log=next((report.directory/'logs').glob('*stdout*'))
            self.assertLessEqual(log.stat().st_size,SOURCE_LIMIT)
            self.assertIn('FIRST',log.read_text());self.assertIn('LAST',log.read_text())

    def test_retention_and_parseable_events(self):
        with tempfile.TemporaryDirectory() as folder:
            for i in range(12):
                report=Report(folder,'保留数量验收')
                report.event('失败','x'*40000)
                report.finish('失败')
            self.assertEqual(len(list(Path(folder).glob('diagnostics-*.tar.gz'))),10)
            for path in Path(folder).glob('*/events.jsonl'):
                for line in path.read_text().splitlines():json.loads(line)

if __name__=='__main__':unittest.main()

class DiagnosticStorageFailureTests(unittest.TestCase):
    def test_collection_storage_error_never_blocks_business_rollback(self):
        from unittest.mock import patch
        import errno
        with tempfile.TemporaryDirectory() as folder:
            report=Report(folder,'磁盘已满')
            with patch('diagnostics.os.open',side_effect=OSError(errno.ENOSPC,'No space left on device')):
                report.fail(RuntimeError('原始恢复错误'))
                report.event('开始','执行回退')
            self.assertEqual(report.error['说明'],'原始恢复错误')

"""重启后不得沿用过期的执行器就绪状态。"""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from ai_review.runtime_status import available, status


class RuntimeStatusTests(unittest.TestCase):
    def test_requires_fresh_worker_observation_and_preserves_installer_degradation(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);installed=root/'installed.json';live=root/'executor-live-status.json'
            installed.write_text(json.dumps({'ready':True,'version':'1.18.7','checks':[{'步骤':'旧安装成功'}]}))
            with patch.dict(os.environ,{'AI_REVIEW_RUNTIME_STATUS':str(installed),'AI_REVIEW_DB':str(root/'review.db')}):
                self.assertFalse(available());self.assertNotIn('checks',status())
                live.write_text(json.dumps({'ready':True,'checked_at':time.time()}));self.assertTrue(available())
                live.write_text(json.dumps({'ready':True,'checked_at':time.time()-60}));self.assertFalse(available());self.assertNotIn('checks',status())
                live.write_text(json.dumps({'ready':False,'checked_at':time.time(),'reason':'启动失败'}));self.assertEqual(status()['reason'],'启动失败');self.assertNotIn('checks',status())
                installed.write_text(json.dumps({'ready':False,'reason':'两套执行器失败'}))
                live.write_text(json.dumps({'ready':True,'checked_at':time.time()}));self.assertFalse(available())

    def test_runtime_failure_preserves_probe_details_and_creates_one_archive(self):
        from ai_review.runtime_status import refresh
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);installed=root/'installed.json';installed.write_text(json.dumps({'ready':True,'checks':[{'步骤':'旧的安装检查'}]}))
            env={'AI_REVIEW_RUNTIME_STATUS':str(installed),'AI_REVIEW_DB':str(root/'review.db'),'MANAGEMENT_DIAGNOSTICS':str(root/'diagnostics')}
            response=MagicMock(status_code=503)
            response.json.return_value={'ready':False,'reason':'会话启动超时','checks':[{'步骤':'创建会话','尝试次数':1,'通过':False}],'process':[{'线程':'12','wchan':'futex_wait_queue'}]}
            session=MagicMock();session.__enter__.return_value=session;session.get.return_value=response
            with patch.dict(os.environ,env),patch('requests.Session',return_value=session):
                refresh();first=status()
                self.assertFalse(first['ready'])
                self.assertEqual(first['checks'][0]['步骤'],'创建会话')
                self.assertTrue(first['diagnostic_id'])
                archive=root/'diagnostics'/('diagnostics-'+first['diagnostic_id']+'.tar.gz')
                self.assertTrue(archive.is_file())
                os.utime(root/'executor-live-status.json',(0,0));refresh()
                self.assertEqual(status()['diagnostic_id'],first['diagnostic_id'])
                self.assertEqual(len(list((root/'diagnostics').glob('*.tar.gz'))),1)
                self.assertEqual(archive.stat().st_mode & 0o777,0o600)

"""补丁只允许修改固定文件，且重复运行和中断恢复不接触用户状态。"""
import fcntl
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('repair', Path(__file__).with_name('repair_image_identity.py'))
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.target, self.bundle = self.root / 'installed', self.root / 'repair'
        (self.target / 'deployment').mkdir(parents=True)
        (self.target / 'state').mkdir()
        (self.bundle / 'payload/deployment').mkdir(parents=True)
        (self.target / 'installer-manifest.json').write_text('{}')
        (self.target / 'state/installation.json').write_text('保留原账号和密钥')
        (self.target / 'compose.install.json').write_text('保留原服务配置')
        files = []
        for name in ('deployment/image_identity.py', 'deployment/install.py', 'SHA256SUMS'):
            old, new = self.target / name, self.bundle / 'payload' / name
            if name != 'deployment/image_identity.py':
                old.write_text('原文件')
            new.write_text('修复文件')
            files.append({'路径': name, '原SHA256': MODULE.sha(old) if old.exists() else None, '新SHA256': MODULE.sha(new)})
        self.manifest = {'目标清单SHA256': MODULE.sha(self.target / 'installer-manifest.json'), '文件': files}
        self.save()

    def save(self):
        (self.bundle / 'repair-manifest.json').write_text(json.dumps(self.manifest, ensure_ascii=False))

    def test_repair_and_rerun_preserve_state_and_manifest(self):
        before = {p: p.read_bytes() for p in (self.target / 'state/installation.json', self.target / 'compose.install.json', self.target / 'installer-manifest.json')}
        MODULE.apply_repair(self.target, self.bundle)
        MODULE.apply_repair(self.target, self.bundle)
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)

    def test_interrupted_repair_can_resume(self):
        row = self.manifest['文件'][0]
        (self.target / row['路径']).write_bytes((self.bundle / 'payload' / row['路径']).read_bytes())
        MODULE.apply_repair(self.target, self.bundle)

    def test_changed_original_and_corrupt_payload_are_rejected_before_writes(self):
        source = self.bundle / 'payload/deployment/install.py'
        source.write_text('损坏的补丁')
        with self.assertRaisesRegex(RuntimeError, '补丁文件校验失败'):
            MODULE.apply_repair(self.target, self.bundle)
        self.assertFalse((self.target / 'deployment/image_identity.py').exists())
        source.write_text('修复文件')
        (self.target / 'deployment/install.py').write_text('用户手工修改')
        with self.assertRaisesRegex(RuntimeError, '原文件已被修改'):
            MODULE.apply_repair(self.target, self.bundle)
        self.assertFalse((self.target / 'deployment/image_identity.py').exists())

    def test_active_lifecycle_lock_is_respected(self):
        with (self.target / '.lifecycle.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, '安装或卸载正在运行'):
                MODULE.apply_repair(self.target, self.bundle)

    def test_wrong_package_is_rejected(self):
        (self.target / 'installer-manifest.json').write_text('另一个版本')
        with self.assertRaisesRegex(RuntimeError, '不是本补丁对应'):
            MODULE.apply_repair(self.target, self.bundle)


if __name__ == '__main__':
    unittest.main()

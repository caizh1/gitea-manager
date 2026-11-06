"""验证传统镜像存储、OCI 镜像存储和拒绝伪造身份的确定性边界。"""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('installer', Path(__file__).with_name('install.py'))
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ImageIdentityTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        (self.root / 'images').mkdir()
        self.path = self.root / 'images/runtime.tar'
        self.tag = 'sample:fixed'

    def fixture(self, *, architecture='amd64', classic=False, indexed=False,
                wrong_config=False, wrong_digest=False, wrong_size=False, wrong_namespace=False):
        def encoded(value):
            return json.dumps(value, sort_keys=True).encode()

        def digest(raw):
            return 'sha256:' + hashlib.sha256(raw).hexdigest()

        config = encoded({'architecture': architecture, 'os': 'linux', 'rootfs': {'type': 'layers', 'diff_ids': []}})
        config_id = digest(config)
        config_path = 'blobs/sha256/' + config_id.split(':')[1]
        manifest = encoded({'schemaVersion': 2, 'config': {
            'digest': 'sha256:' + '0' * 64 if wrong_config else config_id, 'size': len(config)}, 'layers': []})
        manifest_id = digest(manifest)
        descriptor = {'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                      'digest': manifest_id, 'size': len(manifest)}
        content = {config_path: config, 'manifest.json': encoded([
            {'RepoTags': [self.tag], 'Config': config_path, 'Layers': []}])}
        content['blobs/sha256/' + manifest_id.split(':')[1]] = manifest
        top_id = manifest_id
        if indexed:
            nested = encoded({'schemaVersion': 2, 'manifests': [dict(descriptor, platform={'os': 'linux', 'architecture': 'amd64'})]})
            top_id = digest(nested)
            content['blobs/sha256/' + top_id.split(':')[1]] = nested
            descriptor = {'mediaType': 'application/vnd.oci.image.index.v1+json', 'digest': top_id, 'size': len(nested)}
        descriptor['annotations'] = {'io.containerd.image.name': ('other.example/team/' if wrong_namespace else 'docker.io/library/') + self.tag}
        if wrong_digest:
            content['blobs/sha256/' + top_id.split(':')[1]] += b' '
            descriptor['size'] += 1
        if wrong_size:
            descriptor['size'] += 1
        if not classic:
            content['index.json'] = encoded({'manifests': [descriptor]})
        with tarfile.open(self.path, 'w') as archive:
            for name, raw in content.items():
                item = tarfile.TarInfo(name)
                item.size = len(raw)
                archive.addfile(item, io.BytesIO(raw))
        self.images = {'backend': {'tag': self.tag, 'ids': [config_id]}}
        return config_id, manifest_id, top_id

    def test_accepts_config_and_oci_id_from_same_archive(self):
        config_id, manifest_id, _ = self.fixture()
        self.assertEqual(set(MODULE.archive_image_ids(self.path, self.images)['backend']), {config_id, manifest_id})

    def test_classic_archive_and_nested_oci_index(self):
        config_id, _, _ = self.fixture(classic=True)
        self.assertEqual(MODULE.archive_image_ids(self.path, self.images)['backend'], [config_id])
        config_id, manifest_id, index_id = self.fixture(indexed=True)
        self.assertEqual(set(MODULE.archive_image_ids(self.path, self.images)['backend']), {config_id, manifest_id, index_id})
        self.images['backend']['ids'] = [index_id]
        self.assertIn(config_id, MODULE.archive_image_ids(self.path, self.images)['backend'])

    def test_rejects_wrong_architecture_and_unrelated_delivery(self):
        self.fixture(architecture='arm64')
        with self.assertRaisesRegex(RuntimeError, '架构'):
            MODULE.archive_image_ids(self.path, self.images)
        self.fixture()
        self.images['backend']['ids'] = ['sha256:' + '1' * 64]
        with self.assertRaisesRegex(RuntimeError, '没有相同身份'):
            MODULE.archive_image_ids(self.path, self.images)

    def test_rejects_oci_tampering_or_unrelated_configuration(self):
        for option in ('wrong_config', 'wrong_digest', 'wrong_size'):
            with self.subTest(option=option):
                self.fixture(**{option: True})
                with self.assertRaises(RuntimeError):
                    MODULE.archive_image_ids(self.path, self.images)

    def test_does_not_trust_a_tag_suffix_from_another_registry(self):
        config_id, manifest_id, _ = self.fixture(wrong_namespace=True)
        self.assertEqual(MODULE.archive_image_ids(self.path, self.images)['backend'], [config_id])
        self.images['backend']['ids'] = [manifest_id]
        with self.assertRaises(RuntimeError):
            MODULE.archive_image_ids(self.path, self.images)

    def test_new_engine_reuses_oci_id_and_old_engine_reuses_config_id(self):
        config_id, manifest_id, _ = self.fixture()
        installer = MODULE.Installer(self.root, {'images': self.images})
        for identity in (config_id, manifest_id):
            actual = json.dumps([{'Id': identity, 'Architecture': 'amd64', 'Os': 'linux'}])
            with self.subTest(identity=identity), patch.object(MODULE, 'run', side_effect=[self.tag + '\n', actual]) as command:
                installer.load_images()
                self.assertFalse(any('load' in call.args[0] for call in command.call_args_list))

    def test_post_load_accepts_oci_but_rejects_wrong_id_or_architecture(self):
        _, manifest_id, _ = self.fixture()
        installer = MODULE.Installer(self.root, {'images': self.images})
        for identity, architecture, accepted in ((manifest_id, 'amd64', True), (manifest_id, 'arm64', False), ('sha256:' + '2' * 64, 'amd64', False)):
            actual = json.dumps([{'Id': identity, 'Architecture': architecture, 'Os': 'linux'}])
            with self.subTest(identity=identity, architecture=architecture), patch.object(MODULE, 'run', side_effect=['', '', actual]) as command:
                if accepted:
                    installer.load_images()
                else:
                    with self.assertRaisesRegex(RuntimeError, '实际='):
                        installer.load_images()
                self.assertEqual(command.call_args_list[1].args[0][:2], ['docker', 'load'])


if __name__ == '__main__':
    unittest.main()

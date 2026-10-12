"""版本准入边界；不代表目标环境兼容性验收。"""
import ast
from pathlib import Path
import re
import unittest


class ReviewVersionTests(unittest.TestCase):
    def test_backend_and_installer_agree(self):
        root = Path(__file__).resolve().parents[1] / 'ai_review'
        tree = ast.parse((root / 'gitea.py').read_text())
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'version')
        namespace = {'re': re, 'ReviewError': ValueError}
        exec(compile(ast.Module(body=[method], type_ignores=[]), '<版本准入>', 'exec'), namespace)
        installer = ast.parse((root / 'assets/install.py').read_text())
        pattern = next(n.args[0].value for n in ast.walk(installer)
                       if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                       and n.func.attr == 'search' and len(n.args) == 2
                       and isinstance(n.args[1], ast.Name) and n.args[1].id == 'version')
        for version, allowed in [('1.26.4', True), ('1.26.4+review.1', True),
                                 ('1.27.3', True), ('1.27.3+governance.demo.nav7', True),
                                 ('1.27.30', False), ('1.27.4', False), ('1.27.3-rc1', False),
                                 ('1.26.4+unknown', False), ('1.25.0', False)]:
            with self.subTest(version=version):
                client = type('Client', (), {'request': lambda *args: {'version': version}})()
                if allowed:
                    self.assertEqual(namespace['version'](client), version)
                else:
                    with self.assertRaises(ValueError):
                        namespace['version'](client)
                self.assertEqual(bool(re.search(pattern, 'Gitea version ' + version + ' built with go')), allowed)

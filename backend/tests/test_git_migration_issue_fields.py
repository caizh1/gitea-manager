"""截止日期和讨论锁定必须进入转换与独立核验，不能只比较对象数量。"""
import unittest
from git_migration.normalize import objects, timestamp
from git_migration.source_manifest import Census, verify_plan
from svn_migration.common import MigrationError


class IssueFieldsTests(unittest.TestCase):
    def sample(self, kind, pull=False):
        date = '2026-11-10' if kind == 'gitlab' else '2026-11-10T12:34:56+08:00'
        row = {'id': 9, 'iid': 2, 'number': 2, 'title': '有截止日期的工作',
               'state': 'opened' if kind == 'gitlab' else 'open',
               'author': {'id': 1, 'username': 'source'}, 'user': {'id': 1, 'login': 'source'},
               'created_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-01-02T00:00:00Z',
               'due_date': date, 'discussion_locked': True, 'is_locked': True}
        if pull:
            row.update(source_branch='feature', target_branch='main', source_project_id=1, target_project_id=1,
                       diff_refs={'head_sha': 'a'*40, 'base_sha': 'b'*40},
                       head={'sha': 'a'*40, 'ref': 'feature', 'repo': {'id': 1}},
                       base={'sha': 'b'*40, 'ref': 'main', 'repo': {'id': 1}})
            if kind == 'gitlab':
                row.pop('due_date')
        return row

    def test_issue_and_pull_dates_and_locks_are_preserved(self):
        for kind in ('gitea', 'gitlab'):
            for pull in (False, True):
                with self.subTest(kind=kind, pull=pull):
                    row = self.sample(kind, pull)
                    category = ('合并请求详情' if kind == 'gitlab' else '合并请求') if pull else '议题'
                    result = objects({'datasets': [{'category': category, 'rows': [row]}]}, kind)['objects'][0]
                    expected = (0 if pull else timestamp('2026-11-10T00:00:00Z')) if kind == 'gitlab' else timestamp(row['due_date'])
                    self.assertEqual(result.get('deadline'), expected)
                    self.assertTrue(result.get('locked'))

    def test_independent_manifest_detects_dropped_deadline_and_lock(self):
        row = self.sample('gitea')
        class Source:
            kind = 'gitea'
        census = Census(Source(), {'id': 1})
        census.issue_fields('issue', row)
        manifest = {'objects': [('issue', '9')], 'files': [], 'rules': [], 'gaps': [], 'kind': 'gitea',
                    'fields': census.fields}
        normalized = {'objects': [{'kind': 'issue', 'source_id': '9', 'deadline': 0, 'locked': False}]}
        with self.assertRaisesRegex(MigrationError, '字段'):
            verify_plan(manifest, normalized, [], [])
        normalized['objects'][0].update(deadline=timestamp(row['due_date']), locked=True)
        verify_plan(manifest, normalized, [], [])


"""原始来源字段与原生写入载荷之间的确定性核对。"""
import unittest
from git_migration.normalize import objects, timestamp
from svn_migration.common import MigrationError


class NormalizeTests(unittest.TestCase):
    def test_issue_and_comment_keep_source_identity_and_time(self):
        rows = [
            {'category': '标签', 'path': '/labels', 'rows': [{'id': 2, 'name': '缺陷', 'color': 'ff0000'}]},
            {'category': '议题与合并请求', 'path': '/issues', 'rows': [
                {'id': 100, 'number': 7, 'title': '历史问题', 'body': '原始正文', 'state': 'closed',
                 'user': {'id': 9, 'login': 'source-user'}, 'labels': [{'name': '缺陷'}],
                 'created_at': '2020-01-01T08:00:00+08:00', 'updated_at': '2020-01-02T00:00:00Z',
                 'closed_at': '2020-01-02T00:00:00Z'}]},
            {'category': '议题评论', 'path': '/comments', 'rows': [
                {'id': 8, 'body': '评论', 'issue_url': 'https://source.test/api/v1/repos/team/repo/issues/7',
                 'user': {'id': 9, 'login': 'source-user'},
                 'created_at': '2020-01-02T00:00:00Z', 'updated_at': '2020-01-02T00:00:00Z'}]}]
        result = objects({'datasets': rows}, 'gitea')
        self.assertEqual(result['pending'], [])
        self.assertEqual(result['objects'][1]['source_id'], '100')
        self.assertEqual(result['objects'][1]['created'], 1577836800)
        self.assertEqual(result['objects'][2]['parent_id'], '100')
        self.assertEqual(result['objects'][1]['author_id'], 9)

    def test_confidential_content_never_becomes_ordinary_target_content(self):
        data = {'datasets': [{'category': '议题', 'rows': [{'confidential': True}]}]}
        with self.assertRaisesRegex(MigrationError, '保密议题'):
            objects(data, 'gitlab')
        for flag in ('confidential', 'internal'):
            data = {'datasets': [{'category': '议题讨论', 'path': '/issues/1/discussions',
                                  'rows': [{'notes': [{flag: True}]}]}]}
            with self.assertRaisesRegex(MigrationError, '内部评论'):
                objects(data, 'gitlab')

    def test_missing_timezone_and_missing_dependency_fail(self):
        with self.assertRaises(MigrationError):
            timestamp('2020-01-01T00:00:00')
        with self.assertRaises(MigrationError):
            timestamp(None)

    def test_unsupported_content_is_not_silently_discarded(self):
        result = objects({'datasets': [{'category': '发布', 'path': '/releases', 'rows': [{'id': 1}]}]}, 'gitlab')
        self.assertEqual(result['pending'][0]['count'], 1)


if __name__ == '__main__':
    unittest.main()


class CollaborationTests(unittest.TestCase):
    def test_gitlab_inline_replies_keep_thread_position(self):
        created = '2020-01-01T00:00:00Z'
        actor = {'id': 1, 'username': 'source-user'}
        pull = {'id': 9, 'iid': 1, 'title': '变更', 'state': 'opened', 'author': actor,
                'created_at': created, 'updated_at': created, 'source_branch': 'feature',
                'target_branch': 'main', 'source_project_id': 5, 'target_project_id': 5,
                'diff_refs': {'head_sha': 'a' * 40, 'base_sha': 'b' * 40}}
        position = {'position_type': 'text', 'head_sha': 'a' * 40, 'base_sha': 'b' * 40,
                    'new_path': 'file.txt', 'old_path': 'file.txt', 'new_line': 2}
        note = {'id': 10, 'body': '行内根评论', 'position': position, 'author': actor,
                'created_at': created, 'updated_at': created, 'resolved': True}
        reply = {k: v for k, v in dict(note, id=11, body='历史回复').items() if k != 'position'}
        data = {'datasets': [
            {'category': '合并请求详情', 'path': '/merge_requests/1', 'rows': [pull]},
            {'category': '合并请求讨论', 'path': '/merge_requests/1/discussions',
             'rows': [{'id': 'thread-1', 'notes': [note, reply]}]}]}
        result = objects(data, 'gitlab', resolve_diff=lambda _: '@@ -1 +1,2 @@\n 原文\n+新增\n')
        self.assertFalse(result['pending'])
        comments = [o for o in result['objects'] if o['kind'] == 'code_comment']
        self.assertEqual(len(comments), 2)
        self.assertEqual({o['review_id'] for o in comments}, {'discussion-thread-1'})
        self.assertTrue(all(o['line'] == 2 and o['resolved'] for o in comments))
        self.assertTrue(objects(data, 'gitlab')['pending'])

    def test_pull_comment_and_review_keep_parent_and_approval_history(self):
        actor = {'id': 1, 'login': 'source-user'}
        created = '2020-01-01T00:00:00Z'
        pull = {'id': 50, 'number': 2, 'title': '历史变更', 'state': 'open', 'user': actor,
                'created_at': created, 'updated_at': created,
                'head': {'sha': 'a' * 40, 'ref': 'feature', 'repo': {'id': 1}},
                'base': {'sha': 'b' * 40, 'ref': 'main', 'repo': {'id': 1}}}
        data = {'datasets': [
            {'category': '合并请求', 'path': '/repos/a/b/pulls', 'rows': [pull]},
            {'category': '议题评论', 'path': '/comments', 'rows': [
                {'id': 8, 'body': '讨论', 'issue_url': '', 'pull_request_url': 'https://source/a/b/pulls/2',
                 'user': actor, 'created_at': created, 'updated_at': created}]},
            {'category': '评审', 'path': '/repos/a/b/pulls/2/reviews', 'rows': [
                {'id': 3, 'state': 'APPROVED', 'user': actor, 'body': '历史批准', 'submitted_at': created, 'updated_at': created}]},
            {'category': '行内评审', 'path': '/repos/a/b/pulls/2/reviews/3/comments', 'rows': [
                {'id': 9, 'path': 'a.txt', 'position': 2, 'commit_id': 'a' * 40, 'body': '行内意见',
                 'pull_request_review_id': 3, 'user': actor, 'created_at': created, 'updated_at': created}]},
        ]}
        result = objects(data, 'gitea')
        self.assertFalse(result['pending'])
        self.assertEqual(result['objects'][1]['parent_kind'], 'pull')
        self.assertEqual(result['objects'][1]['parent_id'], '50')
        self.assertEqual(result['objects'][2]['review_state'], 'APPROVED')
        self.assertEqual(result['objects'][3]['review_id'], '3')

    def test_gitlab_number_collision_is_explicit_and_deterministic(self):
        created = '2020-01-01T00:00:00Z'
        actor = {'id': 1, 'username': 'source-user'}
        issue = {'id': 1, 'iid': 1, 'title': '问题', 'state': 'opened', 'author': actor, 'created_at': created, 'updated_at': created}
        pull = dict(issue, id=9, title='变更', source_branch='feature', target_branch='main',
                    source_project_id=5, target_project_id=5, diff_refs={'head_sha': 'a' * 40, 'base_sha': 'b' * 40})
        data = {'datasets': [{'category': '议题', 'path': '/issues', 'rows': [issue]},
                            {'category': '合并请求详情', 'path': '/merge_requests/1', 'rows': [pull]}]}
        result = objects(data, 'gitlab')['objects']
        self.assertEqual([item['number'] for item in result], [1, 2])
        self.assertIn('来源合并请求 !1', result[1]['content'])

class ProjectPrerequisiteTests(unittest.TestCase):
    def test_source_merge_requirements_are_not_silently_relaxed(self):
        from git_migration.normalize import objects
        for constraint in ({'only_allow_merge_if_pipeline_succeeds':True},
                           {'only_allow_merge_if_all_discussions_are_resolved':True},
                           {'merge_method':'ff'},{'squash_option':'always'}):
            item={'project':{'id':1},'datasets':[{'category':'项目合并前置条件','path':'/projects/1','rows':[constraint]}]}
            result=objects(item,'gitlab');self.assertTrue(result['pending'])
        item={'project':{'id':1},'datasets':[{'category':'项目合并前置条件','path':'/projects/1','rows':[{'merge_method':'merge','squash_option':'default_off','only_allow_merge_if_pipeline_succeeds':False}]}]}
        self.assertFalse(objects(item,'gitlab')['pending'])

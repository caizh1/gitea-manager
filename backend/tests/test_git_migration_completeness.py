"""采集遗漏与恢复死锁的反例必须在真实转换链中被发现。"""
import unittest
from svn_migration.common import MigrationError
from git_migration.inventory import Inventory
from git_migration.normalize import objects
from git_migration.assets import plan_assets


class CommentAttachmentTests(unittest.TestCase):
    def test_comment_attachment_is_downloaded_and_rewritten(self):
        created = '2020-01-01T00:00:00Z'
        url = 'https://source.test/attachments/12345678-1234-5678-1234-567812345678'
        attachment = {'id': 11, 'uuid': url.rsplit('/', 1)[1], 'name': 'build.log', 'size': 12,
                      'created_at': created, 'browser_download_url': url}
        issue = {'id': 100, 'number': 7, 'title': '历史问题', 'state': 'open',
                 'user': {'id': 9, 'login': 'reader'}, 'created_at': created, 'updated_at': created}
        comment = {'id': 8, 'body': '[编译日志](' + url + ')', 'issue_url': 'https://source.test/api/v1/repos/team/repo/issues/7',
                   'user': issue['user'], 'created_at': created, 'updated_at': created, 'assets': [attachment]}
        class Source:
            kind = 'gitea'
            def get(self, path): return {'id': 1, 'full_name': 'team/repo', 'has_wiki': False}
            def collection(self, path, **kwargs):
                return {'/repos/team/repo/issues': [issue], '/repos/team/repo/issues/comments': [comment],
                        '/repos/team/repo/issues/comments/8/assets': [attachment]}.get(path, [])
        inventory = Inventory(Source()).inspect({'id': 1, 'path': 'team/repo'})
        normalized = objects(inventory, 'gitea')
        assets = plan_assets(inventory, 'gitea', normalized, 'a'*32, 'https://source.test', 'https://target.test')
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]['metadata']['parent_kind'], 'comment')
        self.assertEqual(assets[0]['metadata']['parent_id'], '8')
        self.assertEqual(normalized['pending'], [])
        self.assertNotIn(url, next(o for o in normalized['objects'] if o['kind'] == 'comment')['content'])


class IndependentManifestTests(unittest.TestCase):
    def test_independent_per_issue_view_detects_missing_global_comment(self):
        from git_migration.source_manifest import capture, verify_plan
        created = '2020-01-01T00:00:00Z'
        issue = {'id': 10, 'number': 1, 'title': '历史问题', 'state': 'open', 'user': {'id': 1, 'login': 'reader'},
                 'created_at': created, 'updated_at': created}
        comment = {'id': 20, 'body': '编译记录', 'issue_url': 'https://source.test/api/v1/repos/team/repo/issues/1',
                   'user': issue['user'], 'created_at': created, 'updated_at': created,
                   'assets': [{'id': 30, 'name': 'build.log', 'size': 12}]}
        class Source:
            kind = 'gitea'
            def get(self, path):
                return issue if path.endswith('/issues/1') else {'id': 1, 'full_name': 'team/repo', 'has_wiki': False}
            def collection(self, path, **kwargs):
                if path.endswith('/issues'):
                    return [] if kwargs.get('params', {}).get('state') == 'closed' else [issue]
                if path.endswith('/issues/1/comments'): return [comment]
                return []  # 模拟全仓评论采集遗漏；独立入口仍能读到原始资料。
        source, project = Source(), {'id': 1, 'path': 'team/repo'}
        manifest = capture(source, project)
        self.assertIn(('comment', '20'), manifest['objects'])
        normalized = objects(Inventory(source).inspect(project), 'gitea')
        with self.assertRaisesRegex(MigrationError, '对象清单'):
            verify_plan(manifest, normalized, [], [])
        normalized['objects'].append({'kind': 'comment', 'source_id': '20'})
        with self.assertRaisesRegex(MigrationError, '文件清单'):
            verify_plan(manifest, normalized, [], [])


class HierarchyAndPeopleTests(unittest.TestCase):
    def test_child_pipeline_and_trigger_job_are_recursively_collected(self):
        class Source:
            kind = 'gitlab'
            def get(self, path): return {'id': int(path.rsplit('/',1)[1]), 'project_id': 9, 'created_at': '2020-01-01T00:00:00Z', 'status': 'success'}
            def collection(self, path, **kwargs):
                if path == '/projects/9/pipelines': return [{'id': 1}]
                if path.endswith('/1/bridges'): return [{'id': 10, 'name': '子流水线', 'status': 'success', 'created_at': '2020-01-01T00:00:00Z', 'downstream_pipeline': {'id': 2, 'project_id': 9}}]
                if path.endswith('/2/bridges'): return [{'id': 11, 'name': '孙流水线', 'status': 'success', 'created_at': '2020-01-01T00:00:00Z', 'downstream_pipeline': {'id': 3, 'project_id': 9}}]
                return []
        inventory = Inventory(Source()); inventory.gitlab_ci('/projects/9', 9)
        normalized = objects({'datasets': inventory.datasets}, 'gitlab', repository_map={9: {'repo_id': 90}})
        self.assertEqual({o['source_id'] for o in normalized['objects'] if o['kind'] == 'ci_pipeline'}, {'1','2','3'})
        self.assertEqual({o['source_id'] for o in normalized['objects'] if o['kind'] == 'ci_trigger'}, {'10','11'})
        self.assertFalse(normalized['pending'])

    def test_person_mapping_preserves_assignee_and_current_reviewer_separately(self):
        from git_migration.people import mapped
        pending = []
        row = {'id': 1, 'assignees': [{'id': 5}], 'reviewers': [{'id': 6}]}
        value = mapped(row, 'gitlab', {'gitlab:person:5':[{'id':50}], 'gitlab:person:6':[{'id':60}]}, pending)
        self.assertEqual(value, {'assignee_ids':[50], 'reviewer_ids':[60], 'official_reviewer_ids':[]})
        self.assertFalse(pending)
        mapped(row, 'gitlab', {}, pending)
        self.assertTrue(pending)

    def test_all_twelve_gitlab_merge_combinations_have_native_constraints(self):
        from git_migration.merge_conditions import convert
        for method in ('merge','rebase_merge','ff'):
            for squash in ('never','always','default_on','default_off'):
                with self.subTest(method=method,squash=squash):
                    item = {'datasets':[{'category':'项目合并前置条件','path':'/projects/1','rows':[{'merge_method':method,'squash_option':squash,
                        'only_allow_merge_if_pipeline_succeeds':True,'only_allow_merge_if_all_discussions_are_resolved':True}]}]}
                    normalized = objects(item, 'gitlab')
                    convert(item,'gitlab',{'gitlab:ci:issuer':[{'id':90}]},normalized)
                    self.assertFalse(normalized['pending'])
                    policy = normalized['objects'][0]['history']
                    self.assertEqual((policy['method'], policy['squash']), (method,squash))
                    self.assertEqual(policy['ci_user_id'],90)
                    self.assertTrue(policy['require_discussions'])
                    self.assertEqual('squash' in policy['allowed_styles'],squash!='never')
                    if squash=='always': self.assertEqual(policy['allowed_styles'],['squash'])


class GitCredentialScopeTests(unittest.TestCase):
    def test_credentials_are_limited_to_same_origin_and_repository(self):
        from git_migration.git_credentials import credential
        values={'GM_GIT_ALLOWED_URL':'https://source.test/git/team/repo.git','GM_GIT_USERNAME':'reader','GM_GIT_PASSWORD':'测试凭据'}
        for path in ('git/team/repo.git','git/team/repo.git/info/lfs','git/team/repo.git/gitlab-lfs/objects/abc'):
            self.assertEqual(credential({'protocol':'https','host':'source.test:443','path':path},values),{'username':'reader','password':'测试凭据'})
        for protocol,host,path in (('http','source.test','git/team/repo.git'),('https','source.test.evil','git/team/repo.git'),
            ('https','source.test','git/team/repo.git.evil'),('https','source.test','git/other/repo.git'),
            ('https','source.test','git/team/repo.git/../other.git'),('https','source.test','git/team/repo.git/%2e%2e/other.git'),
            ('https','user@source.test','git/team/repo.git')):
            self.assertFalse(credential({'protocol':protocol,'host':host,'path':path},values))
        self.assertFalse(credential({'protocol':'https','host':'source.test','path':'git/team/repo.git'},dict(values,GM_GIT_PASSWORD='注入\nusername=other')))

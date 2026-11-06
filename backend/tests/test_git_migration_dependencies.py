"""跨仓库闭包、循环依赖、不可读依赖与原生目标关系的回归。"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from git_migration.dependencies import inspect_closure
from git_migration.normalize import objects
from svn_migration.common import MigrationError


class DependencyTests(unittest.TestCase):
    def test_recursive_cycle_and_automatic_selection_are_fixed_once(self):
        projects=[{'id':i,'path':'org/p'+str(i)} for i in (1,2,3)]
        def inspect(project):
            return {'project':project,'datasets':[{'category':'合并请求详情','rows':[{'source_project_id': {1:2,2:3,3:1}[project['id']]}]}]}
        with patch('git_migration.dependencies.Inventory') as factory:
            factory.return_value.inspect.side_effect=inspect
            result=inspect_closure(SimpleNamespace(kind='gitlab'),projects,{1})
        self.assertEqual([v['project']['id'] for v in result],[1,2,3])
        self.assertEqual([v['dependency'] for v in result],[False,True,True])

    def test_inaccessible_dependency_never_becomes_same_repository(self):
        with patch('git_migration.dependencies.Inventory') as factory:
            factory.return_value.inspect.return_value={'datasets':[{'category':'合并请求详情','rows':[{'source_project_id':2}]}]}
            with self.assertRaisesRegex(MigrationError,'不在可读取发现范围'):
                inspect_closure(SimpleNamespace(kind='gitlab'),[{'id':1}],{1})

    def test_cross_repository_pull_keeps_stable_target_id(self):
        pull={'id':9,'iid':1,'source_project_id':2,'target_project_id':1,'source_branch':'feature','target_branch':'main',
              'diff_refs':{'head_sha':'a'*40,'base_sha':'b'*40},'title':'跨仓库请求','state':'opened','author':{'id':3,'username':'历史作者'},
              'created_at':'2020-01-01T00:00:00Z','updated_at':'2020-01-01T00:00:00Z'}
        item={'project':{'id':1},'datasets':[{'category':'合并请求详情','rows':[pull]}]}
        with self.assertRaises(MigrationError):objects(item,'gitlab')
        converted=objects(item,'gitlab',repository_map={1:{'repo_id':101},2:{'repo_id':102}})
        self.assertEqual(converted['objects'][0]['head_repo_id'],102)

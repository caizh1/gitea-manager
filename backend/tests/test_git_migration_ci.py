"""原始 CI 文件覆盖、未启动作业和丢失文件必须被区分。"""
import unittest
from git_migration.ci import ci_assets


class CITests(unittest.TestCase):
    def test_reports_and_archive_are_separately_retained(self):
        item={'project':{'id':1,'path':'org/repo'},'datasets':[{'category':'作业','rows':[{
            'id':5,'created_at':'2020-01-01T00:00:00Z','started_at':'2020-01-01T00:00:00Z','status':'success',
            'artifacts':[{'file_type':'archive','filename':'artifacts.zip','size':8},{'file_type':'junit','filename':'report.xml.gz','size':7},
                         {'file_type':'metadata','filename':'metadata.gz','size':6}]}]}]}
        files,pending=ci_assets(item,'gitlab','https://source.test')
        self.assertFalse(pending);self.assertEqual(len(files),4)
        self.assertEqual(sum(bool(f.get('web')) for f in files),2)
        self.assertTrue(any('file_type=junit' in f['url'] for f in files))

    def test_erased_and_running_jobs_cannot_complete(self):
        for fields in ({'erased_at':'2020-01-02T00:00:00Z'},{'status':'running'}):
            item={'project':{'id':1,'path':'org/repo'},'datasets':[{'category':'作业','rows':[{'id':1,'created_at':'2020-01-01T00:00:00Z',**fields}]}]}
            files,pending=ci_assets(item,'gitlab','https://source.test');self.assertFalse(files);self.assertTrue(pending)

    def test_unstarted_manual_job_has_history_without_invented_log(self):
        item={'project':{'id':1,'path':'org/repo'},'datasets':[{'category':'作业','rows':[{'id':1,'status':'manual','started_at':None,'created_at':'2020-01-01T00:00:00Z'}]}]}
        self.assertEqual(ci_assets(item,'gitlab','https://source.test'),([],[]))

    def test_legacy_gitea_artifact_is_not_lost_when_rest_list_is_empty(self):
        from git_migration.normalize import objects
        artifact={'id':12345,'name':'真实编译文件','created_at':'2020-01-01T00:00:00Z','expired':False,'legacy_path':'/org/repo/actions/runs/4/artifacts/fixture'}
        item={'project':{'id':1,'path':'org/repo'},'datasets':[{'category':'Actions 制品','path':'/artifacts','rows':[]},{'category':'Actions 历史制品','path':'/legacy','rows':[artifact]}]}
        files,pending=ci_assets(item,'gitea','https://source.test');normalized=objects(item,'gitea')
        self.assertFalse(pending);self.assertEqual(len(files),1);self.assertTrue(files[0]['web'])
        self.assertEqual(normalized['objects'][0]['kind'],'ci_artifact')
        self.assertEqual(files[0]['metadata']['parent_id'],normalized['objects'][0]['source_id'])

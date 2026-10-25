"""统计固定基准与真实权限入口验收：禁止用未知、重复或其他组织数据填充指标。"""
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ai_review.common import encode, ReviewError
from ai_review.store import Store
from ai_review.statistics import overview, period


class StatisticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'review.db')
        self.window = period({'start': '2026-10-01', 'end': '2026-10-15', 'timezone': 'Asia/Shanghai'})
        self.created = datetime(2026, 10, 5, tzinfo=timezone.utc).timestamp()
        with self.store.tx() as c:
            for repo in (1, 2):
                c.execute('INSERT INTO repositories(id,server_id,repo_id,value) VALUES (?,?,?,?)', (repo, 1, repo, '{}'))

    def job(self, ident='one', status='succeeded', created=None, repository=1, result=None):
        with self.store.tx() as c:
            c.execute('INSERT INTO jobs(id,repository,number,generation,status,created,available,updated,config_revision,repo_revision,result) VALUES (?,?,1,1,?,?,?,?,1,1,?)',
                      (ident, repository, status, self.created if created is None else created, self.created, self.created,
                       encode(result) if result is not None else None))

    def issue(self, ident='issue', job='one', severity='P1', repository=1, candidate='N1'):
        with self.store.tx() as c:
            c.execute('INSERT INTO review_issues VALUES (?,?,1,?,NULL,?,?,?,?,1,?,?)',
                      (ident, repository, job, encode({'title':'固定问题', 'severity':severity, 'candidate_id':candidate}), 'present', '固定证据', None,
                       self.created + 999999, self.created + 999999))

    def archive(self, ident='AR-one', job='one', repository=1, finding=None):
        with self.store.tx() as c:
            c.execute('INSERT INTO review_finding_archive VALUES (?,?,1,?)',
                      (ident, repository, encode({'job':job,'original':finding or {'severity':'P1','candidate_id':'N1'}})))

    def feedback(self, ident, target='AR-one', label='valid', status='active', correction=''):
        with self.store.tx() as c:
            c.execute('INSERT INTO review_examples VALUES (?,1,1,1,1,?,?,1,?, ?,0)',
                      (ident, status, target, encode({'sample_type':'ai_feedback','label':label,'correction':correction}), self.created))

    def data(self, **kwargs):
        return overview(self.store, self.window, **kwargs)

    def test_empty_is_not_zero_acceptance(self):
        data = self.data()
        self.assertEqual(data['summary'], dict(total=0,succeeded=0,failed=0,p0=0,p1=0,accepted=None))
        self.assertEqual(len(data['daily']), 15)

    def test_statuses_date_boundary_and_whole_cohort_not_first_page(self):
        for i in range(31): self.job(str(i))
        for status in ('failed','cancelled','superseded','queued','running','publishing','recovering','cancelling','publish_unknown'):
            self.job(status, status)
        self.job('before', created=self.window['lower'] - .001)
        self.job('after', created=self.window['upper'])
        self.job('first', created=self.window['lower'])
        self.job('last', created=self.window['upper'] - .001)
        data = self.data()
        self.assertEqual(data['summary']['total'], 42)
        self.assertEqual(data['summary']['succeeded'], 33)
        self.assertEqual(data['summary']['failed'], 1)
        self.assertEqual(data['daily'][0]['succeeded'], 1)
        self.assertEqual(data['daily'][-1]['succeeded'], 1)
        listing = self.store.jobs(window=self.window)
        self.assertEqual(listing['total'], 42)
        self.assertEqual(len(listing['items']), 20)
        self.assertEqual(sum(d['other'] for d in data['daily']), 8)

    def test_identity_not_repeated_observation_or_import_time(self):
        self.job(); self.issue(severity='P0')
        self.job('rerun', result={'findings':[{'issue_id':'issue','severity':'P0'}], 'lifecycle':{'version':1}})
        self.job('old', created=self.window['lower']-86400); self.issue('old-issue','old','P0')
        data = self.data()
        self.assertEqual(data['summary']['p0'],1)
        self.assertEqual(data['daily'][4]['p0'],1)
        self.assertEqual(data['coverage']['legacy_total'],0)
        self.assertEqual(data['feedback']['unavailable'],1)
        self.assertIsNone(data['summary']['accepted'])

    def test_archive_aliases_feedback_duplicates_conflicts_and_withdrawals(self):
        self.job(); self.issue(); self.archive(); self.archive('AR-alias')
        self.feedback('f1'); self.feedback('f2', target='AR-alias')
        data = self.data()
        self.assertEqual(data['coverage']['legacy_total'],0)
        self.assertEqual(data['coverage']['feedback_supported'],1)
        self.assertEqual(data['summary']['accepted'],1)
        self.feedback('f3', label='false_positive', correction='固定的反证理由')
        self.assertEqual(self.data()['feedback']['conflict'],1)
        self.assertEqual(self.data()['summary']['accepted'],0)
        with self.store.tx() as c: c.execute("UPDATE review_examples SET status='withdrawn'")
        self.assertEqual(self.data()['feedback']['withdrawn'],1)
        self.assertEqual(self.data()['summary']['accepted'],0)

    def test_feedback_categories_and_unsupported_not_unreviewed(self):
        self.job(); self.issue()
        for n, (label,status) in enumerate([('false_positive','active'),('wrong_location','active'),('valid','pending'),('valid','disabled'),('valid','withdrawn')]):
            ident = 'AR-' + str(n)
            self.archive(ident, finding={'severity':'P1','candidate_id':str(n)})
            self.feedback(str(n),target=ident,label=label,status=status)
        self.archive('AR-empty', finding={'severity':'P0','candidate_id':'empty'})
        data = self.data()
        self.assertEqual(data['feedback'], dict(accepted=0,false_positive=1,other=1,conflict=0,withdrawn=1,pending=2,unreviewed=1,unavailable=1))

    def test_legacy_cleanup_and_unstructured_report_are_explicit(self):
        self.job(); self.archive()
        self.job('unstructured',result={'report_markdown':'P0 只是原文，不允许猜测为结构化问题'})
        self.job('legacy', result={'findings':[{'severity':'P0','candidate_id':'N1'}]})
        data = self.data()
        self.assertEqual(data['coverage']['legacy'], {'P0':1,'P1':1})
        self.assertEqual(data['summary']['p0'],0)
        self.assertEqual(data['coverage']['missing_results'],1)
        self.assertEqual(data['coverage']['reports_without_findings'],1)

    def test_both_job_and_repository_scope_are_applied_before_every_aggregation(self):
        self.job(); self.issue(); self.archive(); self.feedback('f1')
        self.job('foreign',repository=2); self.issue('foreign-issue','foreign','P0',repository=2)
        self.job('previous-epoch'); self.issue('previous-epoch-issue','previous-epoch','P0')
        data = self.data(allowed_jobs=['one','foreign'],allowed_repositories=[1])
        self.assertEqual(data['summary'],dict(total=1,succeeded=1,failed=0,p0=0,p1=1,accepted=1))
        self.assertEqual(self.data(allowed_jobs=[])['summary']['total'],0)
        self.assertEqual(self.data(allowed_repositories=[])['coverage']['tracked'],0)
        self.assertEqual(self.store.jobs(allowed_jobs=['one','foreign'],allowed_repositories=[1],window=self.window)['total'],1)

    def test_dates_validate_and_daylight_saving_uses_local_midnight(self):
        for args in ({'start':'invalid'},{'timezone':'../../etc/passwd'},{'start':'2026-10-16','end':'2026-10-15'}, {'start':'2020-01-01','end':'2026-01-01'}):
            with self.subTest(args=args), self.assertRaises(ReviewError): period(args)
        spring = period({'start':'2026-03-08','end':'2026-03-08','timezone':'America/New_York'})
        autumn = period({'start':'2026-11-01','end':'2026-11-01','timezone':'America/New_York'})
        self.assertEqual(spring['upper']-spring['lower'],23*3600)
        self.assertEqual(autumn['upper']-autumn['lower'],25*3600)

    def test_partial_archive_does_not_hide_other_structured_findings(self):
        self.job(result={'findings':[{'severity':'P1','candidate_id':'N1'}, {'severity':'P0','candidate_id':'N2'}]})
        self.archive()
        self.assertEqual(self.data()['coverage']['legacy'], {'P1':1,'P0':1})


class StatisticsPermissionTests(unittest.TestCase):
    def setUp(self):
        from tests.test_organization_access import OrganizationAccessTest
        self.fixture = OrganizationAccessTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)

    def test_real_authenticated_route_and_foreign_repository(self):
        f = self.fixture
        response = f.request('/api/ai-review/statistics')
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['summary']['total'],1)
        self.assertEqual(f.request('/api/ai-review/statistics',client=f.admin,binding=None).json['summary']['total'],2)
        self.assertEqual(f.request('/api/ai-review/statistics?repository=2').status_code,404)
        self.assertEqual(f.app.test_client().get('/api/ai-review/statistics').status_code,401)
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertEqual(f.request('/api/ai-review/statistics?start=bad').status_code,400)

    def test_same_repository_previous_binding_cannot_leak_into_stats(self):
        f = self.fixture
        with f.state.tx() as c:
            now = time.time()
            c.execute('INSERT INTO jobs(id,repository,number,generation,status,created,available,updated,config_revision,repo_revision,access_scope) VALUES (?,1,1,1,?,?,?,?,1,1,?)',
                      ('c'*32,'failed',now,now,now,encode({'binding_id':2,'instance_epoch':f.epoch})))
        self.assertEqual(f.request('/api/ai-review/statistics').json['summary']['failed'],0)
        self.assertEqual(f.request('/api/ai-review/statistics',client=f.admin,binding=None).json['summary']['failed'],1)

    def test_archived_repository_is_platform_only(self):
        from models import db
        from access.scopes import ResourceScope
        f = self.fixture
        with f.app.app_context():
            ResourceScope.query.filter_by(kind='ai_repository',resource_key='1').one().archived = True
            db.session.commit()
        # 归档沿用现有权限：原组织也不可读取，平台保留历史。
        self.assertEqual(f.request('/api/ai-review/statistics?repository=1').status_code,404)
        self.assertEqual(f.request('/api/ai-review/statistics').json['summary']['total'],0)
        response = f.request('/api/ai-review/statistics?repository=1',client=f.admin,binding=None)
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['summary']['total'],1)


if __name__ == '__main__':
    unittest.main()

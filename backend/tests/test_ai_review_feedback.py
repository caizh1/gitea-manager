"""辅助意见容错、人工标注边界、归档修订和参考失效的固定回归。"""
import copy
import contextlib
import json
import time
import unittest
import zipfile
from unittest.mock import patch
import test_ai_review as base_tests
import test_ai_review_quality as quality_tests
from ai_review import advisory, dataset, feedback
from ai_review.common import ReviewError, digest, encode
from ai_review.engine import review


class AdvisoryTests(unittest.TestCase):
    def unit(self):return quality_tests.QualityTests().unit()

    def test_optional_evidence_does_not_hide_valid_inline(self):
        _,unit,row=self.unit()
        for key in ('support','trace','before_behavior','after_behavior'):row.pop(key)
        rows,_=advisory.parse({'findings':[row]})
        result,rejected=advisory.confirm(rows,{'checks':[dict(candidate_id=rows[0]['candidate_id'],verdict='confirmed',finding=row,location_verified=True,reason='固定代码核验原因成立')]},unit)
        self.assertFalse(rejected);self.assertEqual(result[0]['anchor']['line'],3)
        self.assertEqual(result[0]['assessment'],'confirmed')

    def test_null_finding_missing_checks_and_malformed_check_preserve_original(self):
        _,unit,row=self.unit();rows,_=advisory.parse({'findings':[row]})
        for checks in ([],None,[dict(candidate_id=rows[0]['candidate_id'],verdict='confirmed',finding=None)],['无效对象']):
            result,rejected=advisory.confirm(rows,{'checks':checks},unit)
            self.assertFalse(rejected);self.assertEqual(result[0]['explanation'],row['explanation'])
            self.assertEqual(result[0]['assessment'],'uncertain');self.assertIsNone(result[0]['anchor'])

    def test_only_substantive_rejection_removes_candidate(self):
        _,unit,row=self.unit();rows,_=advisory.parse({'findings':[row]})
        for reason in (True,{},'',None):
            accepted,_=advisory.confirm(rows,{'checks':[dict(candidate_id=rows[0]['candidate_id'],verdict='rejected',reason=reason)]},unit)
            self.assertEqual(len(accepted),1)
        accepted,rejected=advisory.confirm(rows,{'checks':[dict(candidate_id=rows[0]['candidate_id'],verdict='rejected',reason='入口检查已经阻止该操作序列')]},unit)
        self.assertFalse(accepted);self.assertEqual(len(rejected),1)

    def test_per_candidate_errors_and_no_usable_output(self):
        rows,errors=advisory.parse({'findings':[{'title':'检查持久化状态','explanation':'提前丢弃可能导致重启后映射丢失'},None,{'title':'缺原因'}]})
        self.assertEqual(len(rows),1);self.assertEqual(len(errors),2)
        for value in ({},None,{'findings':[{}]}):
            with self.assertRaises(ReviewError):advisory.parse(value)

    def test_exact_location_guards_remain(self):
        _,unit,row=self.unit()
        for changes in ({'quote':'相似但不同原文'},{'anchor_id':'过期锚点'},{'operation_id':'错误根因操作'}):
            changed=dict(row,**changes);rows,_=advisory.parse({'findings':[changed]})
            accepted,_=advisory.confirm(rows,{'checks':[dict(candidate_id=rows[0]['candidate_id'],verdict='confirmed',finding=changed,location_verified=True,reason='假设问题成立')]},unit)
            self.assertIsNone(accepted[0]['anchor']);self.assertTrue(accepted[0]['explanation'])


class DatasetTests(unittest.TestCase):
    setUp=base_tests.StoreTests.setUp
    def sample(self,cid=101,label='false_positive',reason='已有状态约束排除此操作顺序',repo=None,author=1):
        return dataset.put(self.store,repo or self.repo,1,cid,dict(status='active',label=label,target='AR-源意见',correction=reason,conditions=reason,
            source_comment=91,source_hash=digest('人工来源'),original={'title':'原始标题','explanation':'原始说明'},path='code.c',symbols=['save','discard'],confirmed_by=author),author)

    def test_revision_export_and_disable_preserve_original(self):
        first=self.sample();same=self.sample();self.assertEqual(same['revision'],1)
        changed=self.sample(reason='新的人工纠正，仍保留原始来源');self.assertEqual(changed['revision'],2)
        self.assertEqual(changed['original'],first['original'])
        dataset.disable(self.store,self.repo,first['id']);self.assertFalse(dataset.active_rows(self.store,self.repo))
        with zipfile.ZipFile(dataset.export(self.store,self.repo,True)) as z:
            raw=z.read('审查数据集.jsonl');rows=[json.loads(v) for v in raw.splitlines()]
            self.assertEqual([r['revision'] for r in rows],[1,2,3]);self.assertNotIn('仅测试凭据',raw.decode())
        self.assertEqual(self.sample()['status'],'disabled')

    def test_conflict_scope_budget_and_current_source_only(self):
        self.sample();self.sample(cid=102,label='valid',reason='',author=2)
        self.assertFalse(dataset.active_rows(self.store,self.repo))
        dataset.withdraw(self.store,self.repo,102,'明确撤销冲突标注')
        self.assertEqual(len(dataset.active_rows(self.store,self.repo)),1)
        item={'path':'code.c','before':{'code':'save(record);'},'after':{'code':'discard(record);'}}
        found=dataset.retrieve(self.store,self.repo,item)
        self.assertEqual(len(found),1);self.assertNotIn('anchor',encode(found));self.assertLessEqual(len(encode(found)),2000)
        self.assertEqual(dataset.retrieve(self.store,999,item),[])
        self.assertEqual(dataset.retrieve(self.store,self.repo,dict(item,path='unrelated.c',before={'code':'consume(other);'},after={'code':'free(other);'})),[])

    def test_duplicate_notification_immediately_invalidates_once(self):
        sample=self.sample();repo=self.store.repo(self.repo)
        event={'action':'edited','pull_request':{'number':1},'comment':{'id':91,'body':'新原文','updated_at':'2026-09-12T00:00:00Z'},'sender':{'id':1,'login':'writer'}}
        dataset.enqueue(self.store,repo,'delivery-1',event,'issue_comment');dataset.enqueue(self.store,repo,'delivery-1',event,'issue_comment')
        self.assertFalse(dataset.active_rows(self.store,self.repo))
        rows=dataset.list_examples(self.store,self.repo);self.assertEqual(rows['items'][0]['revision'],2);self.assertEqual(rows['pending_events'],1)

    def test_expired_source_not_provided_to_model(self):
        sample=self.sample()
        with self.store.tx() as c:c.execute('UPDATE review_example_validation SET expires=0')
        self.assertFalse(dataset.active_rows(self.store,self.repo))
        self.assertEqual(dataset.list_examples(self.store,self.repo)['total'],1)

    def test_related_code_receives_same_reference_as_primary_scope(self):
        self.sample()
        item={'path':'config.h','before':{'code':'#define COUNT 8'},'after':{'code':'#define COUNT 9'},
              'context':[{'path':'code.c','code':'void save(void) { discard(record); }'}]}
        self.assertEqual(len(dataset.retrieve(self.store,self.repo,item)),1)
        self.assertFalse(dataset.retrieve(self.store,self.repo,dict(item,context=[])))

    def test_literal_return_feedback_matches_parser_function_without_old_anchor(self):
        sample=self.sample()
        # 使用真实源码解析器的完整声明，不能用手写函数名掩盖协议差异。
        from ai_review.snapshot import functions
        scope=functions('int read_status(void) { return 1; }','code.c')[0][0]['name']
        sample.update(symbols=[],original={'title':'返回值需要纠正','explanation':'已有契约允许该返回值','scope':scope,'quote':'return 1;'})
        item={'path':'code.c','before':{'code':'int read_status(void) { return 0; }'},'after':{'code':'int read_status(void) { return 1; }'}}
        found=dataset.retrieve(self.store,self.repo,item,[sample])
        self.assertEqual(len(found),1);self.assertNotIn('anchor',encode(found))
        item['after']['code']='int other_status(void) { return 1; }';item['before']['code']=''
        self.assertFalse(dataset.retrieve(self.store,self.repo,item,[sample]))
        self.assertFalse(dataset.scope_symbols({'scope':'<file>'}))
        self.assertEqual(dataset.scope_symbols({'scope':'read_status'}),{'read_status'})

    def test_archive_survives_task_pruning(self):
        snap,unit,row=quality_tests.QualityTests().unit();snap.update(head='a'*40,base='b'*40,merge_base='b'*40,coverage={'complete':True,'skipped':[]});jobid=self.store.enqueue(self.repo,1,{'head':snap['head']});job=self.store.claim()
        finding=dict(row,quality_version=3,candidate_id='候选一',path=unit['path'],scope=unit['scope']['name'])
        result={'findings':[finding],'evidence':[{'path':unit['path'],'scope':unit['scope'],'input':unit}]}
        self.store.update(jobid,job['owner'],snapshot=snap,result=result);self.store.finish(jobid,job['owner'],'succeeded')
        dataset.archive_pending(self.store)
        with self.store.tx() as c:c.execute('UPDATE jobs SET updated=?',(time.time()-40*86400,))
        self.store.prune();self.assertIsNone(self.store.job(jobid)['snapshot'])
        with contextlib.closing(self.store.connect()) as c:self.assertEqual(c.execute('SELECT count(*) FROM review_finding_archive').fetchone()[0],1)

    def test_refutation_transport_error_preserves_advisory(self):
        snap,unit,row=quality_tests.QualityTests().unit();snap.update(head='a'*40,base='b'*40,merge_base='b'*40,coverage={'complete':True,'skipped':[]});jobid=self.store.enqueue(self.repo,1,{'head':snap['head']});job=self.store.claim()
        calls=[]
        def caller(*args):
            calls.append(args)
            if len(calls)==2:raise ReviewError('模型响应超时')
            return {'findings':[row]}
        result=review(self.store,job,dict(self.store.config(),max_calls=4,max_findings=10),snap,[unit],caller)
        self.assertEqual(len(result['findings']),1);self.assertEqual(result['findings'][0]['assessment'],'uncertain')
        self.assertIsNone(result['findings'][0]['anchor']);self.assertFalse(result['coverage']['complete'])


class PermissionClient:
    prefix='/repos/test/code'
    def __init__(self,permission='write'):
        self.permission=permission;self.calls=[]
    def request(self,method,path,*args,**kwargs):
        self.calls.append(path)
        if path=='/users/writer':return {'id':1,'login':'writer'}
        if path.endswith('/permission'):return {'permission':self.permission}
        raise AssertionError(path)
    def repository(self):return {'html_url':'http://gitea/test/code'}


class FeedbackTests(unittest.TestCase):
    setUp=base_tests.StoreTests.setUp
    def command(self,body):return {'id':121,'body':body,'html_url':'http://gitea/test/code/pulls/1#issuecomment-121'}
    def archived(self):
        with self.store.tx() as c:c.execute('INSERT INTO review_finding_archive VALUES (?,?,?,?)',('AR-例子',self.repo,1,encode({'original':{'title':'标题','explanation':'问题原因','quote':'save(item);','scope':'save','path':'code.c'},'head':'a'*40})))

    def test_readonly_cannot_mark_or_forge_user(self):
        for user in ({'id':1,'login':'writer'},{'id':2,'login':'writer'}):
            self.assertFalse(feedback.can_write(PermissionClient('read'),user))
        self.assertFalse(feedback.can_write(PermissionClient(),{'id':2,'login':'writer'}))
        with self.assertRaises(feedback.CommandError):feedback.apply_command(self.store,PermissionClient('read'),self.store.repo(self.repo),1,self.command('/ai-review 反馈 AR-例子 有效'),{'id':1,'login':'writer'})

    def test_unit_team_requires_code_write_and_actual_membership(self):
        class TeamClient(PermissionClient):
            code='write';member=1
            def repository(self):return {'owner':{'login':'organization'}}
            def pages(self,*args,**kwargs):return [{'id':4,'permission':'none','units_map':{'repo.code':self.code,'repo.pulls':'write'}}]
            def request(self,method,path,*args,**kwargs):
                if path=='/orgs/organization':return {'id':3}
                if path=='/teams/4/members/writer':return {'id':self.member}
                return super().request(method,path,*args,**kwargs)
        client=TeamClient('none');user={'id':1,'login':'writer'}
        self.assertTrue(feedback.can_write(client,user))
        client.code='read';self.assertFalse(feedback.can_write(client,user))
        client.code='write';client.member=2;self.assertFalse(feedback.can_write(client,user))

    def test_suspended_account_cannot_keep_confirming(self):
        client=PermissionClient();original=client.request
        client.request=lambda method,path,*a,**kw: {'id':1,'login':'writer','prohibit_login':True} if path=='/users/writer' else original(method,path,*a,**kw)
        self.assertFalse(feedback.can_write(client,{'id':1,'login':'writer'}))

    def test_bounded_exclusion_and_same_author_replacement(self):
        self.archived();client=PermissionClient();repo=self.store.repo(self.repo);user={'id':1,'login':'writer'}
        pending=feedback.apply_command(self.store,client,repo,1,self.command('/ai-review 反馈 AR-例子 排除 以后不要再报告此类问题'),user)
        self.assertEqual(pending['status'],'pending')
        active=feedback.apply_command(self.store,client,repo,1,self.command('/ai-review 反馈 AR-例子 排除 条件=该接口由主机保证请求已经完成'),user)
        self.assertEqual(active['status'],'active');self.assertEqual(active['revision'],2)
        bad=copy.deepcopy(repo);bad['id']=999
        with self.assertRaises(feedback.CommandError):feedback.apply_command(self.store,client,bad,1,self.command('/ai-review 反馈 AR-例子 有效'),user)

    def test_human_link_must_be_current_pr_same_origin(self):
        client=PermissionClient();repo=self.store.repo(self.repo);user={'id':1,'login':'writer'}
        for url in ('https://public.example/code#issuecomment-1','http://gitea/test/code/pulls/2#issuecomment-1','http://gitea/test/other/pulls/1#issuecomment-1','http://gitea/test/code/pulls/1#issuecomment-1?x'):
            with self.assertRaises(feedback.CommandError):feedback.apply_command(self.store,client,repo,1,self.command('/ai-review 收录 '+url+' 可达操作次序以及实际影响'),user)

    def test_old_event_does_not_authorize_new_body(self):
        repo=self.store.repo(self.repo);client=PermissionClient();client.pr=lambda number: {}
        source={'id':121,'body':'/ai-review 反馈 AR-例子 有效','updated_at':'后来时间','user':{'id':1}}
        event={'number':1,'comment':121,'action':'edited','sender':{'id':1,'login':'writer'},'body_hash':digest('旧命令'),'updated_at':'原时间'}
        with patch.object(feedback,'comment_at',return_value=(source,None)),patch.object(feedback,'apply_command') as apply:
            feedback.process_event(self.store,repo,event,client,client);apply.assert_not_called()

class FeedbackWebhookTests(unittest.TestCase):
    setUp=base_tests.StoreTests.setUp
    def client(self):
        from flask import Flask
        from auth import init_auth
        from routes.ai_review_routes import ai_review_bp
        app=Flask(__name__);app.config.update(SECRET_KEY='测试会话秘密',AI_REVIEW_DB=str(self.store.path))
        init_auth(app);app.register_blueprint(ai_review_bp,url_prefix='/api/ai-review')
        return app.test_client()

    def test_signed_native_review_and_comment_events_only_enqueue(self):
        import hmac,hashlib
        client=self.client();payload={'repository':{'id':7,'full_name':'test/code'},'pull_request':{'number':1,'base':{'repo':{'id':7}},'html_url':'https://review.internal/gitea/test/code/pulls/1'},'action':'reviewed','sender':{'id':1,'login':'writer'}}
        raw=encode(payload).encode();headers={'X-Gitea-Signature':hmac.new('签名测试秘密'.encode(),raw,hashlib.sha256).hexdigest(),'X-Gitea-Delivery':'review-1','X-Gitea-Event':'pull_request_approved','X-Gitea-Event-Type':'pull_request_review_approved'}
        with patch('ai_review.gitea.Gitea.request',side_effect=AssertionError('Webhook不应访问网络')):
            response=client.post(f'/api/ai-review/webhooks/{self.repo}',data=raw,headers=headers)
        self.assertEqual(response.status_code,202);self.assertEqual(self.store.jobs()['total'],0)
        self.assertEqual(self.store.repo(self.repo)['feedback_origins'],['https://review.internal/gitea/test/code'])
        headers['X-Gitea-Signature']='0'*64
        self.assertEqual(client.post(f'/api/ai-review/webhooks/{self.repo}',data=raw,headers=headers).status_code,403)

    def test_dataset_endpoints_require_login_and_csrf(self):
        client=self.client();root=f'/api/ai-review/repositories/{self.repo}/dataset'
        for suffix in ('','/export','/DS-不存在/history'):
            self.assertEqual(client.get(root+suffix).status_code,401)
        with client.session_transaction() as session:session['_user_id']='1';session['_fresh']=True
        self.assertEqual(client.get(root).status_code,200)
        self.assertEqual(client.post(root+'/DS-不存在/disable',json={}).status_code,403)
        self.assertEqual(client.get(root,headers={'Origin':'https://unrelated.example'}).status_code,403)

class ReceiptTests(unittest.TestCase):
    setUp=base_tests.StoreTests.setUp
    def test_lost_create_response_recovers_without_duplicate(self):
        from ai_review.common import PublishUnknown
        class Bot:
            prefix='/repos/test/code'
            def __init__(self):self.rows=[];self.posts=0
            def pages(self,*args,**kwargs):return self.rows
            def request(self,method,path,data):
                if method=='POST':
                    self.posts+=1;self.rows.append({'id':123,'user':{'id':9},'body':data['body']})
                    raise PublishUnknown('已发送但响应丢失')
                raise AssertionError('不应再次写入')
        bot=Bot();repo=self.store.repo(self.repo)
        with self.assertRaises(PublishUnknown):feedback.acknowledge(self.store,bot,repo,1,77,'已保存的人工标注')
        feedback.acknowledge(self.store,bot,repo,1,77,'已保存的人工标注');self.assertEqual(bot.posts,1)

    def test_unknown_receipt_does_not_blindly_resend(self):
        class Bot:
            prefix='/repos/test/code'
            def pages(self,*args,**kwargs):return []
            def request(self,*args,**kwargs):raise AssertionError('未知结果不能重发')
        with self.store.tx() as c:c.execute('INSERT INTO review_feedback_receipts(repository,comment,attempted) VALUES (?,?,1)',(self.repo,77))
        with self.assertRaises(ReviewError):feedback.acknowledge(self.store,Bot(),self.store.repo(self.repo),1,77,'确认回执')

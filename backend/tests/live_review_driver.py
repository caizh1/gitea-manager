"""真实 Gitea 发布竞争测试驱动；仅测试控制器持有合成仓库管理员凭据。"""
import base64
import json
import os
import sys
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet
from ai_review.common import ReviewError, PublishUnknown, Stale, WriteRejected
from ai_review.gitea import Gitea
from ai_review.snapshot import capture, work_items
from ai_review.store import Store
from ai_review.publish import Publisher


def execute(data):
    os.environ['AI_REVIEW_GITEA_HOSTS']='gitea'
    with tempfile.TemporaryDirectory() as root:
        key=Path(root)/'主密钥';key.write_bytes(Fernet.generate_key());os.environ['AI_REVIEW_MASTER_KEY_FILE']=str(key)
        store=Store(Path(root)/'review.db')
        cfg=store.config();cfg.update(enabled=True,debounce_seconds=0);store.save_config(cfg)
        admin=Gitea('http://gitea:3000',data['admin'],data['repo'],data['repo_id'])
        client=Gitea('http://gitea:3000',data['bot'],data['repo'],data['repo_id'])
        repo=store.save_repo(1,data['repo_id'],{'enabled':True,'bot_id':client.request('GET','/user')['id'],'repo_name':data['repo'],
              'bot_token':store.seal(data['bot']),'gitea_url':'http://gitea:3000'})
        store.enqueue(repo['id'],data['number'],{'test':data['mode']});job=store.claim()
        original=client.request; pushed=False; writes=[]
        def push():
            nonlocal pushed
            if pushed:return
            pushed=True
            admin.request('POST',admin.prefix+'/contents/race-trigger.txt',{'branch':data['branch'],'message':'发布期间插入合成提交',
                'content':base64.b64encode('仅用于版本竞争验证\n'.encode()).decode()})
        def request(method,path,*args,**kwargs):
            nonlocal pushed
            payload=args[0] if args else {}
            if method=='POST' and path.endswith('/reviews'):
                writes.append(payload.get('event'))
                if data['mode']=='rejected':
                    # 在真实写入前撤销本合成仓库的机器人读取权限，验证实际拒绝。
                    admin.request('DELETE', admin.prefix+'/collaborators/review-bot')
                    try:
                        return original(method,path,*args,**kwargs)
                    finally:
                        admin.request('PUT', admin.prefix+'/collaborators/review-bot', {'permission':'read'})
                if data['mode']=='partial_write':
                    # 实际 Gitea 留下一条不完整草稿，模拟正式创建接口中途失败。
                    # 不属于正常 Worker 发布流程，不用于替代真实模型成功链路。
                    original(method,path,dict(payload,event='PENDING',comments=payload['comments'][:1]))
                    raise PublishUnknown('测试驱动模拟实际部分写入后响应丢失')
            value=original(method,path,*args,**kwargs)
            if data['mode']=='diff_race' and path.endswith('.diff'):push()
            if method=='POST' and path.endswith('/reviews'):
                event=payload.get('event')
                if data['mode']=='response_lost' and event=='COMMENT' and not pushed:
                    pushed=True;raise PublishUnknown('测试驱动已丢弃一次真实成功响应')
            return value
        client.request=request
        state='未执行';snapshot=None;result=None
        try:
            snapshot=capture(client,data['number'],store.config());work_items(snapshot)
            anchors=[a for a in snapshot['anchors'].values() if a['side']=='new' and 'REVIEW_TEST_TARGET' in a['quote']]
            if len(anchors)!=1:raise RuntimeError('测试输入需要唯一预期锚点')
            a=anchors[0]
            result={'coverage':snapshot['coverage'],'findings':[{'anchor':a,'severity':'P2','title':'真实发布协议验收',
                'trigger':'合成测试输入','explanation':'固定位置验收；不表示模型判断','impact':'检查发布版本绑定',
                'recommendation':'核对自动报告','path':a['path'],'scope':'probe','fingerprint':'测试'+job['id']}]}
            store.update(job['id'],job['owner'],snapshot=snapshot,result=result)
            pub=Publisher(store,client,job,store.repo(repo['id']),snapshot,result)
            if data['mode']=='before_publish':push()
            if data['mode']=='expired_owner':
                with store.tx() as c:c.execute('UPDATE jobs SET lease=0 WHERE id=?',(job['id'],))
            try:pub.publish()
            except PublishUnknown:
                if data['mode']=='response_lost':
                    pub.publish(recovery=True)
                elif data['mode']=='partial_write':
                    try:pub.publish(recovery=True)
                    except PublishUnknown:pass
                    state='部分写入待核对'
                else:raise
            except WriteRejected:
                state='明确拒绝写入'
            if state=='未执行':state='已发布'
        except Stale:state='已阻止过期发布'
        reviews=original('GET',client.prefix+f'/pulls/{data["number"]}/reviews')
        ordinary=original('GET',client.prefix+f'/issues/{data["number"]}/comments')
        actual_head=client.pr(data['number'])['head']['sha']
        comments=[c for r in reviews for c in original('GET',client.prefix+f'/pulls/{data["number"]}/reviews/{r["id"]}/comments')]
        timeline=original('GET',client.prefix+f'/issues/{data["number"]}/timeline')
        expected_stale=data['mode'] in ('diff_race','before_publish','expired_owner')
        passed=(not reviews and state=='已阻止过期发布') if expected_stale else (
            len(reviews)==1 and reviews[0]['state']=='COMMENT' and reviews[0]['commit_id']==snapshot['head']
            and len(comments)==1 and comments[0]['path']=='probe.c' and comments[0]['position']==2
            and sum(t['type']=='review' for t in timeline)==1)
        if data['mode']=='partial_write':
            passed=len(reviews)==1 and reviews[0]['state']=='PENDING' and writes==['COMMENT'] and state=='部分写入待核对'
        elif data['mode']=='rejected':
            passed=not reviews and writes==['COMMENT'] and state=='明确拒绝写入'
        elif not expected_stale:
            passed=passed and writes==['COMMENT']
        passed=passed and any(c['body']=='开发者原有讨论，请保留。' for c in ordinary)
        return {'场景':data['mode'],'通过':bool(passed),'状态':state,'快照Head':snapshot['head'] if snapshot else None,
                '当前Head':actual_head,'Review数量':len(reviews),'评论数量':len(comments),
                'Review版本':[r['commit_id'] for r in reviews], 'Worker提交事件':writes, '实际位置':[(c['path'],c['position'],c['original_position']) for c in comments]}


if __name__=='__main__':
    try: print(json.dumps(execute(json.load(sys.stdin)),ensure_ascii=False))
    except Exception as exc:
        import traceback
        frames=[{'文件':Path(f.filename).name,'行':f.lineno,'函数':f.name} for f in traceback.extract_tb(exc.__traceback__)]
        print(json.dumps({'通过':False,'错误类型':type(exc).__name__,'错误':str(exc) if isinstance(exc,ReviewError) else '内部测试异常','调用位置':frames,'说明':'不包含变量值、凭据或远端响应'},ensure_ascii=False));sys.exit(1)

"""真实 Gitea 的分批发布门禁；模型使用固定协议样本。"""
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

from cryptography.fernet import Fernet
from ai_review import batch_worker, batch_store
from ai_review.gitea import Gitea
from ai_review.store import Store


def main():
    fixture = json.load(sys.stdin)
    def client(*_):
        return Gitea('http://inline-gitea:3000', fixture['token'], fixture['repo_name'], fixture['repo_id'])
    with tempfile.TemporaryDirectory() as folder:
        key = Path(folder)/'master.key'; key.write_bytes(Fernet.generate_key())
        os.environ['AI_REVIEW_MASTER_KEY_FILE'] = str(key)
        os.environ['AI_REVIEW_WORKSPACE_ROOT'] = folder
        store = Store(Path(folder)/'review.db')
        store.save_config({'revision':1, 'enabled':True, 'model':'协议替身', 'debounce_seconds':0,
                           'batch_auto_calls':100, 'tokenizer_profile':'qwen38'})
        repo = store.save_repo(1, fixture['repo_id'], {'enabled':True,
            'bot_token':store.seal(fixture['token']), 'webhook_secret':store.seal('隔离测试'),
            'repo_name':fixture['repo_name'], 'bot_id':fixture['bot_id'], 'gitea_url':'http://inline-gitea:3000'})
        observer = client(); pr = observer.pr(fixture['number'])
        ident = store.enqueue(repo['id'], fixture['number'], {'head':pr['head']['sha'], 'base':pr['base']['sha']})
        calls = []
        def respond(_store, job, _cfg, _workspace, _token, diff_snapshot):
            payload = json.loads(diff_snapshot['_batch_messages'][1]['content'])
            calls.append(job['session_id'])
            if 'candidates' in payload:
                answer = {'checks':[{'id':c['id'],'decision':'confirmed','reason':'固定测试证据已核对',
                                     'evidence_ids':c['evidence_ids']} for c in payload['candidates']]}
            else:
                row = next((r for r in payload['evidence'] if r['path']=='main.c' and r['kind']=='+' and r['anchor_id']), None)
                answer = {'summary':'固定样本完成','limitations':[], 'findings':[]}
                if row:
                    answer['findings'] = [{'severity':'P2','title':'固定发布测试意见','trigger':'隔离样本',
                        'explanation':'用于验证发布链路','impact':'不代表真实缺陷','recommendation':'人工忽略此测试意见',
                        'anchor_id':row['anchor_id'],'evidence_ids':[row['id']]}]
            return {'review_scope':'diff','report_markdown':json.dumps(answer), '_review_completion':{'complete':True},
                    '_native_result_sha256':'a'*64, '_native_final_event_seq':1}
        path = observer.prefix+'/pulls/'+str(fixture['number'])+'/reviews'
        with patch('ai_review.worker.client_for', side_effect=client), patch('ai_review.native_review.execute', side_effect=respond), \
             patch('ai_review.native_review.acknowledge_result'), patch('ai_review.worker.refresh_pr'):
            for _ in range(100):
                job = store.claim()
                assert job, store.job(ident)['error']
                batch_worker.process(store, job)
                current = store.job(ident)
                if current['status']=='succeeded':
                    break
                assert current['status']=='queued', current['error']
                assert not observer.pages(path), '必要单元完成前不得正式发布'
            else:
                raise AssertionError('分批流程未结束')
        plan = batch_store.get(store, ident)
        assert all(u['state']=='done' for u in plan['units'])
        assert len(calls)==len(set(calls)), '每个批次必须独立会话'
        reviews = observer.pages(path)
        assert len(reviews)==1, '只允许一次正式发布'
        comments = observer.pages(path+'/'+str(reviews[0]['id'])+'/comments')
        assert comments and all(c['path']=='main.c' for c in comments)
        assert current['result']['coverage']['complete']
        print(json.dumps({'结果':'通过','单元数':len(plan['units']),'模型协议调用数':len(calls),
                          '正式审查数':len(reviews),'行内评论数':len(comments),
                          '边界':'真实 Gitea 与 Manager；模型为协议替身'},ensure_ascii=False))
        observer.close()


if __name__ == '__main__':
    main()

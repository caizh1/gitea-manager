"""隔离 Gitea 内的真实行内发布；模型报告为固定样本，不评价模型判断质量。"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from cryptography.fernet import Fernet
from ai_review import diff_review
from ai_review.common import Stale
from ai_review.gitea import Gitea
from ai_review.publish import Publisher
from ai_review.store import Store


def main():
    config = json.load(sys.stdin)
    client = Gitea('http://inline-gitea:3000', config['token'], config['repo_name'], config['repo_id'])
    snapshot = diff_review.capture(client, config['number'], {'revision': 1})
    selected = []
    wanted = [('main.c', 'new'), ('main.c', 'old'), ('added.c', 'new'),
              ('deleted.c', 'old'),
              ('space name.c', 'new'), ('unicode.c', 'new')]
    assert not [a for a in snapshot['anchors'].values() if a['path'] == 'renamed.c'], '重命名不发布错位评论'
    assert not [a for a in snapshot['anchors'].values() if a['path'] == 'no_newline.c'], '无换行标记不发布错位评论'
    for path, side in wanted:
        matches = [a for a in snapshot['anchors'].values() if a['path'] == path and a['side'] == side]
        assert matches, (path, side)
        selected.append(matches[0])
    for path, side in [('main.c', 'new'), ('main.c', 'old'), ('added.c', 'new'), ('deleted.c', 'old'), ('unicode.c', 'new')]:
        selected.append([a for a in snapshot['anchors'].values() if a['path'] == path and a['side'] == side][-1])
    # 连续替换块中部的片段只剩一侧，覆盖 Gitea 重算零长度侧坐标的情况。
    for side in ('old', 'new'):
        matches = [a for a in snapshot['anchors'].values() if a['path'] == 'replacement.c' and a['side'] == side]
        selected.extend([matches[4], matches[-1]])
    formats = ('p2 {title} 定位{id}', '## [P2] {title}\n定位 {id}',
        '### **[P2] {title}**\n**定位：** `{id}`', '- **P2 {title}**\n定位:{id}')
    text = '## 验收报告\n以下为定位验收样本，非真实缺陷判断。\n' + '\n'.join(
        formats[i % len(formats)].format(title=f'定位样本 {i + 1}', id=a['id']) +
        f"\n\n这是 {a['path']} 的固定发布验收内容。\n"
        for i, a in enumerate(selected)) + '\n## 无行内位置的意见\nrenamed.c 和 no_newline.c 的意见完整保留在总报告。\n## 待确认\n这里不产生行内意见。'
    result = diff_review.adapt({'review_scope': 'diff', 'report_markdown': text,
        '_review_completion': {'complete': True}}, snapshot)
    assert len(result['findings']) == len(selected)
    with tempfile.TemporaryDirectory() as folder:
        key = Path(folder) / 'master.key'; key.write_bytes(Fernet.generate_key())
        os.environ['AI_REVIEW_MASTER_KEY_FILE'] = str(key)
        store = Store(Path(folder) / 'review.db')
        store.save_config({'revision': 1, 'enabled': True, 'debounce_seconds': 0, 'model': '协议替身'})
        repo = store.save_repo(1, config['repo_id'], {'enabled': True,
            'bot_token': store.seal(config['token']), 'webhook_secret': store.seal('隔离验收'),
            'repo_name': config['repo_name'], 'bot_id': config['bot_id'], 'gitea_url': 'http://inline-gitea:3000'})

        def job_for(value, suffix):
            # 同一 PR 的独立任务身份，便于验证去重及恢复。
            ident = store.enqueue(repo['id'], config['number'],
                {'head': snapshot['head'], 'base': snapshot['base']}, manual=True)
            job = store.claim()
            assert job and job['id'] == ident
            store.update(ident, job['owner'], snapshot=snapshot, result=value)
            return job

        # 复现用户所述：同一个 PR 曾被旧版本审查过，只有总报告。
        legacy = copy.deepcopy(result)
        legacy['findings'] = []
        legacy.pop('inline_parser_version', None)
        old_job = job_for(legacy, '旧版总报告')
        old_publisher = Publisher(store, client, old_job, repo, snapshot, legacy)
        old_review = old_publisher.publish()
        assert old_publisher.publish(recovery=True) == old_review
        assert not old_publisher.comments({'id': old_review})
        store.finish(old_job['id'], old_job['owner'], 'succeeded')

        job = job_for(result, '行内发布验收')
        publisher = Publisher(store, client, job, repo, snapshot, result)
        try:
            review_id = publisher.publish()
        except Exception:
            print(json.dumps({'失败现场固定补丁': snapshot['diff'],
                '回读': [publisher.comments(r) for r in client.pages(publisher.path)]},
                ensure_ascii=False), file=sys.stderr)
            raise
        assert publisher.publish(recovery=True) == review_id
        reviews = client.pages(publisher.path)
        assert len(reviews) == 2, '恢复时重复发布'
        review = next(r for r in reviews if r['id'] == review_id)
        assert f'新增行内评论 {len(selected)} 条' in review['body']
        comments = publisher.comments(review)
        assert len(comments) == len(selected)
        evidence = [{'文件': c['path'], '新侧行': c['position'], '旧侧行': c['original_position'],
                     '回读补丁': c['diff_hunk']} for c in comments]

        # 模拟现场：远端已有正式 Review，本地仍待核对，后续任务因此阻塞。
        from ai_review.attention import request_reconciliation
        store.finish(job['id'], job['owner'], 'publish_unknown', '隔离注入：评论回读尚未确认')
        request_reconciliation(store, job['id'])
        waiting = store.enqueue(repo['id'], config['number'],
            {'head': snapshot['head'], 'base': snapshot['base']}, manual=True)
        assert store.claim() is None, '待核对时后续任务不应绕过阻塞'
        # 启动真正的 Worker 主循环，而非绕过模型门禁直接调用核对函数。
        maintenance = Path(folder) / 'maintenance'
        maintenance.write_text('隔离验收：维护期间核对必须暂停。')
        installed = Path(folder) / 'installed-runtime.json'
        installed.write_text(json.dumps({'ready': False, 'reason': '隔离注入：模型执行器不可用'}, ensure_ascii=False))
        environment = dict(os.environ, AI_REVIEW_DB=str(store.path), AI_REVIEW_RUNTIME_STATUS=str(installed),
            AI_REVIEW_EXECUTOR_URL='http://127.0.0.1:9', AI_REVIEW_AUTHORIZATION_URL='',
            GM_UPGRADE_MAINTENANCE_FILE=str(maintenance), MANAGEMENT_DIAGNOSTICS=folder + '/diagnostics')
        worker_log = Path(folder) / 'worker.log'
        with worker_log.open('w') as log:
            process = subprocess.Popen([sys.executable, '-m', 'ai_review.worker'], env=environment,
                                       stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if any(h['name'] == 'worker' for h in store.health_status()):
                        break
                    assert process.poll() is None, worker_log.read_text()[-1000:]
                    time.sleep(.1)
                else:
                    raise AssertionError('真实工作进程未启动：' + worker_log.read_text()[-1000:])
                time.sleep(1.1)
                assert store.job(job['id'])['reconciliation']['state'] == 'queued', '维护期间擅自执行发布核对'
                maintenance.unlink()
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    actual = store.job(job['id'])
                    if actual['status'] == 'succeeded' or actual['reconciliation']['state'] == 'failed':
                        break
                    assert process.poll() is None, worker_log.read_text()[-1000:]
                    time.sleep(.1)
                assert store.job(job['id'])['status'] == 'succeeded', (
                    store.job(job['id'])['reconciliation'], worker_log.read_text()[-1000:])
                assert store.jobs(status='running')['total'] == 0, '执行器不可用时擅自开始模型审查'
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
        assert store.job(job['id'])['status'] == 'succeeded', store.job(job['id'])['error']
        assert not store.job(job['id'])['attention']['required'], '已核对任务仍阻塞 PR'
        assert [r['id'] for r in client.pages(publisher.path)] == [r['id'] for r in reviews], '核对过程改动 Review 集合'
        following = store.claim()
        assert following and following['repository'] == repo['id'], '核对成功后后续任务仍未被领取'
        store.finish(following['id'], following['owner'], 'failed', '隔离验收：已验证队列领取，不调用模型')

        # 强制分页只针对验收；每页继续走相同回读和固定版本校验。
        paged = copy.deepcopy(result)
        for i, finding in enumerate(paged['findings']):
            finding['title'] += ' 分页'; finding['fingerprint'] += '-page'
        paged['publication_limit'] = 3
        job2 = job_for(paged, '分页验收')
        p2 = Publisher(store, client, job2, repo, snapshot, paged)
        p2.publish(); count = len(client.pages(p2.path))
        p2.publish(recovery=True)
        assert len(client.pages(p2.path)) == count == 2 + (len(selected) + 2) // 3
        store.finish(job2['id'], job2['owner'], 'succeeded')

        job3 = job_for(copy.deepcopy(result), '历史去重验收')
        p3 = Publisher(store, client, job3, repo, snapshot, copy.deepcopy(result))
        dedup_id = p3.publish()
        assert not p3.comments({'id': dedup_id}), '历史意见重复发布'
        dedup_review = next(r for r in client.pages(p3.path) if r['id'] == dedup_id)
        assert f'历史重复 {len(selected)} 项' in dedup_review['body']
        store.finish(job3['id'], job3['owner'], 'succeeded')

        stale_result = copy.deepcopy(result)
        job4 = job_for(stale_result, '版本变化验收')
        stale = dict(snapshot, head='f' * 40)
        try:
            Publisher(store, client, job4, repo, stale, stale_result).publish()
        except Stale:
            pass
        else:
            raise AssertionError('版本变化后仍允许发布')
        assert len(client.pages(publisher.path)) == count + 1
        print(json.dumps({'结果': '通过', 'Gitea版本': client.version(), '真实行内评论': evidence,
            '恢复不重复': True, '分页及分页恢复': True, '历史去重': True, '版本保护': True,
            '旧版总报告不阻止新行内评论': True, '格式差异发布': True, '发布统计回读': True,
            '未知状态只读恢复': True, '后续队列解除': True,
            '真实工作进程执行器不可用仍核对': True, '维护期间核对暂停': True,
            '边界': '真实 Gitea 和生产发布器，模型报告是固定样本；模型定位遵循率待内网验证'}, ensure_ascii=False))


if __name__ == '__main__': main()

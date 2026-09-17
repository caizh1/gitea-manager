"""独立工作进程；不启动 Manager，不接触其 SSH、Docker 或管理数据库。"""
import contextlib
import json
import logging
import os
import signal
import threading
import time

from werkzeug.serving import make_server

from .broker import create_broker
from .common import ReviewError, Stale, PublishUnknown, encode
from .engine import review
from .gitea import Gitea
from .publish import Publisher
from .snapshot import capture, work_items
from .store import Store


def client_for(store, repo, job=None):
    guard = (lambda: store.active(job['id'], job['owner'])) if job else None
    return Gitea(repo['gitea_url'], store.unseal(repo['bot_token']), repo['repo_name'], repo['repo_id'], guard)


def reconcile_repositories(store):
    if not store.config()['enabled']:
        return
    now = time.time()
    for repo in store.repos():
        if not repo['enabled'] or repo['last_reconcile'] > now-300:
            continue
        with store.tx() as c:
            c.execute('UPDATE repositories SET last_reconcile=? WHERE id=?', (now, repo['id']))
        client = client_for(store, repo)
        try:
            client.repository()
            rows = client.pages(client.prefix+'/pulls?state=open')
            opened = set()
            for pr in rows:
                if pr.get('state') != 'open' or pr.get('merged'):
                    continue
                number = pr['number']; opened.add(number)
                store.enqueue(repo['id'], number, {'head': pr['head']['sha'], 'base': pr['base']['sha']})
            with contextlib.closing(store.connect()) as c:
                tracked = [r[0] for r in c.execute('SELECT number FROM pr_state WHERE repository=?', (repo['id'],))]
            for number in set(tracked)-opened:
                store.enqueue(repo['id'], number, {'closed': True}, closed=True)
            store.repo_status(repo['id'], reconcile_error='')
        except ReviewError as exc:
            store.repo_status(repo['id'], reconcile_error=str(exc))
        finally:
            client.close()
        # 每轮只核对一个仓库，避免一次扫描长期占用任务执行。
        return


def process(store, job, recovering=False):
    repo, config = store.repo(job['repository']), store.config()
    client = client_for(store, repo, job)
    stopped = threading.Event()
    started = time.monotonic()
    publisher = None
    def heartbeat():
        while not stopped.wait(20):
            try:
                if time.monotonic()-started > config['task_timeout_seconds']:
                    actual = store.job(job['id'])
                    status = 'publish_unknown' if actual['status'] in ('publishing','recovering') else 'failed'
                    store.finish(job['id'], job['owner'], status, '任务达到执行时间上限，发布中的请求需核对实际结果')
                    return
                store.heartbeat(job['id'], job['owner'])
                store.health('worker', '独立工作进程正在执行审查')
            except Stale:
                return
            except Exception:
                # 数据库短暂不可用时不伪造租约，后续主动操作会再次验证。
                logging.warning('AI Review 租约续期失败：任务 %s', job['id'])
    thread = threading.Thread(target=heartbeat, daemon=True); thread.start()
    try:
        store.active(job['id'], job['owner'])
        if recovering:
            snapshot, result = job['snapshot'], job['result']
        else:
            snapshot = capture(client, job['number'], config)
            with contextlib.closing(store.connect()) as c:
                existing = c.execute("SELECT id,review_id,result,review_fragment FROM jobs WHERE repository=? AND number=? AND status='succeeded' AND json_extract(snapshot,'$.id')=? LIMIT 1",
                                     (job['repository'], job['number'], snapshot['id'])).fetchone()
            if existing and not job['manual']:
                store.update(job['id'], job['owner'], snapshot=snapshot, review_id=existing['review_id'], result=json.loads(existing['result']) if existing['result'] else None, review_fragment=existing['review_fragment'])
                store.finish(job['id'], job['owner'], 'succeeded', '相同代码和配置版本已经完成 Review，未重复调用模型')
                return
            store.update(job['id'], job['owner'], snapshot=snapshot, target_head=snapshot['head'], progress_stage='context', stage='组织修改上下文')
            items = work_items(snapshot, client)
            store.update(job['id'], job['owner'], snapshot=snapshot, units_total=len(items), units_completed=0)
            result = review(store, job, config, snapshot, items)
            store.update(job['id'], job['owner'], snapshot=snapshot, result=result)
        publisher = Publisher(store, client, job, repo, snapshot, result)
        publisher.publish(recovery=recovering)
        store.finish(job['id'], job['owner'], 'succeeded')
        refresh_pr(store, repo, job)
    except Stale as exc:
        actual = store.job(job['id'])
        uncertain = actual['status'] in ('publishing','recovering') and not (publisher and publisher.cancelled)
        status = 'publish_unknown' if uncertain else 'superseded'
        store.finish(job['id'], job['owner'], status, str(exc))
        refresh_pr(store, repo, job)
    except PublishUnknown as exc:
        store.finish(job['id'], job['owner'], 'publish_unknown', str(exc))
    except ReviewError as exc:
        actual = store.job(job['id'])
        state = 'publish_unknown' if actual['status'] in ('publishing', 'recovering') else 'failed'
        store.finish(job['id'], job['owner'], state, str(exc))
    except Exception as exc:
        # 不记录异常正文或堆栈中的源码、配置和请求，错误类型供定位实现缺陷。
        logging.error('AI Review 内部异常：任务 %s，类型 %s', job['id'], type(exc).__name__)
        actual = store.job(job['id'])
        status = 'publish_unknown' if actual['status'] in ('publishing','recovering') else 'failed'
        store.finish(job['id'], job['owner'], status, 'Review 内部异常：' + type(exc).__name__)
    finally:
        stopped.set(); thread.join(timeout=2); client.close()


def refresh_pr(store, repo, job):
    """最终时刻发生 Push 或漏发 Webhook 时立即排入实际新版本。"""
    client = client_for(store, repo)
    try:
        pr = client.pr(job['number'])
        closed = pr.get('state') != 'open' or bool(pr.get('merged'))
        signature = {'head': pr['head']['sha'], 'base': pr['base']['sha']}
        actual = store.job(job['id'])
        snapshot = actual.get('snapshot')
        if not closed and actual['status']=='succeeded' and snapshot and all(snapshot[k]==signature[k] for k in signature):
            # 人工触发使用独立代次，完成后确认该代次读取的真实版本，避免再排入
            # 一条仅用于去重的空任务，也避免 UI 指向更早的同版本 Review。
            with store.tx() as c:
                c.execute('UPDATE pr_state SET signature=? WHERE repository=? AND number=? AND generation=?',
                          (encode([signature,job['config_revision'],job['repo_revision'],False]),repo['id'],job['number'],job['generation']))
        store.enqueue(repo['id'], job['number'], signature, closed=closed)
    except ReviewError:
        pass  # 定期核对继续补偿；已完成的 Review 不能因此被改成失败。
    finally:
        client.close()


def reconcile_unknown(store):
    """未知发布只读取远端结果；不续发草稿、不删除评论、不重发请求。"""
    now = time.time()
    with contextlib.closing(store.connect()) as c:
        row = c.execute("SELECT id FROM jobs WHERE status='publish_unknown' AND available<=? ORDER BY available LIMIT 1", (now,)).fetchone()
        if not row:
            return
    job = store.job(row['id'])
    client, repo = None, None
    completed = False
    started = None
    try:
        with store.lock(f"发布-{job['repository']}-{job['number']}"):
            # 锁内重新检查领取条件，避免并发核对覆盖新回执。
            with store.tx() as c:
                started = time.time()
                claimed = c.execute("""UPDATE jobs SET available=?,reconcile_state='checking',
                    reconcile_started_at=?,reconcile_finished_at=NULL,reconcile_message='正在读取并核对 Gitea 发布结果'
                    WHERE id=? AND status='publish_unknown' AND available<=?""",
                    (started+60, started, job['id'], now)).rowcount
                if not claimed:
                    started = None
                    return
            job = store.job(job['id'])
            if not job['snapshot'] or not job['result']:
                raise PublishUnknown('缺少原任务发布快照或结果，需要管理员核对原请求已经结束且未发布')
            repo = store.repo(job['repository'])
            client = client_for(store, repo)
            publisher = Publisher(store, client, job, repo, job['snapshot'], job['result'])
            rows = client.pages(publisher.path)
            mine = [r for r in rows if publisher.owned(r)]
            if len(mine) != 1:
                raise PublishUnknown('尚未找到唯一正式 Review；未重发，请核对原请求结果')
            if mine[0]['state'] != 'COMMENT':
                raise PublishUnknown('发现未完成草稿；请核对并清理后人工重审，未自动续发')
            actual = client.request('GET', publisher.path + '/' + str(mine[0]['id']))
            publisher.verify(actual)
            reference = publisher.reference(actual)
            with store.tx() as c:
                completed = bool(c.execute("UPDATE jobs SET status='succeeded',stage='已核对正式 Review',error='',review_id=?,review_fragment=?,updated=?,lease=0 WHERE id=? AND status='publish_unknown'",
                                          (reference['review_id'], reference['review_fragment'], time.time(), job['id'])).rowcount)
                c.execute('DELETE FROM capabilities WHERE job=?', (job['id'],))
        if completed:
            refresh_pr(store, repo, job)
    except Exception as exc:
        if started is not None:
            state = 'needs_confirmation' if isinstance(exc, PublishUnknown) else 'failed'
            message = str(exc)[:1000] if isinstance(exc, ReviewError) else '发布核对内部异常：'+type(exc).__name__
            with store.tx() as c:
                c.execute("""UPDATE jobs SET error=?,reconcile_state=?,reconcile_message=?,reconcile_finished_at=?
                    WHERE id=? AND status='publish_unknown' AND reconcile_started_at=?""",
                    (message, state, message, time.time(), job['id'], started))
        elif isinstance(exc, ReviewError):
            # 不覆盖正在执行的核对回执，也不让忙碌 PR 饿死其他待办。
            with store.tx() as c:
                c.execute("UPDATE jobs SET available=? WHERE id=? AND status='publish_unknown' AND available<=?",
                          (time.time()+5, job['id'], now))
    finally:
        if client:
            client.close()


def confirm_unpublished(store, job_id):
    """管理员已核对原请求结束且没有发布；再次只读确认后解除本 PR 阻塞。"""
    job = store.job(job_id)
    client = None
    try:
        with store.lock(f"发布-{job['repository']}-{job['number']}"):
            actual = store.job(job_id)
            if actual['status'] in ('succeeded', 'failed'):
                return  # 已由另一个会话处理，重复确认不产生新的副作用。
            if actual['status'] != 'publish_unknown':
                raise ReviewError('该任务不需要核对发布结果')
            job = actual
            repo = store.repo(job['repository'])
            client = client_for(store, repo)
            client.repository()
            publisher = Publisher(store, client, job, repo, job.get('snapshot') or {}, job.get('result') or {})
            rows = client.pages(publisher.path)
            if any(publisher.owned(r) or r.get('id') == job.get('review_id') for r in rows):
                raise ReviewError('仍存在本任务 Review，请先核对实际结果，不能确认未发布')
            if any(r.get('state') == 'PENDING' and r.get('user', {}).get('id') == repo['bot_id'] for r in rows):
                raise ReviewError('机器人仍有未完成草稿，请在 Gitea 核对处理后再确认')
            with store.tx() as c:
                if not c.execute("UPDATE jobs SET status='failed',stage='已确认未发布',error='共享管理员确认原请求已经结束且未发布，可人工重新审查',updated=?,lease=0 WHERE id=? AND status='publish_unknown'",
                                 (time.time(), job_id)).rowcount:
                    raise ReviewError('任务状态已变化，请刷新后查看')
                c.execute("UPDATE jobs SET reconcile_requested_by='共享管理员' WHERE id=?", (job_id,))
                c.execute('DELETE FROM capabilities WHERE job=?', (job_id,))
    finally:
        if client:
            client.close()


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    store = Store()
    store.validate_storage()
    server = make_server('0.0.0.0', 5051, create_broker(store), threaded=True)
    serving = threading.Thread(target=server.serve_forever, daemon=True); serving.start()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    last_prune = 0
    while not stop.is_set():
        try:
            store.health('worker', '独立工作进程运行中')
            from services.upgrade_maintenance import active
            if active():
                stop.wait(1)
                continue
            reconcile_unknown(store)
            reconcile_repositories(store)
            job = store.claim()
            if job:
                process(store, job)
            else:
                stop.wait(2)
            if time.time()-last_prune > 3600:
                store.prune(); last_prune = time.time()
        except Exception as exc:
            logging.error('AI Review 工作循环失败：%s', type(exc).__name__)
            stop.wait(5)
    server.shutdown()


if __name__ == '__main__':
    main()

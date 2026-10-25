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
from .performance import span, record


def client_for(store, repo, job=None):
    from .authorization import guarded_client
    return guarded_client(store, repo, job)


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


def _cleanup_owned(store, job):
    """只允许当前 owner 回收自己的终态任务；旧 worker 晚返回时什么都不做。"""
    actual = store.job(job['id'], evidence=False)
    owns_job = actual.get('owner') == job['owner']
    terminal = actual.get('status') in ('succeeded', 'failed', 'superseded', 'publish_unknown')
    if not owns_job or not terminal:
        return False
    if actual.get('session_id') and actual.get('session_status') not in ('completed', 'failed', 'aborted', 'unknown'):
        from .native_review import abort
        abort(actual['session_id'])
    from .native_workspace import cleanup
    cleanup(job['id'])
    return True


def _failure_diagnostic(actual, exc, job):
    """保留同会话最先落库的模型失败；执行器随后退出是因果链的末端，
    不能覆盖上游 HTTP/超时根因。
    """
    from datetime import datetime, timezone
    from .diagnostics import safe_local_message, safe_remote
    stored = safe_remote(actual.get('diagnostic') or {})
    stored_message = safe_local_message((actual.get('diagnostic') or {}).get('message'))
    incoming = safe_remote(getattr(exc, 'diagnostic', {}) or {})
    diagnostic = dict(stored)
    code = getattr(exc, 'error_code', '')
    same_origin = (not stored or not incoming or
                   all(not stored.get(key) or not incoming.get(key) or stored[key] == incoming[key]
                       for key in ('session_id', 'call_id', 'request_id')))
    preserve_stored = stored.get('status') == 'failed' and same_origin
    if not preserve_stored:
        diagnostic = incoming
    common = {'phase': diagnostic.get('phase') or actual.get('stage'), 'job': job['id'],
              'unit': job.get('_unit_id'), 'model_phase': job.get('_phase'),
              'time_utc': datetime.now(timezone.utc).isoformat(),
              'exception_type': diagnostic.get('exception_type', type(exc).__name__)}
    if preserve_stored:
        for key,value in common.items():
            diagnostic.setdefault(key,value)
        diagnostic.update(executor_error_code=code or 'EXECUTOR_FAILED',
                          executor_phase=actual.get('stage'))
        if incoming:
            secondary = {key: incoming[key] for key in (
                'error_code', 'source', 'phase', 'reason', 'exception_type', 'native_error_type',
                'status', 'call_id', 'session_id') if incoming.get(key) is not None}
            if any(secondary.get(key) != diagnostic.get(key)
                   for key in ('error_code', 'source', 'phase', 'reason', 'exception_type')):
                diagnostic['secondary'] = secondary
        from .diagnostics import message_for
        diagnostic['message'] = stored_message or message_for(diagnostic.get('error_code'),diagnostic.get('reason'))
        return diagnostic, diagnostic['message']
    if incoming:
        diagnostic.update(incoming)
    for key,value in common.items():
        diagnostic.setdefault(key,value)
    diagnostic.update(status='failed', error_code=diagnostic.get('error_code') or code or 'REVIEW_FAILED',
                      message='')
    if not diagnostic.get('message'):
        from .diagnostics import message_for
        diagnostic['message'] = (safe_local_message(str(exc)) if not incoming else '') or \
            message_for(diagnostic['error_code'],diagnostic.get('reason'))
    if not diagnostic.get('frames'):
        from .diagnostics import safe_frames
        diagnostic['frames'] = safe_frames(exc)
    return diagnostic, diagnostic['message']


def _finish_failure(store, actual, exc, job, state):
    if actual['status'] in ('cancelling', 'cancelled'):
        return actual.get('diagnostic') or {}
    diagnostic, message = _failure_diagnostic(actual, exc, job)
    logging.error('AI 执行诊断 %s', encode(diagnostic))
    try:
        store.update(job['id'], job['owner'], diagnostic=diagnostic)
        store.finish(job['id'], job['owner'], state, message)
    except Exception as recording:
        from .diagnostics import describe
        describe(recording, job, '保存首次异常失败，首次诊断已记录')
    return diagnostic


def _read_failure_actual(store, job, exc):
    try:
        return store.job(job['id'], evidence=False)
    except Exception as recording:
        diagnostic, _ = _failure_diagnostic(dict(job, stage='执行审查'), exc, job)
        logging.error('AI 执行诊断 %s', encode(diagnostic))
        from .diagnostics import describe
        describe(recording, job, '读取失败任务诊断上下文失败，首次诊断已记录')
        return None


def _persisted_native_result(job):
    return bool(job.get('session_id') and job.get('snapshot') and job.get('result')
                and job.get('native_result_sha256') and job.get('final_event_seq'))


def process(store, job, recovering=False):
    # 执行锁覆盖准备、网络和回收；取消确认不能抢先删除工作区。
    with store.lock('execution-' + job['id'], blocking=True):
        try:
            store.active(job['id'], job['owner'])
        except Stale:
            return
        _process(store, job, recovering)


def _process(store, job, recovering=False):
    if job.get('batch_protocol') and not recovering:
        from .batch_worker import process as process_batches
        return process_batches(store, job)
    repo, config = store.repo(job['repository']), store.job_config(job['id'])
    client = client_for(store, repo, job)
    stopped = threading.Event()
    publisher = None
    workspace = None
    record(store,job['id'],'started:preparation','',0)
    preparation_started=time.monotonic()
    def heartbeat():
        renewed = time.monotonic()
        while not stopped.wait(1):
            try:
                with contextlib.closing(store.connect()) as c:
                    current = dict(c.execute('SELECT deadline_at,status FROM jobs WHERE id=?', (job['id'],)).fetchone())
                if current['status'] in ('cancelling', 'cancelled'):
                    client.interrupt()
                    return
                if time.monotonic() - renewed < 20:
                    continue
                renewed = time.monotonic()
                if current.get('deadline_at') and time.time() >= current['deadline_at']:
                    actual = store.job(job['id'])
                    status = 'publish_unknown' if actual['status'] in ('publishing','recovering') else 'failed'
                    store.finish(job['id'], job['owner'], status, '原生审查从准备到发布已达总期限')
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
        from .authorization import authorize
        authorize(repo, job)
        store.active(job['id'], job['owner'])
        if recovering or _persisted_native_result(job):
            snapshot, result = job['snapshot'], job['result']
            if _persisted_native_result(job):
                from .native_review import acknowledge_result
                acknowledge_result(store, job, job['native_result_sha256'], job['final_event_seq'])
        else:
            from .budgets import review_timeout
            from .session_events import create_session
            from .native_workspace import prepare, bind_snapshot_evidence
            from .native_review import execute, adapt
            from . import diff_review
            started_at = job.get('started_at') or time.time()
            planned_deadline = started_at + review_timeout(config)
            workspace_path = os.path.join(os.environ.get('AI_REVIEW_WORKSPACE_ROOT', '/app/review-workspaces'), job['id'])
            session_id, event_token, deadline = create_session(store, job['id'], job['owner'], planned_deadline, workspace_path)
            job['session_id'], job['deadline_at'] = session_id, deadline
            client.deadline_at = deadline
            snapshot = job['snapshot'] if job.get('snapshot') else diff_review.capture(client, job['number'], config)
            diff_only = snapshot.get('review_scope') == 'diff'
            with contextlib.closing(store.connect()) as c:
                existing = c.execute("SELECT id,review_id,result,review_fragment FROM jobs WHERE repository=? AND number=? AND status='succeeded' AND json_extract(snapshot,'$.id')=? LIMIT 1",
                                     (job['repository'], job['number'], snapshot['id'])).fetchone()
            if existing and not job['manual']:
                store.update(job['id'], job['owner'], snapshot=snapshot, review_id=existing['review_id'], result=json.loads(existing['result']) if existing['result'] else None, review_fragment=existing['review_fragment'])
                store.finish(job['id'], job['owner'], 'succeeded', '相同代码和配置版本已经完成 Review，未重复调用模型')
                return
            store.update(job['id'], job['owner'], snapshot=snapshot, target_head=snapshot['head'],
                         progress_stage='context', stage='准备 PR diff' if diff_only else '准备完整固定源码工作区')
            if diff_only:
                workspace = diff_review.prepare(workspace_path)
                workspace_info = {'变更文件数': snapshot['coverage']['files_total']}
            else:
                # 升级前已经固定或提交的任务继续原契约，不重放为新模式。
                workspace, workspace_info = prepare(client, job, snapshot, deadline)
                snapshot = bind_snapshot_evidence(store, job, snapshot, workspace, deadline)
            store.update(job['id'], job['owner'], snapshot=snapshot, workspace_path=workspace,
                         units_total=workspace_info['变更文件数'], progress_stage='analysis',
                         stage='审查 PR diff' if diff_only else '原生 OpenCode 会话自主审查')
            record(store,job['id'],'preparation','',time.monotonic()-preparation_started)
            preparation_started=None
            with span(store,job['id'],'analysis'):
                if diff_only:
                    raw_result = execute(store, job, config, workspace, event_token, diff_snapshot=snapshot)
                    result = diff_review.adapt(raw_result, snapshot)
                else:
                    raw_result = execute(store, job, config, workspace, event_token)
                    result = adapt(raw_result, snapshot, workspace, config['max_findings'], store, job['id'])
            result_sha256 = raw_result.get('_native_result_sha256')
            final_event_seq = raw_result.get('_native_final_event_seq')
            if not result_sha256 or not final_event_seq:
                raise ReviewError('原生审查结果缺少可回收的终态回执')
            store.update(job['id'], job['owner'], snapshot=snapshot, result=result,
                         native_result_sha256=result_sha256, final_event_seq=final_event_seq)
            from .native_review import acknowledge_result
            acknowledge_result(store, job, result_sha256, final_event_seq)
        if preparation_started is not None:
            record(store,job['id'],'preparation','',time.monotonic()-preparation_started)
            preparation_started=None
        if snapshot.get('lifecycle_version') == 1:
            # 主会话已经终态；逐问题复核使用各自请求期限，不继承主会话余量。
            store.update(job['id'], job['owner'], deadline_at=None, stage='核对历史问题', progress_stage='issue_checks')
            client.deadline_at = None
            from .issue_lifecycle import reconcile
            result = reconcile(store, client, job, repo, snapshot, result)
        store.update(job['id'], job['owner'], batch_started=time.time())
        publisher = Publisher(store, client, job, repo, snapshot, result)
        with span(store,job['id'],'publication'):
            publisher.publish(recovery=recovering)
        store.finish(job['id'], job['owner'], 'succeeded')
        refresh_pr(store, repo, job)
    except Stale as exc:
        actual = store.job(job['id'])
        if actual['status'] in ('cancelling', 'cancelled'):
            return
        uncertain = actual['status'] in ('publishing','recovering') and not (publisher and publisher.cancelled)
        status = 'publish_unknown' if uncertain else 'superseded'
        store.finish(job['id'], job['owner'], status, str(exc))
        refresh_pr(store, repo, job)
    except PublishUnknown as exc:
        exc.error_code = getattr(exc, 'error_code', 'PUBLISH_UNKNOWN')
        actual = _read_failure_actual(store, job, exc)
        if actual is not None:
            _finish_failure(store, actual, exc, job, 'publish_unknown')
    except ReviewError as exc:
        actual = _read_failure_actual(store, job, exc)
        if actual is not None:
            state = 'publish_unknown' if actual['status'] in ('publishing', 'recovering') else 'failed'
            _finish_failure(store, actual, exc, job, state)
    except Exception as exc:
        from .diagnostics import describe
        diagnostic = describe(exc, job, '执行审查')
        try:
            actual = store.job(job['id'], evidence=False)
            status = 'publish_unknown' if actual['status'] in ('publishing','recovering') else 'failed'
            store.update(job['id'], job['owner'], diagnostic=diagnostic)
            store.finish(job['id'], job['owner'], status, diagnostic['message'])
        except Exception as recording:
            describe(recording, job, '保存首次异常失败，首次诊断已记录')
    finally:
        if preparation_started is not None:record(store,job['id'],'preparation','',time.monotonic()-preparation_started)
        try:
            _cleanup_owned(store, job)
        except Exception:
            logging.warning('AI Review 工作区回收失败：任务 %s', job['id'])
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
                          (encode([signature,store.execution_revision_for(job['config_revision'], c),job['repo_revision'],False]),repo['id'],job['number'],job['generation']))
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
            if job['result'].get('lifecycle'):
                from .issue_publication import recover
                reference, incomplete = recover(publisher)
                state = 'failed' if incomplete else 'succeeded'
                message = '已核对部分发布；其余页面确定未发送，可在新任务中处理' if incomplete else ''
                with store.tx() as c:
                    completed = bool(c.execute("UPDATE jobs SET status=?,stage=?,error=?,review_id=?,review_fragment=?,updated=?,lease=0 WHERE id=? AND status='publish_unknown'",
                        (state, '发布回执已核对', message, reference['review_id'], reference['review_fragment'], time.time(), job['id'])).rowcount)
                    c.execute('DELETE FROM capabilities WHERE job=?', (job['id'],))
                return
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



def confirm_unpublished(store, job_id, actor='共享管理员'):
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
                if not c.execute("UPDATE jobs SET status='failed',stage='已确认未发布',error=?,updated=?,lease=0 WHERE id=? AND status='publish_unknown'",
                                 (actor + '确认原请求已经结束且未发布，可人工重新审查', time.time(), job_id)).rowcount:
                    raise ReviewError('任务状态已变化，请刷新后查看')
                c.execute('UPDATE jobs SET reconcile_requested_by=? WHERE id=?', (actor, job_id))
                c.execute('DELETE FROM capabilities WHERE job=?', (job_id,))
    finally:
        if client:
            client.close()



def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    store = Store()
    store.validate_storage()
    broker = create_broker(store)
    from .session_events import register
    register(broker, store)
    server = make_server('0.0.0.0', 5051, broker, threaded=True)
    serving = threading.Thread(target=server.serve_forever, daemon=True); serving.start()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    def runtime_health():
        from .runtime_status import refresh
        while not stop.is_set():
            try: refresh()
            except Exception as exc: logging.warning('执行器状态刷新失败：%s', type(exc).__name__)
            stop.wait(5)
    health_thread = threading.Thread(target=runtime_health, daemon=True, name='执行器健康缓存')
    health_thread.start()
    from .cancellation import run as cancellation_loop
    cancellation_thread = threading.Thread(target=cancellation_loop, args=(store, stop),
                                           daemon=True, name='单任务取消核对')
    cancellation_thread.start()
    last_prune = 0
    while not stop.is_set():
        try:
            store.health('worker', '独立工作进程运行中')
            from services.upgrade_maintenance import active
            if active():
                stop.wait(1)
                continue
            # 只读回查 Gitea 不依赖模型执行器；维护期间仍禁止更新核对状态。
            reconcile_unknown(store)
            from .runtime_status import available
            if not available():
                stop.wait(2)
                continue
            reconcile_repositories(store)
            job = store.claim()
            if job:
                process(store, job)
            else:
                stop.wait(2)
            if time.time()-last_prune > 3600:
                store.prune(); last_prune = time.time()
        except Exception as exc:
            from .diagnostics import describe
            describe(exc, phase='审查工作循环')
            stop.wait(5)
    server.shutdown()


if __name__ == '__main__':
    main()

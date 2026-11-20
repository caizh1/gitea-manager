"""单独运行的状态评论同步器；失败不会改变审查任务，也不持有模型权限。"""
import base64
import contextlib
import json
import logging
import re
import signal
import threading
import time
from urllib.parse import unquote, urlsplit

from .common import AccessRevoked, ReviewError, WriteRejected, encode
from .gitea import Gitea
from .progress_state import LABELS, RUNNING, closed_state, job_progress
from .publish import prose, marker
from .store import Store

SUMMARY_MARK = '<!-- ai-review:summary:v1 -->'
DATA_PREFIX = '<!-- ai-review:progress:v1:'
# 发布器按原正文核对回执。在同一 PR 的回执确认前，进度服务不能改写总览。
SUMMARY_RESERVED = """EXISTS (
    SELECT 1 FROM jobs j JOIN review_issue_operations o ON o.job=j.id AND o.key='summary'
    WHERE j.repository=p.repository AND j.number=p.number AND (
      o.state IN ('started','unknown') OR
      (j.generation=p.generation AND j.status IN ('publishing','recovering','publish_unknown'))))"""


def state_for(store, repository, number):
    with contextlib.closing(store.connect()) as c:
        state = c.execute(f'SELECT p.*, {SUMMARY_RESERVED} AS summary_reserved FROM pr_state p '
                          'WHERE p.repository=? AND p.number=?', (repository, number)).fetchone()
        row = c.execute('SELECT id FROM jobs WHERE repository=? AND number=? ORDER BY generation DESC LIMIT 1',
                        (repository, number)).fetchone()
    return dict(state) if state else None, store.job(row['id']) if row else None


def payload_for(repo, state, job, enabled, pr, now=None):
    now = time.time() if now is None else now
    progress = job_progress(job, now)
    # 已安装的 v1 页面要求完成数为整数；总数保持未知，因此不展示虚假的 0/N。
    # 仅兼容评论传输格式，Manager 的原生任务计数仍保持 None。
    if progress.get('native_session'):
        progress['units_completed'] = 0
    stopped = not enabled or not repo['enabled'] or closed_state(state) or pr.get('state') != 'open' or pr.get('merged')
    current_head = pr['head']['sha']
    if stopped:
        progress.update(status='stopped', label=LABELS['stopped'], detail='PR 已关闭、合并或 AI Review 已关闭')
    elif progress['head'] and progress['head'] != current_head:
        progress.update(status='outdated', label=LABELS['outdated'], detail='下方任务属于历史版本，当前提交等待审查')
    elif progress['status'] == 'incomplete':
        # 已安装的 v1 模板能展示 failed 和原因；新版同时识别任务的准确状态。
        progress.update(status='failed', task_status='incomplete')
    result = job.get('result') or {}
    snapshot = job.get('snapshot') or {}
    engine = snapshot.get('engine') or (job.get('configuration') or {}).get('engine')
    coverage = result.get('coverage') or snapshot.get('coverage')
    diff_only = progress['review_scope'] == 'diff'
    coverage = ({k: coverage.get(k) for k in ('complete', 'files_reviewed', 'files_total', 'source_read_tracking')}
                if coverage and (not diff_only or job.get('batch_protocol')) else None)
    return dict(progress, attention=job.get('attention'), version=1, repository_id=repo['repo_id'], repository=repo['repo_name'], number=job['number'],
                job=job['id'], generation=state['generation'], job_generation=job['generation'], sequence=state['progress_seq'],
                enabled=bool(enabled and repo['enabled']), current_head=current_head, checked_at=now,
                findings=len(result.get('findings', [])) if result and job['status'] == 'succeeded' and not diff_only else None,
                review_scope='diff' if diff_only else 'repository', review_engine=engine, coverage=coverage,
                pr_agent_protocol=snapshot.get('pr_agent_protocol', job.get('pr_agent_protocol')),
                error=job['error'][:1000], review_id=job.get('review_id') if job['status'] == 'succeeded' else None,
                review_fragment=job.get('review_fragment', '') if job['status'] == 'succeeded' else '')


def format_time(value):
    return time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(value)) if value else '尚无'


def review_scope_note(engine, scope, pr_agent_protocol=None):
    if engine == 'pr_agent':
        from .pr_agent_encoding import LOCAL_GIT_PROTOCOL
        if pr_agent_protocol == LOCAL_GIT_PROTOCOL:
            return 'PR-Agent 从固定 Git 工作区生成变更，并按需扩展变更附近的源码上下文。历史问题复审可补充相关源码。'
        return 'PR-Agent 主审使用 PR 变更和相邻上下文。历史问题复审可补充固定版本中的相关源码。'
    if scope == 'repository':
        return '审查范围：完整 PR diff，按需检索固定版本仓库中的相关源码。'
    if scope == 'diff':
        return '本次以 PR 变更为审查依据，实际覆盖范围见任务详情。'
    return ''


def comment_body(payload):
    encoded = base64.urlsafe_b64encode(encode(payload).encode()).decode()
    text = SUMMARY_MARK + '\n' + DATA_PREFIX + encoded + ' -->\n### AI Code Review · ' + payload['label']
    text += '\n\n' + prose(payload['detail'])
    text += '\n\n审查版本：`' + (payload['head'] or '等待读取') + '`。'
    text += '\n\n接收：' + format_time(payload['received_at'])
    if payload['started_at']:
        text += '；开始：' + format_time(payload['started_at'])
    if payload['finished_at']:
        text += '；结束：' + format_time(payload['finished_at'])
    if payload['elapsed_seconds'] is not None:
        text += f"；耗时：{payload['elapsed_seconds']} 秒"
    if payload['units_total'] is not None:
        text += f"。\n\n完成分析单元：{payload['units_completed']}/{payload['units_total']}。"
    if payload['findings'] is not None:
        text += f"\n\n发现 {payload['findings']} 项问题。"
    coverage = payload['coverage']
    engine = payload.get('review_engine')
    scope = payload.get('review_scope')
    if engine in ('pr_agent', 'codex_cli') or scope == 'diff':
        note = review_scope_note(engine, scope, payload.get('pr_agent_protocol'))
        if note:
            text += '\n\n' + note
    if coverage:
        if coverage.get('source_read_tracking') == 'native_commands':
            text += f" 已提供 {coverage['files_total']} 个变更文件的完整 diff，源码调查记录见原生命令日志。"
        else:
            text += f" 已读取 {coverage['files_reviewed']}/{coverage['files_total']} 个变更文件。"
        if not coverage['complete']:
            text += ' **部分覆盖，请继续检查未覆盖内容。**'
    if payload['review_id']:
        label = f"AI Review {payload['review_id']}"
        fragment = payload['review_fragment']
        text += '\n\n' + (f'[{label}](#{fragment})' if re.fullmatch(r'issuecomment-[1-9][0-9]*', fragment) else label)
    if payload['error']:
        text += '\n\n说明：' + prose(payload['error'])
    return text + '\n\n辅助审查意见，不作为合并门禁。'


class ProgressSync:
    def __init__(self, store, factory=None):
        self.store = store
        from .authorization import guarded_client
        self.factory = factory or (lambda repo: guarded_client(store, repo))

    def refresh_native(self):
        """进程启动后刷新原生状态评论；保留写入回执、不确定窗口及失败退避。"""
        with self.store.tx() as c:
            c.execute('''UPDATE pr_state SET progress_seq=progress_seq+1 WHERE EXISTS
                (SELECT 1 FROM jobs j WHERE j.repository=pr_state.repository AND j.number=pr_state.number
                 AND j.generation=pr_state.generation AND ((j.session_id IS NOT NULL AND j.session_id!='')
                 OR EXISTS (SELECT 1 FROM configuration_history h WHERE h.revision=j.config_revision
                            AND json_extract(h.value,'$.engine') IN ('codex_cli','pr_agent'))))''')

    def pending(self, now=None):
        now = time.time() if now is None else now
        with contextlib.closing(self.store.connect()) as c:
            return [dict(r) for r in c.execute(f'''SELECT p.repository,p.number FROM pr_state p
                WHERE p.progress_next<=? AND (p.progress_seq>p.progress_synced OR EXISTS
                (SELECT 1 FROM jobs j WHERE j.repository=p.repository AND j.number=p.number
                 AND j.generation=p.generation AND j.status IN ('running','publishing','recovering') AND p.progress_sent_at<=?))
                AND NOT {SUMMARY_RESERVED}
                ORDER BY p.progress_next,p.progress_sent_at LIMIT 50''', (now, now-30))]

    def owned(self, row, repo):
        return isinstance(row, dict) and row.get('user', {}).get('id') == repo['bot_id'] and row.get('body', '').startswith(SUMMARY_MARK)

    def belongs(self, row, repo, number):
        expected = '/' + repo['repo_name'] + f'/pulls/{number}'
        prefix = urlsplit(repo['gitea_url']).path.rstrip('/')
        return unquote(urlsplit(row.get('pull_request_url', '') or row.get('html_url', '')).path) == prefix + expected

    def check_summary_body(self, repository, number, comment):
        body = comment['body']
        with contextlib.closing(self.store.connect()) as c:
            managed = c.execute('SELECT body,pending FROM review_issue_summaries WHERE repository=? AND number=?',
                                (repository, number)).fetchone()
            if managed and body in (managed['body'], managed['pending']):
                return
            # 旧代发布可能在新任务创建后才取得回执，仅接管同一 PR 同一评论的确切正文。
            confirmed = c.execute("""SELECT 1 FROM jobs j JOIN review_issue_operations o ON o.job=j.id
                WHERE j.repository=? AND j.number=? AND o.key='summary' AND o.state='done'
                  AND json_extract(o.receipt,'$.comment_id')=? AND json_extract(o.payload,'$.body')=? LIMIT 1""",
                                  (repository, number, comment['id'], body)).fetchone()
            if confirmed:
                return
        if managed:
            raise ReviewError('总览被人工编辑，未自动覆盖')
        try:
            encoded = body.split(DATA_PREFIX, 1)[1].split(' -->', 1)[0]
            previous = json.loads(base64.urlsafe_b64decode(encoded))
            if comment_body(previous) == body:
                return
        except (ValueError, KeyError, IndexError, TypeError):
            pass
        raise ReviewError('历史总览正文无法完整核对，未覆盖')

    def adopt_reference(self, client, repo, job):
        """旧任务没有片段字段时，只读取已记录 Review，并验证作者、SHA 和本系统标记。"""
        head = job_progress(job)['head']
        if job['status'] != 'succeeded' or not job.get('review_id') or job.get('review_fragment') or not head:
            return
        actual = client.request('GET', client.prefix + f"/pulls/{job['number']}/reviews/{job['review_id']}", missing=True)
        if not actual:
            return
        with contextlib.closing(self.store.connect()) as c:
            candidates = c.execute("SELECT id FROM jobs WHERE repository=? AND number=? AND review_id=? AND status='succeeded'",
                                   (repo['id'], job['number'], job['review_id'])).fetchall()
        url = urlsplit(actual.get('html_url', ''))
        if (actual.get('id') != job['review_id'] or actual.get('state') != 'COMMENT'
                or actual.get('user', {}).get('id') != repo['bot_id'] or actual.get('commit_id') != head
                or not self.belongs(actual, repo, job['number'])
                or not re.fullmatch(r'issuecomment-[1-9][0-9]*', url.fragment)
                or not any(marker(r) in actual.get('body', '') for r in candidates)):
            return
        with self.store.tx() as c:
            c.execute("UPDATE jobs SET review_fragment=? WHERE id=? AND status='succeeded' AND review_id=? AND review_fragment=''",
                      (url.fragment, job['id'], job['review_id']))

    def sync(self, repository, number):
        client = None
        try:
            with self.store.lock(f'进度-{repository}-{number}'):
                state, job = state_for(self.store, repository, number)
                if not job or state['summary_reserved'] or state['progress_next'] > time.time():
                    return False
                repo = self.store.repo(repository)
                client = self.factory(repo)
                client.repository()
                pr = client.pr(number)
                self.adopt_reference(client, repo, job)
                # 在联网读取后重新取任务，避免用之前的执行代次覆盖新状态。
                state, job = state_for(self.store, repository, number)
                if not job or state['summary_reserved']:
                    return False
                native = (job.get('configuration') or {}).get('engine') in ('codex_cli', 'pr_agent')
                seq, generation = state['progress_seq'], state['generation']
                payload = payload_for(repo, state, job, self.store.config()['enabled'], pr)
                body = comment_body(payload)
                if native and job['status'] == 'succeeded':
                    with contextlib.closing(self.store.connect()) as c:
                        plan = c.execute('SELECT value FROM review_publication_plans WHERE job=?', (job['id'],)).fetchone()
                    if plan:
                        summary = json.loads(plan['value'])['overview']
                    else:
                        from .publication import overview
                        summary = overview(self.store, job, (job.get('result') or {}).get('publication') or job['result'])
                    body += '\n\n' + summary.removeprefix(SUMMARY_MARK + '\n')
                elif not native:
                    from .issue_lifecycle import overview
                    body += overview(self.store, repo, job, pr)
                comment = None
                if state['summary_id']:
                    comment = client.request('GET', client.prefix + '/issues/comments/' + str(state['summary_id']), missing=True)
                    if comment and (not self.owned(comment, repo) or not self.belongs(comment, repo, number)):
                        raise ReviewError('总体评论归属不匹配，未覆盖现有讨论')
                if comment is None and state['summary_id']:
                    raise ReviewError('原总览评论已删除，未自动补发')
                if comment is None:
                    rows = client.pages(client.prefix + f'/issues/{number}/comments')
                    owned = [r for r in rows if self.owned(r, repo)]
                    if len(owned) > 1:
                        raise ReviewError('发现多个机器人总体评论，请管理员核对后保留一条')
                    comment = owned[0] if owned else None
                    if comment and not self.belongs(comment, repo, number):
                        raise ReviewError('总体评论不属于当前 PR')
                if comment:
                    self.check_summary_body(repository, number, comment)
                    if native and job['status'] != 'succeeded':
                        # 仅从已核对的系统评论保留正式报告。连续失败或重启时不叠加进度正文。
                        previous = comment['body']
                        report = re.search(r'(?m)^<!-- ai-review:auto-summary:[0-9a-f]{32} -->$', previous)
                        if report:
                            body += '\n\n**上一次已发布的审查结果**\n\n' + previous[report.start():]
                # 每次外部写入前重新核对 head/base，不能以缓存 PR 冒充最新版本。
                latest_pr = client.pr(number)
                if any(latest_pr[k]['sha'] != pr[k]['sha'] for k in ('head', 'base')):
                    return False
                with self.store.tx() as c:
                    latest = c.execute('SELECT * FROM pr_state WHERE repository=? AND number=?', (repository, number)).fetchone()
                    if latest['progress_seq'] != seq or latest['generation'] != generation:
                        return False
                    if comment is None and latest['progress_creating']:
                        raise ReviewError('总体评论创建结果待核对；尚未找到原请求结果，未重复创建')
                    c.execute('INSERT INTO review_issue_summaries VALUES (?,?,?,?) ON CONFLICT(repository,number) DO UPDATE SET pending=excluded.pending',
                              (repository, number, comment['body'] if comment else '', body))
                    # 提交前持久化不确定窗口；即使进程在 POST 后崩溃，也只能回读接管。
                    c.execute('UPDATE pr_state SET progress_creating=? WHERE repository=? AND number=?',
                              (int(comment is None), repository, number))
                path = client.prefix + ('/issues/comments/' + str(comment['id']) if comment else f'/issues/{number}/comments')
                if not comment or comment.get('body') != body:
                    try:
                        actual = client.request('PATCH' if comment else 'POST', path, {'body': body})
                    except (AccessRevoked, WriteRejected):
                        # 只有这次写入明确遭拒才能撤销待确认标记；后面的回读失败仍保留回执。
                        with self.store.tx() as c:
                            c.execute('UPDATE pr_state SET progress_creating=0 WHERE repository=? AND number=?', (repository, number))
                            c.execute('UPDATE review_issue_summaries SET pending=NULL WHERE repository=? AND number=?', (repository, number))
                        raise
                    if not isinstance(actual, dict) or type(actual.get('id')) is not int:
                        raise ReviewError('总体评论写入响应无效，等待回读核对')
                    comment_id = actual['id']
                else:
                    comment_id = comment['id']
                actual = client.request('GET', client.prefix + '/issues/comments/' + str(comment_id))
                if not self.owned(actual, repo) or not self.belongs(actual, repo, number) or actual.get('body') != body:
                    raise ReviewError('总体评论回读校验失败')
                latest_pr = client.pr(number)
                changed_version = any(latest_pr[k]['sha'] != pr[k]['sha'] for k in ('head', 'base'))
                now = time.time()
                with self.store.tx() as c:
                    c.execute('UPDATE review_issue_summaries SET body=?,pending=NULL WHERE repository=? AND number=?', (body, repository, number))
                    if changed_version:
                        c.execute('UPDATE pr_state SET progress_seq=progress_seq+1 WHERE repository=? AND number=?', (repository, number))
                    # 只确认本次发送的版本；写入期间到达的新事件保持待同步。
                    c.execute('''UPDATE pr_state SET summary_id=?,progress_creating=0,progress_synced=?,progress_sent_at=?,
                                 progress_next=?,progress_error='',progress_failures=0 WHERE repository=? AND number=?''',
                              (comment_id, seq, now, now+5, repository, number))
                return True
        except Exception as exc:
            message = str(exc) if isinstance(exc, ReviewError) else '状态同步内部异常：' + type(exc).__name__
            try:
                with self.store.tx() as c:
                    c.execute('''UPDATE pr_state SET progress_error=?,progress_failures=progress_failures+1,
                                 progress_next=?+MIN(60,5*(progress_failures+1)) WHERE repository=? AND number=?''',
                              (message[:1000], time.time(), repository, number))
            except Exception:
                logging.warning('AI Review 状态同步错误记录暂时不可写')
            return False
        finally:
            if client:
                client.close()


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    store = Store(); store.validate_storage()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    sync = ProgressSync(store)
    refresh_needed = True
    with store.lock('进度同步进程'):
        while not stop.is_set():
            try:
                store.health('progress', '独立进度同步进程运行中')
                from services.upgrade_maintenance import active
                if active():
                    stop.wait(1)
                    continue
                if refresh_needed:
                    sync.refresh_native()
                    refresh_needed = False
                for item in sync.pending():
                    if stop.is_set():
                        break
                    sync.sync(**item)
                    store.health('progress', '独立进度同步进程运行中')
            except Exception as exc:
                logging.warning('AI Review 状态同步循环异常：%s', type(exc).__name__)
            stop.wait(1)


if __name__ == '__main__':
    main()

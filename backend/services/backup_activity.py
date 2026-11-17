"""只读观测正在执行的备份操作，命令结果和业务控制仍由备份线程处理。"""
import base64
from collections import deque
from contextlib import ExitStack, contextmanager
from datetime import datetime
import json
import logging
from threading import Event, Lock, Thread
from time import monotonic
from types import SimpleNamespace

from flask import current_app

from models import db
from services.operation_progress import update_backup_progress

INTERVAL = 5
SAMPLE_WINDOW = 30

STAGES = {
    'prepare': ('正在准备备份任务', 0),
    'stop_source': ('停止源 Gitea', 5),
    'snapshot': ('正在记录停写状态下的仓库与引用', 10),
    'snapshot_tables': ('记录数据库校验基准', 15),
    'dump': ('正在导出同一停写点的数据库和文件', 20),
    'snapshot_verify': ('复核仓库与引用', 35),
    'freeze_export': ('固化源端备份副本', 40),
    'transfer': ('源站已恢复，正在传输固定副本', 55),
    'save_archive': ('保存本地 ZIP', 55),
    'assemble': ('整理备份内容', 65),
    'manifest': ('校验归档内容并生成清单', 75),
    'resume_source': ('恢复源站服务', 45),
    'commit_snapshot': ('保存 Commit 校验记录', 90),
    'checksum': ('正在生成备份校验信息', 95),
}


def _utc():
    return datetime.utcnow().isoformat() + 'Z'


class BackupActivity:
    def __init__(self, backup, server=None):
        self.backup = backup
        self.app = current_app._get_current_object()
        self.backup_id = backup.id
        self.probe_server = SimpleNamespace(**{
            field: getattr(server, field) for field in ('connection_mode', 'host', 'ssh_port', 'ssh_user')
        }) if server is not None else None
        self.source = None
        self.stage = None
        self.previous_failed = False
        self.action_key = None
        self.last_write = float('-inf')
        self.transfer_sample = None
        self.transfer_speed = None
        self.transfer_eta = None
        self.publish_lock = Lock()
        self.probe_lock = Lock()

    def _publish(self, task, stage, action, started, counts, observed, *, force, previous_failed=False, **sample):
        label, percent = STAGES[stage]
        update_backup_progress(task, stage, label, percent, action, **counts, force=force,
            previous_failed=previous_failed, activity={
                'action_started_at': started,
                'observed_at': observed,
                'source_state': self.source,
                **sample,
            })

    def show(self, stage, action, *, current=None, total=None, unit='', item='', force=False,
             previous_failed=False, heartbeat=False):
        counts = dict(current=current, total=total, unit=unit, item=item)
        if stage == 'transfer' and current is not None:
            now = monotonic()
            if self.transfer_sample and now - self.transfer_sample[0] >= 1:
                self.transfer_speed = max(0, current - self.transfer_sample[1]) / (now - self.transfer_sample[0])
                self.transfer_sample = (now, current)
            elif self.transfer_sample is None:
                self.transfer_sample = (now, current)
            self.transfer_eta = ((total - current) / self.transfer_speed
                                 if total is not None and self.transfer_speed else None)
        changed = stage != self.stage
        if changed:
            self.previous_failed = previous_failed
        previous_failed = self.previous_failed
        key = (stage, action, item)
        if key != self.action_key:
            self.action_key = key
            self.action_started = _utc()
        started = self.action_started
        if changed or force or monotonic() - self.last_write >= 1:
            try:
                # 独立应用上下文使用自己的数据库会话，观测写入不提交备份业务字段。
                with self.app.app_context():
                    self._publish(self.backup_id, stage, action, started, counts, _utc() if heartbeat else None,
                                  force=True, previous_failed=previous_failed,
                                  **({'state': 'active'} if heartbeat else {}),
                                  **({'bytes_per_second': self.transfer_speed, 'eta_seconds': self.transfer_eta}
                                     if stage == 'transfer' else {}))
                db.session.expire(self.backup, [
                    'progress_stage', 'progress_label', 'progress_percent', 'progress_detail',
                    'progress_current', 'progress_total', 'progress_unit', 'progress_item',
                    'progress_updated_at', 'progress_step_started_at',
                ])
            except Exception:
                logging.warning('暂时无法发布备份动作', exc_info=True)
            self.stage = stage
            self.last_write = monotonic()
        # 观测器只使用本次动作的快照，不读取备份线程的 ORM 对象。
        def publish(**sample):
            self._publish(self.backup_id, stage, action, started, counts, _utc(),
                          force=True, previous_failed=previous_failed, **sample)
        return publish

    @contextmanager
    def observe(self, publish, probe, *, api=None):
        done = Event()
        samples = deque()

        def monitor():
            with ExitStack() as resources:
                reader = api
                while not done.wait(INTERVAL):
                    # 上一次探测退出前，不同时建立另一条观测连接。
                    if not self.probe_lock.acquire(blocking=False):
                        continue
                    try:
                        if reader is api and api is not None and self.probe_server is not None:
                            from services.docker_service import managed_client
                            # 单独连接避免只读观测占用备份执行端的 SSH 会话。
                            observer = managed_client(self.probe_server)
                            resources.callback(observer.close)
                            reader = observer.api
                        sample = probe(reader) if api is not None else probe()
                    except Exception:
                        sample = None
                    finally:
                        self.probe_lock.release()
                    if not sample or done.is_set():
                        continue
                    if 'zip_bytes' in sample:
                        now = monotonic()
                        size = sample['zip_bytes']
                        if samples and size < samples[-1][1]:
                            samples.clear()
                        samples.append((now, size))
                        while len(samples) > 1 and samples[1][0] <= now - SAMPLE_WINDOW:
                            samples.popleft()
                        if now - samples[0][0] >= SAMPLE_WINDOW:
                            sample['zip_delta'] = size - samples[0][1]
                            sample['zip_window_seconds'] = round(now - samples[0][0])
                    with self.publish_lock:
                        if not done.is_set():
                            try:
                                with self.app.app_context():
                                    publish(**sample)
                            except Exception:
                                logging.warning('备份进程观测暂时无法写入', exc_info=True)

        thread = Thread(target=monitor, name='backup-observation', daemon=True)
        try:
            thread.start()
        except RuntimeError:
            logging.warning('暂时无法启动备份观测线程', exc_info=True)
            yield
            return
        try:
            yield
        finally:
            done.set()
            # 等待已开始的数据库写入。读取 Docker 的线程即使超时也无法再写入旧步骤。
            with self.publish_lock:
                pass

    def execute(self, container, args, *, stage, action, user=None, archive=None, **counts):
        publish = self.show(stage, action, **counts)
        api = container.client.api
        exec_id = api.exec_create(container.id, args, user=user or '')['Id']

        def probe(reader):
            with reader.get(reader._url('/exec/{0}/json', exec_id), timeout=3) as response:
                response.raise_for_status()
                if not response.json()['Running']:
                    return None
            sample = {'state': 'running'}
            if archive:
                from requests import RequestException
                try:
                    with reader.head(reader._url('/containers/{0}/archive', container.id),
                                  params={'path': archive}, timeout=3) as response:
                        if response.ok:
                            metadata = json.loads(base64.b64decode(response.headers['X-Docker-Container-Path-Stat']))
                            sample['zip_bytes'] = int(metadata['size'])
                except (RequestException, ValueError, KeyError, TypeError):
                    # 可选的文件观测失败时，仍然可以报告已确认的进程状态。
                    pass
            return sample

        with self.observe(publish, probe, api=api):
            raw = api.exec_start(exec_id)
        from services.backup_consistency import command_output
        return command_output(api.exec_inspect(exec_id)['ExitCode'], raw)

    @contextmanager
    def container_state(self, container, stage, action):
        publish = self.show(stage, action, force=True)
        api = container.client.api

        def probe(reader):
            with reader.get(reader._url('/containers/{0}/json', container.id), timeout=3) as response:
                response.raise_for_status()
                return {'state': 'container', 'container_state': response.json()['State']['Status']}

        with self.observe(publish, probe, api=api):
            yield

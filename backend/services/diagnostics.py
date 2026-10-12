"""安装与运维共用的受限诊断记录；原始故障先落盘，附加采集不能覆盖它。"""
import contextlib
import contextvars
import datetime
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import functools
import signal
import subprocess
import tarfile
import time
import traceback
import uuid
import sys

CURRENT = contextvars.ContextVar('operation_diagnostics', default=None)
SOURCE_LIMIT = 2 * 1024 * 1024
RUN_LIMIT = 20 * 1024 * 1024
SECRETS = set()


def register_secret(value):
    if isinstance(value, str) and len(value) >= 4:
        SECRETS.add(value)


def redact(value):
    text = str(value)
    for secret in sorted(tuple(SECRETS), key=len, reverse=True):
        text = text.replace(secret, '[已脱敏]')
    text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)', '[私钥已移除]', text, flags=re.S)
    text = re.sub(r'(?i)(authorization[\s"\x27:=]+)(?:bearer|basic|token)?\s*[^\s,}\r\n]+', r'\1[已脱敏]', text)
    text = re.sub(r'(?i)((?:password|passwd|api[_-]?key|(?:access[_-]?)?token|secret(?:[_-]?key)?)[\s"\x27:=]+)[^\s,}\r\n]+', r'\1[已脱敏]', text)
    text = re.sub(r'(?i)(https?://)[^/\s:@]+:[^/\s@]+@', r'\1[已脱敏]@', text)
    return text


def bounded(value, limit=SOURCE_LIMIT):
    raw = redact(value).encode('utf-8')
    if len(raw) <= limit:
        return raw
    marker = ('\n[日志已截断，原始字节数：%d，保留首尾]\n' % len(raw)).encode()
    half = (limit-len(marker))//2
    return raw[:half] + marker + raw[-half:]


def private_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if isinstance(value, str):
        data = bounded(value)
    else:
        text = redact(json.dumps(value, ensure_ascii=False, indent=2))
        data = text.encode('utf-8')
        if len(data) > SOURCE_LIMIT:
            data = json.dumps({'说明':'结构化日志超过上限，保留首尾摘要','摘要':bounded(text,SOURCE_LIMIT//3).decode('utf-8',errors='replace')},ensure_ascii=False).encode('utf-8')
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(data); f.flush(); os.fsync(f.fileno())
    os.replace(temporary, path)


def error_code(message):
    value=str(message).lower()
    for code, markers in (
        ('CPU_UNSUPPORTED',('cpu lacks avx','illegal instruction')),
        ('BUN_CRASH',('bun has crashed','segmentation fault')),
        ('CONTAINERD_LAYER_BUSY',('failed to unmount','device or resource busy')),
        ('CONTAINER_OOM',('oom killed','oom 杀死','oomkilled')),
        ('DISK_FULL',('no space left on device','空间不足')),
        ('PORT_IN_USE',('address already in use','端口已占用','已占用')),
        ('SSH_KEY_MISSING',('ssh key not found','密钥无法读取')),
        ('SSH_HOST_KEY_CHANGED',('host key for server','host key does not match','指纹变化','指纹发生变化')),
        ('SSH_AUTH_FAILED',('authentication failed','密钥认证失败')),
        ('TIMEOUT',('timed out','超时','超过 30 秒'))):
        if any(marker in value for marker in markers): return code
    return 'OPERATION_FAILED'


class CommandFailure(RuntimeError):
    pass


def diagnostic_io(function):
    @functools.wraps(function)
    def safe(*args, **kwargs):
        try: return function(*args, **kwargs)
        except OSError as exc:
            # 磁盘满等采集故障不能阻止业务回退，也不能替换首个业务异常。
            try: print('诊断写入未完成：'+redact(str(exc)),file=sys.stderr,flush=True)
            except OSError: pass
            return None
    return safe


class Report:
    def __init__(self, root, title, run_id=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.id = run_id or datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', self.id):
            raise ValueError('诊断编号无效')
        self.directory = self.root/self.id
        self.directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.title, self.error, self.step = title, None, '开始'
        self.started = time.time()
        self.sequence = 0
        self.attempts = {}
        for previous in self.root.glob('*/status.json'):
            if previous.parent == self.directory: continue
            try:
                record = json.loads(previous.read_text())
                if record.get('状态') == '运行中' and record.get('进程') and not Path('/proc/'+str(record['进程'])).exists():
                    record['状态'] = '上次运行中断'
                    private_write(previous, record)
            except (ValueError, OSError): pass
        self.event('开始', title)
        self.write('status.json', {'状态': '运行中', '标题': title, '开始时间': self.started, '进程': os.getpid()})

    @diagnostic_io
    def write(self, name, value):
        path = self.directory/name
        used = sum(p.stat().st_size for p in self.directory.rglob('*') if p.is_file())
        if used < RUN_LIMIT-SOURCE_LIMIT or name in ('status.json', 'summary.md'):
            private_write(path, value)
        else:
            self.event('未采集', name, 原因='本次诊断达到容量上限')

    @diagnostic_io
    def event(self, status, message, **details):
        record = {'时间': datetime.datetime.now(datetime.timezone.utc).isoformat(), '操作编号': self.id,
                  '步骤': self.step, '状态': status, '说明': message, **details}
        path = self.directory/'events.jsonl'
        if getattr(self, '_events_truncated', False): return
        if path.exists() and path.stat().st_size > SOURCE_LIMIT-8192:
            self._events_truncated = True
            record = {'时间':record['时间'],'操作编号':self.id,'步骤':self.step,'状态':'截断','说明':'事件日志达到 2 MiB 上限，后续事件省略；首次错误与最终摘要单独保留'}
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'ab') as f:
            safe = {key:(bounded(value,4096).decode('utf-8',errors='replace') if isinstance(value,str) else value) for key,value in record.items()}
            f.write(redact(json.dumps(safe,ensure_ascii=False)).encode()+b'\n'); f.flush()

    def fail(self, exc):
        error = {'错误编号':error_code(exc),'步骤': self.step, '异常类型': type(exc).__name__, '说明': redact(str(exc)),
                 '堆栈': redact(''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)))}
        self.event('失败', str(exc), 异常类型=type(exc).__name__)
        if self.error is None:
            self.error = error
            self.write('first-error.json', error)

    @contextlib.contextmanager
    def stage(self, label):
        previous = self.step; self.step = label; start = time.monotonic()
        print(label, flush=True); self.event('开始', label)
        try:
            yield
        except BaseException as exc:
            self.fail(exc); raise
        else:
            self.event('完成', label, 耗时秒=round(time.monotonic()-start, 2))
        finally:
            self.step = previous

    def command(self, args, *, label, timeout=180, data=None, check=True, env=None, sensitive=False):
        self.sequence += 1
        number = self.sequence
        start = time.monotonic(); last_notice = start
        self.attempts[label]=self.attempts.get(label,0)+1
        self.event('执行', label, 超时秒=timeout,尝试次数=self.attempts[label])
        outputs = {'stdout': bytearray(), 'stderr': bytearray()}
        totals = {'stdout': 0, 'stderr': 0}
        timed_out = False
        with subprocess.Popen(args, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True) as process:
            if data is not None:
                process.stdin.write(data.encode() if isinstance(data, str) else data); process.stdin.close()
            with selectors.DefaultSelector() as selector:
                for key, stream in [('stdout', process.stdout), ('stderr', process.stderr)]:
                    os.set_blocking(stream.fileno(), False); selector.register(stream, selectors.EVENT_READ, key)
                while selector.get_map():
                    now = time.monotonic()
                    if now-start >= timeout:
                        if not timed_out:
                            threads=[]
                            for thread in Path(f'/proc/{process.pid}/task').glob('*'):
                                row={'线程':thread.name}
                                for field in ('comm','wchan','stat'):
                                    try: row[field]=(thread/field).read_text()[:2048]
                                    except OSError as exc: row[field]=str(exc)
                                threads.append(row)
                            self.write(f'logs/{number:04d}-timeout-process.json',{'进程':process.pid,'已等待秒':round(now-start,2),'线程':threads})
                        timed_out = True
                        try: os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError: pass
                    if now-last_notice >= 10:
                        print(f'{label}：已等待 {int(now-start)} 秒，剩余最多 {max(0,int(timeout-(now-start)))} 秒', flush=True)
                        last_notice = now
                    for key, _ in selector.select(.2):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj); continue
                        name = key.data; totals[name] += len(chunk); outputs[name].extend(chunk)
                        if len(outputs[name]) > SOURCE_LIMIT:
                            del outputs[name][SOURCE_LIMIT//2:-SOURCE_LIMIT//2]
                    if timed_out and now-start > timeout+3:
                        break
            try: process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                raise CommandFailure(f'{label}：终止后进程仍未退出') from None
        decoded = {key: bytes(value).decode('utf-8', errors='replace') for key, value in outputs.items()}
        if not sensitive:
            for key, value in decoded.items():
                self.write(f'logs/{number:04d}-{key}.log', f'操作：{label}\n原始字节数：{totals[key]}\n'+value)
        self.event('超时' if timed_out else ('完成' if process.returncode == 0 else '失败'), label,
                   退出码=process.returncode, 退出信号=-process.returncode if process.returncode < 0 else None,
                   耗时秒=round(time.monotonic()-start, 2), 输出已隐藏=sensitive)
        if check and (timed_out or process.returncode):
            detail = '敏感命令输出已隐藏' if sensitive else decoded['stderr'][-3000:] or decoded['stdout'][-1000:]
            raise CommandFailure(f'{label}：'+('超时' if timed_out else f'退出码 {process.returncode}')+'；'+redact(detail))
        return process.returncode, decoded['stdout'], decoded['stderr']

    def collect(self, project=None):
        deadline = time.monotonic()+27
        commands = [('系统内核', ['uname', '-a']), ('CPU 指令集', ['lscpu']),
                    ('磁盘容量', ['df', '-h']), ('inode', ['df', '-i']),
                    ('挂载信息', ['findmnt', '-rn', '-o', 'TARGET,SOURCE,FSTYPE']),
                    ('Docker 版本', ['docker', 'version']),
                    ('存储驱动', ['docker', 'info', '--format', '{{.Driver}} {{json .DriverStatus}}']),
                    ('运行时日志', ['journalctl', '-u', 'docker', '-u', 'containerd', '--since', '@'+str(int(self.started)), '--no-pager', '-n', '400'])]
        host={}
        for title, command in commands:
            left=deadline-time.monotonic()
            if left <= 0:
                self.event('未采集', title, 原因='诊断总预算已用完'); continue
            try:
                rc,out,err=self.command(command,label=title,timeout=min(3,left),check=False)
                host[title]={'退出码':rc,'输出':out[:32000],'错误':err[:2000]}
            except Exception as exc: self.event('未采集', title, 原因=str(exc))
        self.write('host.json',host)
        if not project or time.monotonic() >= deadline:
            return
        try:
            _, out, _ = self.command(['docker','ps','-aq','--filter','label=com.docker.compose.project='+project], label='相关容器', timeout=min(2,deadline-time.monotonic()))
            containers=[]
            for cid in out.split():
                if time.monotonic() >= deadline: break
                # 只返回状态和镜像身份；不导出环境变量、挂载凭据及命令参数。
                _, value, _ = self.command(['docker','inspect','--format','{{json .State}}',cid], label='容器状态 '+cid[:12], timeout=min(2,deadline-time.monotonic()))
                state=json.loads(value)
                _, identity, _ = self.command(['docker','inspect','--format','{{.Image}} {{.RestartCount}} {{.Name}}',cid], label='容器身份 '+cid[:12], timeout=max(.1,min(2,deadline-time.monotonic())),check=False)
                _, limits, _ = self.command(['docker','inspect','--format','{{json .HostConfig.Memory}} {{json .HostConfig.MemorySwap}} {{json .HostConfig.NanoCpus}} {{json .HostConfig.PidsLimit}}',cid],label='容器资源限额 '+cid[:12],timeout=max(.1,min(2,deadline-time.monotonic())),check=False)
                containers.append({'容器':cid, '身份与重启次数':identity.strip(), '状态':state,'资源限额（内存字节、交换总字节、纳核、进程数）':limits.strip()})
                if time.monotonic() < deadline:
                    self.command(['docker','logs','--since',str(int(self.started)),'--timestamps','--tail','500',cid], label='容器日志 '+cid[:12], timeout=min(2,deadline-time.monotonic()), check=False)
            self.write('containers.json', containers)
        except Exception as exc: self.event('未采集','容器附加信息',原因=str(exc))

    @diagnostic_io
    def finish(self, status):
        self.write('status.json', {'状态':status,'标题':self.title,'开始时间':self.started,'结束时间':time.time(),'错误':self.error})
        self.write('summary.md', f'# {self.title}\n\n操作编号：{self.id}\n\n结果：{status}\n\n'+
                   (f'错误编号：{self.error["错误编号"]}\n\n失败步骤：{self.error["步骤"]}\n\n首次错误：{self.error["说明"]}\n\n已确认的是上述失败事实，其他原因尚未确定。' if self.error else '本次操作没有记录阻塞错误。'))
        archive=self.root/('diagnostics-'+self.id+'.tar.gz')
        fd=os.open(str(archive),os.O_CREAT|os.O_WRONLY|os.O_TRUNC,0o600)
        with os.fdopen(fd,'wb') as raw, tarfile.open(fileobj=raw,mode='w:gz') as bundle:
            bundle.add(self.directory, arcname=self.id)
        completed = sorted((p for p in self.root.glob('*/status.json') if p.parent != self.directory), key=lambda p:p.stat().st_mtime)
        used = sum(p.stat().st_size for p in self.root.rglob('*') if p.is_file())
        for index, status_file in enumerate(completed):
            if len(completed)-index <= 9 and used <= 200*1024*1024: break
            try:
                if json.loads(status_file.read_text()).get('状态') == '运行中': continue
                folder = status_file.parent
                old_archive = self.root/('diagnostics-'+folder.name+'.tar.gz')
                size = sum(p.stat().st_size for p in folder.rglob('*') if p.is_file())
                if old_archive.exists(): size += old_archive.stat().st_size; old_archive.unlink()
                shutil.rmtree(folder); used -= size
            except (OSError, ValueError): pass
        print('诊断包：'+str(archive),flush=True)
        return archive


def tracked_operation(kind, model):
    """沿用任务标识，覆盖被业务函数捕获的失败，页面据此定位诊断包。"""
    def decorate(function):
        @functools.wraps(function)
        def wrapped(identifier, *args, **kwargs):
            report = Report(os.environ.get('MANAGEMENT_DIAGNOSTICS_ROOT', '/app/data/diagnostics'), kind)
            item = model.query.get(identifier)
            if item is not None:
                from models import db
                item.diagnostic_id = report.id; db.session.commit()
            report.event('关联','业务操作',业务类型=kind,业务编号=identifier)
            context = CURRENT.set(report)
            try:
                with report.stage(kind):
                    result = function(identifier, *args, **kwargs)
                    item = model.query.get(identifier)
                    if item is None: raise RuntimeError('操作记录不存在')
                    if item.status == 'failed': raise RuntimeError(item.error_msg or '操作失败，业务层未提供原因')
                report.finish('完成')
                return result
            except Exception as exc:
                report.fail(exc)
                item = model.query.get(identifier)
                if item is not None:
                    from models import db
                    item.error_msg = redact(str(exc))[:16000] + '；诊断编号：' + report.id
                    item.status = 'failed'
                    from datetime import datetime
                    item.completed_at = datetime.utcnow()
                    db.session.commit()
                    from services.operation_progress import update_progress
                    update_progress(item, 'failed', kind + '失败', item.progress_percent or 0, item.error_msg)
                report.finish('失败')
                return None
            finally:
                CURRENT.reset(context)
        return wrapped
    return decorate

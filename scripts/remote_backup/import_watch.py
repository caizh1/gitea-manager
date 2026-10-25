"""只对本轮新恢复助手对应的真实 incoming.sql 导入进程注入停止。"""
import inspect
import shlex
import re
import json
import time

from .errors import CheckFailure


def incoming_process(line):
    fields = line.strip().split(None, 1)
    if len(fields) != 2 or not fields[0].isdigit():
        return None
    try:
        args = shlex.split(fields[1])
    except ValueError:
        return None
    # 本机真实 amd64 仿真把 argv 展示为固定 Rosetta 包装；不能扫描任意 shell 正文。
    if len(args) >= 4 and args[:2] == ['{psql}', '/run/rosetta/rosetta']:
        if args[2] != args[3] or not re.fullmatch(r'/usr/lib/postgresql/[0-9]+/bin/psql', args[2]):
            return None
        args = args[3:]
    if not args or args[0].rsplit('/', 1)[-1] != 'psql' or '--single-transaction' not in args:
        return None
    try:
        path = args[args.index('-f') + 1]
        database = args[args.index('-d') + 1]
    except (ValueError, IndexError):
        return None
    matched = re.fullmatch(r'/tmp/(manager-restore-[a-f0-9]{32})/incoming\.sql', path)
    if not matched or database != 'gitea':
        return None
    return {'进程PID': int(fields[0]), '事务标记': matched.group(1), '实际进程': fields[1]}


def journal_matches(value, observed, task, pg, helper):
    marker = observed['事务标记']
    return (value.get('版本') == 1 and value.get('任务') == task
            and value.get('助手') == helper and value.get('数据库容器') == pg
            and value.get('标识') == marker and value.get('阶段') == '数据库修改'
            and value.get('数据库暂存') == '/tmp/' + marker
            and value.get('数据库名称') == 'gitea')


def observer_script():
    """提前就绪；收到本轮任务编号后核对真实进程及持久事务身份，再停止 PG。"""
    return ('import hashlib,json,os,re,select,shlex,subprocess,sys,time\n'
            + inspect.getsource(incoming_process) + inspect.getsource(journal_matches) + '''
pg, previous = sys.argv[1], set(json.loads(sys.argv[2]))
assert re.fullmatch(r'[a-f0-9]{64}', pg), '目标PG完整身份无效'
def output(args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=15).stdout
def cancelled():
    if select.select([sys.stdin], [], [], 0)[0]:
        return sys.stdin.readline().strip() in ('', '取消')
    return False
assert output(['docker','inspect','-f','{{.Id}}','postgres']).strip() == pg, '就绪前PG身份变化'
print(json.dumps({'状态':'观察器就绪','观察器PID':os.getpid(),'目标PG':pg},ensure_ascii=False),flush=True)
control = sys.stdin.readline().strip()
if not control or control == '取消':
    sys.exit(0)
task = json.loads(control)['任务']
assert type(task) is int and task > 0, '必须先绑定本轮真实任务'
deadline = time.monotonic() + 600
samples = []
while time.monotonic() < deadline:
    if cancelled():
        sys.exit(0)
    for line in output(['docker','top',pg,'-eo','pid,args']).splitlines():
        observed = incoming_process(line)
        if observed is None:
            continue
        helpers = output(['docker','ps','-aq','--no-trunc','--filter',
            'label=gitea.manager.operation=' + observed['事务标记']]).splitlines()
        assert len(helpers) == 1 and re.fullmatch(r'[a-f0-9]{64}', helpers[0]), '实际导入没有唯一事务助手'
        assert helpers[0] not in previous, '实际导入不能关联到旧助手'
        journal_raw = output(['docker','exec',helpers[0],'cat','/data/.manager-restore-lock/state.json'])
        journal = json.loads(journal_raw)
        assert journal_matches(journal,observed,task,pg,helpers[0]), '持久事务与本轮任务或PG不一致，拒绝停止'
        assert output(['docker','inspect','-f','{{.Id}}','postgres']).strip() == pg, 'PG身份变化，拒绝停止'
        # 再次读取同一进程，避免将已完成导入或回退的名称当作注入命中。
        current = [incoming_process(row) for row in output(['docker','top',pg,'-eo','pid,args']).splitlines()]
        if observed not in current:
            samples.append({'说明':'事务已确认，但第二次读取时进程消失，未停止PG','进程':observed})
            continue
        if cancelled():
            sys.exit(0)
        before = time.time_ns()
        output(['docker','stop','-t','5',pg])
        state = json.loads(output(['docker','inspect','-f','{{json .State}}',pg]))
        assert not state['Running'], '目标PG未实际停止'
        print(json.dumps({'说明':'实际incoming.sql进程、唯一新事务助手及同一PG身份均核实后停止',
            '实际导入进程':observed,'新事务助手':helpers[0],'目标PG':pg,
            '持久事务SHA256':hashlib.sha256(journal_raw.encode()).hexdigest(),'任务':task,
            '停止请求Unix纳秒':before,'停止后Unix纳秒':time.time_ns(),
            '停止后运行':state['Running'],'停止后退出码':state['ExitCode']},ensure_ascii=False),flush=True)
        sys.exit(0)
    time.sleep(0.03)
print(json.dumps({'说明':'观察器未注入，保留已解析的真实进程窗口','样本':samples},ensure_ascii=False),flush=True)
raise RuntimeError('未观察到实际incoming.sql导入进程，没有停止PG，不能把进度名称当作导入命中')
''')


class ImportObserver:
    """一次有界 SSH 观察会话；退出确认前保留活动标记，防止遗漏待取消观察器。"""
    def __init__(self, lab, pg, helpers):
        self.lab, self.pg, self.helpers = lab, pg, helpers
        self.path = lab.root / 'database-import-observer-active.json'
        self.client = self.channel = None
        self.received, self.errors = bytearray(), bytearray()

    def __enter__(self):
        from .scenarios import write_json
        if self.path.exists():
            raise CheckFailure('存在尚未确认退出的导入观察器，拒绝覆盖或提交新任务')
        self.client = self.lab.ssh('b')
        try:
            self.stdin, stdout, self.stderr = self.client.exec_command(shlex.join(
                ['python3', '-u', '-c', observer_script(), self.pg, json.dumps(self.helpers)]), timeout=15)
            self.channel = stdout.channel
            self.ready = json.loads(stdout.readline())
            if self.ready.get('状态') != '观察器就绪' or self.ready.get('目标PG') != self.pg:
                raise CheckFailure('导入观察器没有明确就绪，尚未提交恢复')
            write_json(self.path, {'说明':'本轮有界观察器，确认远端退出后才删除标记', **self.ready})
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def arm(self, task):
        self.stdin.write(json.dumps({'任务': task}) + '\n'); self.stdin.flush()

    def _receive(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.channel.recv_ready():
                self.received.extend(self.channel.recv(65536))
            if self.channel.recv_stderr_ready():
                self.errors.extend(self.channel.recv_stderr(65536))
            if self.channel.exit_status_ready() and not self.channel.recv_ready() and not self.channel.recv_stderr_ready():
                return self.channel.recv_exit_status()
            time.sleep(0.03)
        raise CheckFailure('导入观察器尚未确认退出，活动标记保留，禁止继续其他破坏阶段')

    def wait(self):
        from .scenarios import write_json
        code = self._receive(625)
        path = self.lab.root / ('database-import-observer-' + str(time.time_ns()) + '.json')
        write_json(path, {'说明':'真实观察器原始输出，仅受限保留；校准不等于注入通过', '就绪':self.ready,
            '退出码':code,'标准输出':self.received.decode(errors='replace'),
            '错误':self.lab.scrub(self.errors.decode(errors='replace'))})
        if code:
            raise CheckFailure('实际导入观察失败，原始错误保留：' + str(path))
        return json.loads(self.received)

    def __exit__(self, *_):
        try:
            if self.channel is not None:
                if not self.channel.exit_status_ready():
                    self.stdin.write('取消\n'); self.stdin.flush(); self.stdin.channel.shutdown_write()
                self._receive(40)
                self.path.unlink(missing_ok=True)
        finally:
            if self.client is not None:
                self.client.close()

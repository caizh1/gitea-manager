"""通过已有运维连接读取 PostgreSQL 时间线标记；固定只读事务，不修改 Gitea 数据。"""
import configparser
import ipaddress
import json
import re
import shlex
import time
from urllib.parse import urlsplit

from .rules import ApprovalError, positive


class Transport:
    def __init__(self, server, deadline):
        self.server, self.deadline = server, deadline
        self.docker = self.ssh = None

    def remaining(self):
        left = self.deadline-time.monotonic()
        if left <= 0:
            raise ApprovalError('原生完整性核验超出总时间预算')
        return min(left, 2)

    def command(self, args):
        self.remaining()
        import paramiko
        from config import SSH_KEY_PATH, SSH_KEY_PASSPHRASE
        if self.ssh is None:
            client=paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.ssh=client
            key=paramiko.RSAKey.from_private_key_file(SSH_KEY_PATH,password=SSH_KEY_PASSPHRASE)
            limit=self.remaining()
            client.connect(self.server.host,port=self.server.ssh_port,username=self.server.ssh_user,pkey=key,
                           timeout=limit,banner_timeout=limit,auth_timeout=limit,allow_agent=False,look_for_keys=False)
        channel=self.ssh.get_transport().open_session(timeout=self.remaining())
        channel.settimeout(self.remaining())
        output=bytearray()
        try:
            channel.exec_command(shlex.join(args))
            while True:
                self.remaining()
                if channel.recv_ready():output.extend(channel.recv(65536))
                if channel.recv_stderr_ready():channel.recv_stderr(65536)
                if len(output)>1024*1024:raise ApprovalError('原生元数据响应超出读取预算')
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():break
                time.sleep(.01)
            if channel.recv_exit_status()!=0:raise ApprovalError('原生元数据只读命令失败')
            return bytes(output)
        finally:channel.close()

    def client(self):
        if self.docker is None:
            import docker
            self.docker=docker.from_env(timeout=self.remaining())
        self.docker.api.timeout=self.remaining()  # 只属于本次读取，不修改共享客户端。
        return self.docker

    def inspect(self, name):
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,199}',name or ''):
            raise ApprovalError('原生元数据容器身份无效')
        if self.server.is_local:
            value=self.client().containers.get(name).attrs
        else:
            value=json.loads(self.command(['docker','inspect','--format','{{json .}}',name]))
        return {key:value[key] for key in ('Id','State','NetworkSettings')}

    def execute(self, container, args, user=None):
        self.remaining()
        if self.server.is_local:
            api=self.client().api
            created=api.exec_create(container,args,user=user)
            output=bytearray()
            stream=api.exec_start(created['Id'],stream=True,demux=True)
            try:
                for stdout,stderr in stream:
                    self.remaining()
                    if stdout:output.extend(stdout)
                    if len(output)>1024*1024:raise ApprovalError('原生元数据响应超出读取预算')
            finally:
                if hasattr(stream,'close'):stream.close()
            if api.exec_inspect(created['Id'])['ExitCode']!=0:raise ApprovalError('原生元数据只读命令失败')
            out=bytes(output)
        else:
            out=self.command(['docker','exec',*(['--user',user] if user else []),container,*args])
        if len(out)>1024*1024:raise ApprovalError('原生元数据响应超出读取预算')
        self.remaining()
        return out.decode('utf-8')

    def close(self):
        if self.docker is not None:self.docker.close()
        if self.ssh is not None:self.ssh.close()


class NativeMetadata:
    def __init__(self, server, deadline):
        self.server=server
        self.transport=Transport(server,deadline)
        self.binding=None

    def close(self):self.transport.close()

    def bind(self):
        t=self.transport;s=self.server
        stage='容器状态'
        try:
            gitea=t.inspect(s.gitea_container);postgres=t.inspect(s.pg_container)
            if not gitea['State']['Running'] or not postgres['State']['Running']:
                raise ApprovalError('原生数据库与 Gitea 必须已运行，不能为接入重启服务')
            stage='原生配置'
            help_text=t.execute(gitea['Id'],['gitea','--help'],user='git')
            match=re.search(r'ConfigFile:\s*(/[^\r\n]+)',help_text)
            if not match:raise ApprovalError('无法确认原生数据库配置文件')
            ini_path=match[1].strip()
            if '\x00' in ini_path or '..' in ini_path.split('/'):
                raise ApprovalError('原生数据库配置路径无效')
            raw=t.execute(gitea['Id'],['cat',ini_path],user='git')
            if len(raw)>65536:raise ApprovalError('原生数据库配置超出读取预算')
            parser=configparser.RawConfigParser(strict=False)
            parser.read_string('[gitea_global]\n'+raw)
            section=parser['database']
            if section.get('DB_TYPE')!='postgres':
                raise ApprovalError('当前完整性适配只验证 PostgreSQL；其他数据库需完成只读适配，未改动原实例')
            database,user=section.get('NAME',''),section.get('USER','')
            schema=section.get('SCHEMA','') or 'public'
            if any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,99}',v) for v in (database,user,schema)):
                raise ApprovalError('原生数据库标识不在已验证范围')
            if database!=s.pg_dbname or user!=s.pg_user:
                raise ApprovalError('Manager 数据库定位与原生配置不一致，未查询其他数据库')
            host=urlsplit('//'+section.get('HOST',''))
            port=host.port or 5432
            if not host.hostname or host.username or host.password or host.path or host.query or host.fragment:
                raise ApprovalError('原生数据库地址不在已验证范围')
            networks=set(gitea['NetworkSettings']['Networks']) & set(postgres['NetworkSettings']['Networks'])
            addresses={n.get(key) for name,n in postgres['NetworkSettings']['Networks'].items() if name in networks
                       for key in ('IPAddress','GlobalIPv6Address') if n.get(key)}
            stage='数据库地址'
            resolved=t.execute(gitea['Id'],['getent','hosts',host.hostname],user='git')
            resolved={str(ipaddress.ip_address(line.split()[0])) for line in resolved.splitlines() if line.strip()}
            if not resolved or not addresses or not resolved <= addresses:
                raise ApprovalError('原生数据库地址未指向 Manager 指定的同一容器，未使用可能错库的标记')
            # app.ini 会在首次启动生成密钥时被写回，mtime 不能证明实际连接；核对 web 进程的数据库套接字。
            stage='原生进程连接'
            runtime_args=['sh','-c','''set -eu
for item in /proc/[0-9]*/comm; do
 [ "$(cat "$item" 2>/dev/null || true)" = gitea ] || continue
 folder=${item%/comm}; command=$(tr '\\000' ' ' < "$folder/cmdline")
 case " $command " in *" web "*)
  awk '{print "进程 " $1" "$22}' "$folder/stat"
  for fd in "$folder"/fd/*; do
   target=$(readlink "$fd" 2>/dev/null || true)
   case "$target" in socket:*) printf '句柄 %s\\n' "$target";; esac
  done
  for family in tcp tcp6; do
   [ ! -f "$folder/net/$family" ] || awk -v family="$family" 'NR>1 {print family" "$2" "$3" "$4" "$10}' "$folder/net/$family"
  done;; esac
done
''','核对原生配置时序',ini_path]
            runtime=t.execute(gitea['Id'],runtime_args,user='git')
            processes,sockets=web_connections(runtime,addresses,port)
            connection_sql=("BEGIN READ ONLY; SET LOCAL statement_timeout='1000ms'; SET LOCAL lock_timeout='100ms'; "
                "SELECT coalesce(json_agg(json_build_object('address',host(client_addr),'port',client_port)),'[]')::text "
                f"FROM pg_stat_activity WHERE datname='{database}' AND usename='{user}' AND client_addr IS NOT NULL; ROLLBACK;")
            stage='数据库会话核对'
            raw=t.execute(postgres['Id'],['psql','-X','-q','-A','-t','-w','-v','ON_ERROR_STOP=1','-p',str(port),
                                         '-U',user,'-d',database,'-c',connection_sql])
            clients={(str(ipaddress.ip_address(item['address'])),item['port']) for item in json.loads(raw)}
            if clients and not sockets.intersection(clients):
                # 正常连接池可能在两次只读观察之间更替套接字。只补查一次同一进程，
                # 不重新查询数据库、不延长预算，也不以磁盘配置或他人进程代替实际匹配。
                stage='原生进程连接补查'
                current_processes,current_sockets=web_connections(
                    t.execute(gitea['Id'],runtime_args,user='git'),addresses,port)
                if current_processes!=processes:
                    raise ApprovalError('连接核验期间原生 web 进程变化，未使用新进程代替旧绑定')
                sockets=current_sockets
            if not sockets or not sockets.intersection(clients):
                raise ApprovalError('当前 web 进程没有连接到所配置数据库，不能使用磁盘配置猜测运行时实例')
            self.binding={'container':postgres['Id'],'database':database,'user':user,'schema':schema,'port':port,
                          'web_pid':processes[0][0],'web_ticks':processes[0][1],
                          'gitea_started':gitea['State']['StartedAt'],'postgres_started':postgres['State']['StartedAt']}
            return self.binding
        except ApprovalError:raise
        except Exception as exc:
            raise ApprovalError('原生时间线只读连接无法核验：'+stage+'（'+type(exc).__name__+'），未完成接入预检') from None

    def snapshot(self, repo_id, number):
        b=self.binding or self.bind()
        repo_id=positive(repo_id,'仓库 ID');number=positive(number,'PR 序号')
        # 最多 5001 条原始位置；超过 5000 会由标记校验拒绝。PG 强制禁止此事务写入。
        sql=f'''BEGIN READ ONLY;
SET LOCAL statement_timeout='1000ms'; SET LOCAL lock_timeout='100ms';
SET LOCAL search_path TO "{b['schema']}";
WITH subject AS (SELECT p.id AS pr_id,i.id AS issue_id,i.repo_id,i.index FROM pull_request p
 JOIN issue i ON i.id=p.issue_id WHERE i.repo_id={repo_id} AND i.index={number}),
ordered AS (SELECT c.id,c.type,c.review_id,row_number() OVER (ORDER BY c.created_unix,c.id) AS ordinal
 FROM comment c JOIN subject s ON s.issue_id=c.issue_id ORDER BY c.created_unix,c.id LIMIT 5001)
SELECT json_build_object('pr_id',(SELECT pr_id FROM subject),'repo_id',(SELECT repo_id FROM subject),
 'number',(SELECT index FROM subject),'count',(SELECT count(*) FROM ordered),
 'max_id',(SELECT coalesce(max(id),0) FROM ordered),
 'events',(SELECT coalesce(json_agg(json_build_object('id',id,'type',type,'review_id',review_id,'ordinal',ordinal) ORDER BY ordinal),'[]')
 FROM ordered WHERE type IN (22,25,29)))::text;
ROLLBACK;'''
        try:
            self.check_running_binding()
            raw=self.transport.execute(b['container'],['psql','-X','-q','-A','-t','-w','-v','ON_ERROR_STOP=1',
                '-p',str(b['port']),'-U',b['user'],'-d',b['database'],'-c',sql])
            self.check_running_binding()
            return json.loads(raw)
        except ApprovalError:raise
        except Exception:raise ApprovalError('原生时间线只读快照失败，未使用不完整 API 数据') from None

    def check_running_binding(self):
        for name,key in ((self.server.gitea_container,'gitea_started'),(self.binding['container'],'postgres_started')):
            current=self.transport.inspect(name)
            if not current['State']['Running'] or current['State']['StartedAt']!=self.binding[key]:
                raise ApprovalError('读取期间原生服务重启或身份变化，未沿用旧连接标记')
        stat=self.transport.execute(self.server.gitea_container,['cat','/proc/'+self.binding['web_pid']+'/stat'],user='git')
        if stat.split()[21]!=self.binding['web_ticks']:
            raise ApprovalError('读取期间原生 web 进程变化，未沿用旧连接标记')


def web_connections(runtime,addresses,port):
    processes=[line.split()[1:] for line in runtime.splitlines() if line.startswith('进程 ')]
    if len(processes)!=1 or len(processes[0])!=2 or any(not item.isdigit() for item in processes[0]):
        raise ApprovalError('无法确认唯一原生 web 进程，未查询完整性标记')
    own_inodes=set(re.findall(r'句柄 socket:\[(\d+)\]',runtime))
    sockets=set()
    for line in runtime.splitlines():
        fields=line.split()
        if len(fields)!=5 or fields[0] not in ('tcp','tcp6') or fields[3]!='01' or fields[4] not in own_inodes:continue
        local_address,local_port=decode_endpoint(fields[1])
        remote_address,remote_port=decode_endpoint(fields[2])
        if remote_address in addresses and remote_port==port:sockets.add((local_address,local_port))
    return processes,sockets


def decode_endpoint(value):
    address,port=value.split(':')
    raw=bytes.fromhex(address)
    if len(raw) not in (4,16):raise ApprovalError('原生连接地址格式错误')
    address=ipaddress.ip_address(b''.join(raw[i:i+4][::-1] for i in range(0,len(raw),4)))
    if isinstance(address,ipaddress.IPv6Address) and address.ipv4_mapped:address=address.ipv4_mapped
    return str(address),int(port,16)

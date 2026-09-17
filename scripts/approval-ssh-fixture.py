#!/usr/bin/env python3
"""隔离 SSH 协议夹具：仅接受临时密钥和本轮两个容器的只读命令。"""
import json
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time

import paramiko

folder=Path(sys.argv[1])
allowed=set(json.loads((folder/'允许容器.json').read_text()))
stop=threading.Event()
host_key=paramiko.RSAKey.generate(2048)
client_key=paramiko.RSAKey.generate(2048)
client_key.write_private_key_file(str(folder/'客户端测试密钥'))
(folder/'客户端测试密钥').chmod(0o600)


def mode():return json.loads((folder/'故障控制.json').read_text()).get('模式','正常')


def execute(channel,command):
    started=time.monotonic()
    args=shlex.split(command.decode())
    proof={'说明':'只记录命令类别，不保存配置正文、令牌或密钥','命令类别':args[:2]}
    try:
        state=mode()
        if state=='持续无输出':stop.wait(12);return
        if state=='过量输出':channel.sendall(b'x'*(1024*1024+65536));return
        if state=='丢弃连接':return
        if args[:2]==['docker','inspect']:
            assert args[2:4]==['--format','{{json .}}'] and len(args)==5 and args[4] in allowed
        elif args[:2]==['docker','exec']:
            index=4 if args[2:4]==['--user','git'] else 2
            assert args[index] in allowed and args[index+1] in ('gitea','cat','getent','sh','psql')
        else:raise ValueError('拒绝非本轮只读容器命令')
        result=subprocess.run(args,capture_output=True,timeout=10)
        if result.stdout:channel.sendall(result.stdout)
        if result.stderr:channel.sendall_stderr(result.stderr)
        channel.send_exit_status(result.returncode)
        proof['退出码']=result.returncode
    except Exception as exc:
        proof['异常类型']=type(exc).__name__
        try:channel.send_exit_status(1)
        except Exception:pass
    finally:
        channel.close()
        proof['耗时秒']=round(time.monotonic()-started,4)
        with (folder/'协议执行记录.jsonl').open('a') as out:out.write(json.dumps(proof,ensure_ascii=False)+'\n')


class Server(paramiko.ServerInterface):
    def check_auth_publickey(self,username,key):
        return paramiko.AUTH_SUCCESSFUL if username=='approval-matrix' and key==client_key and mode()!='拒绝认证' else paramiko.AUTH_FAILED
    def get_allowed_auths(self,username):return 'publickey'
    def check_channel_request(self,kind,chanid):return paramiko.OPEN_SUCCEEDED if kind=='session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
    def check_channel_exec_request(self,channel,command):
        threading.Thread(target=execute,args=(channel,command),daemon=True).start()
        return True


def connection(sock):
    transport=paramiko.Transport(sock)
    channels=[]
    try:
        transport.add_server_key(host_key);transport.start_server(server=Server())
        while transport.is_active() and not stop.is_set():
            channel=transport.accept(.2)
            if channel is not None:channels.append(channel)
            channels=[channel for channel in channels if not channel.closed]
    except Exception:pass
    finally:transport.close()


listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(16);listener.settimeout(.2)
(folder/'服务就绪.json').write_text(json.dumps({'端口':listener.getsockname()[1],'说明':'真实SSH握手与通道；命令通过参数列表执行，仅允许本轮容器'},ensure_ascii=False))
signal.signal(signal.SIGTERM,lambda *args:stop.set())
try:
    while not stop.is_set():
        try:sock,_=listener.accept()
        except socket.timeout:continue
        threading.Thread(target=connection,args=(sock,),daemon=True).start()
finally:listener.close()

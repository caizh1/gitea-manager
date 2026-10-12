"""真实运行时自检；启动时执行一次，保存原始退出证据，健康接口只读结果。"""
import json
import hashlib
import sys
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time
import requests

VERSION = os.environ.get('OPENCODE_EXPECTED_VERSION', '1.18.7')


def environment(home):
    env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':str(home),'LANG':'C.UTF-8',
         'XDG_CONFIG_HOME':str(home/'config'),'XDG_DATA_HOME':str(home/'data'),
         'XDG_CACHE_HOME':str(home/'cache'),'XDG_STATE_HOME':str(home/'state'),
         'OPENCODE_TEST_MANAGED_CONFIG_DIR':str(home/'managed'),
         'OPENCODE_MODELS_PATH':str(home/'models.json'),
         'OPENCODE_CONFIG_CONTENT':json.dumps({'autoupdate':False,'share':'disabled','snapshot':False,'plugin':[],
                                                'mcp':{},'lsp':False,'formatter':False,'permission':{'*':'deny'}})}
    for flag in ('AUTOUPDATE','MODELS_FETCH','DEFAULT_PLUGINS','PROJECT_CONFIG','EXTERNAL_SKILLS','CLAUDE_CODE','LSP_DOWNLOAD','TERMINAL_TITLE'):
        env['OPENCODE_DISABLE_'+flag]='true'
    env['OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER']='true'
    (home/'models.json').write_text('{}')
    if VERSION=='1.1.51':
        from legacy_adapter import prepare
        prepare(home,env)
    return env


def stop(process):
    try:os.killpg(process.pid,signal.SIGKILL)
    except ProcessLookupError:pass
    try:process.wait(timeout=2)
    except subprocess.TimeoutExpired:pass


def inspect_process(process):
    result=[]
    for task in Path(f'/proc/{process.pid}/task').glob('*'):
        row={'线程':task.name}
        for name in ('comm','wchan','syscall','stat'):
            try:row[name]=(task/name).read_text()[:2048]
            except OSError as exc:row[name]=str(exc)
        result.append(row)
    return result


def launch(args, **kwargs):
    # 在独立子进程内设置限额；避免 Gunicorn 线程中使用 preexec_fn 死锁。
    wrapper = "import os,resource,sys; resource.setrlimit(resource.RLIMIT_FSIZE,(2097152,2097152)); os.execve(sys.argv[1],sys.argv[1:],os.environ)"
    return subprocess.Popen([sys.executable, '-c', wrapper, *args], start_new_session=True, **kwargs)


def probe():
    result={'ready':False,'version':VERSION,'mode':'legacy' if VERSION=='1.1.51' else 'standard','checks':[]}
    digest=hashlib.sha256()
    with open('/opt/review/opencode','rb') as binary:
        for block in iter(lambda:binary.read(1024*1024),b''):digest.update(block)
    result['binary_sha256']=digest.hexdigest()
    result['cpu']=[line for line in Path('/proc/cpuinfo').read_text().splitlines() if line.startswith(('model name','flags'))][:2]
    with tempfile.TemporaryDirectory(prefix='runtime-probe-',dir='/tmp') as directory:
        home=Path(directory);env=environment(home)
        start=time.monotonic()
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            process=launch(['/opt/review/opencode','--version'],env=env,cwd=home,
                                     stdout=out,stderr=err)
            try:process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                result['process']=inspect_process(process);result['reason']='版本命令超过 30 秒';stop(process)
            out.seek(0);err.seek(0);stdout=out.read(65536).decode(errors='replace');stderr=err.read(65536).decode(errors='replace')
            result['checks'].append({'步骤':'版本命令','退出码':process.returncode,'退出信号':-process.returncode if process.returncode and process.returncode<0 else None,'耗时秒':round(time.monotonic()-start,2),'stdout':stdout,'stderr':stderr})
            if process.returncode!=0 or stdout.strip()!=VERSION:
                result.setdefault('reason','版本命令失败或版本不符');return result
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        with tempfile.TemporaryFile() as log:
            process=launch(['/opt/review/opencode','serve','--hostname','127.0.0.1','--port',str(port)],
                                     env=env,cwd=home,stdout=log,stderr=log)
            session=requests.Session();session.trust_env=False;start=time.monotonic();last='尚未响应'
            try:
                deadline = start + 30
                healthy = False
                while time.monotonic() < deadline and process.poll() is None:
                    remaining = deadline - time.monotonic()
                    try:
                        response = session.get(f'http://127.0.0.1:{port}/global/health', timeout=(min(.2, remaining), min(.5, remaining)))
                        if response.status_code == 200 and response.json().get('version') == VERSION:
                            healthy = True
                            break
                        last = '健康接口 HTTP ' + str(response.status_code)
                    except (requests.RequestException, ValueError) as exc:
                        last = type(exc).__name__ + ': ' + str(exc)
                    time.sleep(min(.2, max(0, deadline-time.monotonic())))
                result['checks'].append({'步骤':'服务健康','通过':healthy,'耗时秒':round(time.monotonic()-start,2),'结果':'HTTP 200，版本匹配' if healthy else last})
                remaining = deadline-time.monotonic()
                if healthy and remaining > 0:
                    session_start = time.monotonic()
                    try:
                        # 创建会话有副作用；在剩余总预算内只提交一次，避免短超时重复初始化。
                        response = session.post(f'http://127.0.0.1:{port}/session', json={'title':'安装自检'}, timeout=(min(.2,remaining/2),max(.001,remaining-min(.2,remaining/2))))
                        last = '会话接口 HTTP ' + str(response.status_code)
                        if response.status_code == 200 and response.json().get('id') and time.monotonic() <= deadline:
                            result['ready'] = True
                            result['reason'] = '版本、服务及会话接口通过'
                    except (requests.RequestException, ValueError) as exc:
                        last = type(exc).__name__ + ': ' + str(exc)
                    result['checks'].append({'步骤':'创建会话','尝试次数':1,'通过':result['ready'],'耗时秒':round(time.monotonic()-session_start,2),'结果':last})
                if not result['ready']:
                    result['reason']='服务与会话检查未通过：'+last;result['process']=inspect_process(process)
            finally:
                code=process.poll();stop(process);session.close();log.seek(0)
                result['checks'].append({'步骤':'服务与会话','退出码':code,'耗时秒':round(time.monotonic()-start,2),'日志':log.read(65536).decode(errors='replace')})
    return result


if __name__=='__main__':
    value=probe();print(json.dumps(value,ensure_ascii=False),flush=True)
    raise SystemExit(0 if value['ready'] else 10)

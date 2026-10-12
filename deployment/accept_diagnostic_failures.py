"""隔离主机自动诊断故障注入；仅创建带独立标签的短期容器。"""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
import uuid


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--package',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    sys.path.insert(0,str(args.package/'deployment'));from diagnostics import Report
    manifest=json.loads((args.package/'installer-manifest.json').read_text());image=manifest['images']['backend']['tag']
    project='diagnostic-fault-'+uuid.uuid4().hex[:8];results=[]
    def case(name,action):
        report=Report(args.output,name)
        try:
            with report.stage(name):action(report)
            raise RuntimeError('注入未触发预期错误')
        except Exception as exc:report.fail(exc)
        report.collect(project);archive=report.finish('故障已记录')
        results.append({'场景':name,'错误':report.error,'诊断包':str(archive)})
    case('磁盘不足',lambda r:r.command([sys.executable,'-c',"open('/dev/full','wb',buffering=0).write(b'x')"],label='写入 /dev/full（ENOSPC 受控注入）',timeout=3))
    with socket.socket() as sock:
        sock.bind(('0.0.0.0',0));sock.listen();port=sock.getsockname()[1]
        case('端口占用',lambda r:r.command([sys.executable,'-c',f"import socket; s=socket.socket(); s.bind(('0.0.0.0',{port}))"],label='验证服务端口 '+str(port),timeout=3))
    case('镜像解包失败（受控错误注入）',lambda r:r.command([sys.executable,'-c',"import sys; print('apply layer error: failed to unmount /var/lib/containerd/tmpmounts/containerd-mount-test: device or resource busy',file=sys.stderr); sys.exit(1)"],label='创建数据库容器',timeout=3))
    case('启动挂死',lambda r:r.command([sys.executable,'-c','import time; time.sleep(60)'],label='等待服务启动',timeout=1))
    name=project+'-oom'
    def oom(report):
        code,out,err=report.command(['docker','run','--name',name,'--label','com.docker.compose.project='+project,'--network','none','--memory','64m','--memory-swap','64m','--entrypoint','python',image,'-c','x=bytearray(256*1024*1024)'],label='启动受限内存容器',timeout=20,check=False)
        state=json.loads(subprocess.check_output(['docker','inspect','--format','{{json .State}}',name]))
        report.write('oom.json',state)
        if state.get('OOMKilled'):raise RuntimeError('容器被 OOM 杀死；退出码 '+str(state['ExitCode']))
        raise RuntimeError('OOM 注入未达到预期状态：'+str(code))
    try:case('容器 OOM',oom)
    finally:subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    case('SSH 私钥缺失',lambda r:r.command(['docker','run','--rm','--network','none','--entrypoint','python',image,'-c',"from services.ssh_service import SSHService; SSHService('127.0.0.1',key_path='/missing/key')._get_client()"],label='读取管理私钥',timeout=10))
    case('备份执行失败',lambda r:r.command(['docker','exec',project+'-missing','gitea','dump'],label='执行实例备份',timeout=3))
    (args.output/'fault-results.json').write_text(json.dumps({'说明':'真实子进程、端口、磁盘和 OOM；镜像解包明确采用受控错误注入。SSH 认证及指纹另由双机 API 验收。','结果':results},ensure_ascii=False,indent=2)+'\n')
    print('故障诊断采集完成，交由独立审查者仅凭诊断包判断。',flush=True)

if __name__=='__main__':main()

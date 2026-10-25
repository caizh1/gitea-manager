"""在独立目录用原安装入口复验交付包；不接触已有部署。"""
import argparse
import ipaddress
import json
from pathlib import Path
import socket
import subprocess
import tarfile
import time

root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--package',type=Path,required=True)
parser.add_argument('--directory',type=Path,required=True)
args=parser.parse_args()
directory=args.directory.resolve()
directory.mkdir(parents=True,exist_ok=False)
with tarfile.open(args.package) as archive:
    require_safe=all((directory/m.name).resolve().is_relative_to(directory) and (m.isfile() or m.isdir()) for m in archive)
    if not require_safe:raise RuntimeError('安装包包含非预期路径')
    archive.extractall(directory)
with socket.socket() as probe:
    probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
process=subprocess.Popen(['bash','install.sh','--desktop-test','--host','127.0.0.1','--port',str(port)],cwd=directory)
prepared=False
try:
    while process.poll() is None:
        state_path=directory/'state/installation.json'
        if state_path.exists() and not prepared:
            state=json.loads(state_path.read_text())
            if state['directory']!=str(directory) or state['product']!='manager':raise RuntimeError('隔离安装身份不符')
            # 当前测试机默认地址池已用尽；只为这个新安装预备两个显式隔离子网。
            networks=json.loads(subprocess.check_output(['docker','network','inspect',*subprocess.check_output(['docker','network','ls','-q'],text=True).split()]))
            used=[ipaddress.ip_network(c['Subnet']) for n in networks for c in (n.get('IPAM',{}).get('Config') or []) if c.get('Subnet')]
            for name in ('default','review-execution'):
                subnet=next(net for net in ipaddress.ip_network('10.249.0.0/16').subnets(new_prefix=24) if not any(net.version==u.version and net.overlaps(u) for u in used))
                command=['docker','network','create','--subnet',str(subnet),
                    '--label','com.docker.compose.project='+state['project'],'--label','com.docker.compose.network='+name,
                    '--label','io.gitea-manager.test=svn-install']
                if name=='review-execution':command.append('--internal')
                subprocess.run([*command,state['project']+'_'+name],check=True,stdout=subprocess.DEVNULL)
                used.append(subnet)
            prepared=True
        time.sleep(.3)
    state=json.loads((directory/'state/installation.json').read_text())
    report={'状态':'通过' if process.returncode==0 and state.get('complete') else '失败',
            '退出码':process.returncode,'项目':state['project'],'地址':state['url'],
            '边界':'macOS Docker Desktop 的 Linux amd64 镜像及原安装入口验收；默认地址池耗尽时仅预备本测试专用子网，不代表 Ubuntu systemd 真机验收。'}
    (directory.parent/'installation-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    if report['状态']!='通过':raise RuntimeError('原安装入口验收未通过，保留现场')
finally:
    if process.poll() is None:process.terminate();process.wait()

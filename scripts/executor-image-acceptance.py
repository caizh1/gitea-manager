"""以最终镜像验证升级探针及执行器默认启动，不挂载源码或业务凭据。"""
import json
import subprocess
import sys
import time
from pathlib import Path

image, evidence = sys.argv[1:]
limits = ['--platform', 'linux/amd64', '--network', 'none', '--read-only',
          '--tmpfs', '/tmp:size=384m,mode=1777', '--memory', '2g', '--pids-limit', '128',
          '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true']
probe = subprocess.run(['docker', 'run', '--rm', *limits, image, 'python',
                        '/opt/review/runtime_probe.py'], capture_output=True, text=True)
report = {'说明': '真实交付镜像验收，未挂载源码与业务数据', '镜像': image,
          '探针退出码': probe.returncode, '探针标准错误': probe.stderr[-4096:]}
Path(evidence).write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
assert probe.returncode == 0, probe.stderr
report['探针'] = json.loads(probe.stdout)
assert report['探针']['ready'] is True
container = subprocess.check_output(['docker', 'run', '-d', *limits,
    '--label', 'io.gitea-manager.executor-image-test=true', image], text=True).strip()
try:
    deadline = time.monotonic() + 75
    while True:
        check = subprocess.run(['docker', 'exec', container, 'python', '-c',
            "import requests; r=requests.get('http://127.0.0.1:5052/health',timeout=2); print(r.text); r.raise_for_status()"],
            capture_output=True, text=True)
        if check.returncode == 0:
            report['默认启动健康'] = json.loads(check.stdout)
            assert report['默认启动健康']['ready'] is True
            break
        if time.monotonic() >= deadline:
            raise RuntimeError('默认执行器未健康启动：'+check.stderr[-1000:])
        time.sleep(1)
    report['结果'] = '通过'
finally:
    report['容器日志'] = subprocess.check_output(['docker', 'logs', container], stderr=subprocess.STDOUT, text=True)[-12000:]
    subprocess.run(['docker', 'rm', '-f', container], check=True, stdout=subprocess.DEVNULL)
    Path(evidence).write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
print('执行器镜像探针、默认启动及健康接口全部通过')

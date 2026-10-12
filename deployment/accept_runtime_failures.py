"""仅在隔离 Ubuntu 验收机注入真实 SIGILL 与挂死；不连接模型或生产实例。"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--package',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--core-only',action='store_true');args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True,mode=0o700)
    sys.path.insert(0,str(args.package/'deployment'))
    spec=importlib.util.spec_from_file_location('fault_installer',args.package/'deployment/install.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    manifest=json.loads((args.package/'installer-manifest.json').read_text())
    reports=[]
    with tempfile.TemporaryDirectory() as directory:
        context=Path(directory)
        for kind,command in ([] if args.core_only else [('illegal','echo "cpu lacks avx support; Bun has crashed" >&2\nkill -ILL $$'),('hang','sleep 600')]):
            (context/'opencode').write_text('#!/bin/sh\n'+command+'\n');(context/'opencode').chmod(0o555)
            (context/'Dockerfile').write_text('FROM '+manifest['images']['executor']['tag']+'\nUSER 0:0\nCOPY opencode /opt/review/opencode\nUSER 10001:10001\n')
            subprocess.run(['docker','build','--network','none','-t','manager-fault:'+kind,str(context)],check=True)
        for kind in (() if args.core_only else ('illegal','hang','both')):
            root=args.output/kind;root.mkdir();(root/'state').mkdir()
            selected=json.loads(json.dumps(manifest));selected['images']['executor']['tag']='manager-fault:'+('illegal' if kind=='both' else kind)
            if kind=='both':selected['images']['executor_legacy']['tag']='manager-fault:illegal'
            installer=module.Installer(root,selected)
            installer.initialize_state(argparse.Namespace(host='127.0.0.1',port=35480,ssh_port=None))
            module.REPORT=module.Report(root/'state/diagnostics','隔离运行时故障注入：'+kind)
            with module.REPORT.stage('检测执行器并自动回退'):installer.select_runtime()
            expected=kind!='both'
            if installer.state['runtime']['ready']!=expected:raise RuntimeError('运行时故障结果不符：'+kind)
            if expected and installer.state['executor_key']!='executor_legacy':raise RuntimeError('未选择兼容执行器')
            archive=module.REPORT.finish('兼容回退通过' if expected else '两版失败并停用 AI')
            reports.append({'注入':kind,'实际结果':installer.state['runtime'],'诊断包':str(archive)})
        # 单独验证两版都失败时真正完成核心安装；复制包且保持原包不变。
        degraded=args.output/'core-degraded'
        shutil.copytree(args.package,degraded,ignore=shutil.ignore_patterns('state','compose.install.json'),copy_function=os.link)
        changed=json.loads(json.dumps(manifest))
        info=json.loads(subprocess.check_output(['docker','image','inspect','manager-fault:illegal']))[0]
        for key in ('executor','executor_legacy'):changed['images'][key]={'tag':'manager-fault:illegal','ids':[info['Id']]}
        path=degraded/'installer-manifest.json';path.unlink();path.write_text(json.dumps(changed,ensure_ascii=False,indent=2)+'\n')
        checks=degraded/'SHA256SUMS';lines=checks.read_text().splitlines();checks.unlink()
        checks.write_text('\n'.join(hashlib.sha256(path.read_bytes()).hexdigest()+'  installer-manifest.json' if row.endswith('  installer-manifest.json') else row for row in lines)+'\n')
        result=subprocess.run(['bash','install.sh','--host','10.0.2.15','--port','5487'],cwd=degraded,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=600)
        (args.output/'core-degraded.log').write_text(result.stdout)
        if result.returncode:raise RuntimeError('AI 降级后的核心安装失败，请看 core-degraded.log')
        state=json.loads((degraded/'state/installation.json').read_text())
        if state['runtime']['ready']:raise RuntimeError('两版失败后错误报告 AI 可用')
        (args.output/'results.json').write_text(json.dumps({'说明':'真实隔离故障注入，不代表真实模型质量','运行时检查':reports,'核心降级安装':'通过'},ensure_ascii=False,indent=2)+'\n')
        print('真实 SIGILL、启动挂死回退与核心降级安装均通过',flush=True)

if __name__=='__main__':main()

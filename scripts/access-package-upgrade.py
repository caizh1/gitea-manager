#!/usr/bin/env python3
"""在最终包新建的隔离实例中验证升级与迁移后故障回退，核对 LDAP 密文和独立密钥。"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]


def run(command, **kwargs):
    result = subprocess.run(command, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise RuntimeError('隔离命令失败：'+str(command[0])+'，退出码 '+str(result.returncode))
    return result.stdout


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installation',required=True,type=Path)
    parser.add_argument('--package-dir',required=True,type=Path)
    args=parser.parse_args()
    installation=args.installation.resolve(); package=args.package_dir.resolve()
    state=json.loads((installation/'state/installation.json').read_text())
    if not installation.is_relative_to(ROOT/'work') or not state['project'].startswith('gitea-manager-'):
        raise RuntimeError('只能使用工作目录中本次新建的隔离安装实例')
    source_report=installation.parent/'report.json'
    if not source_report.is_file() or json.loads(source_report.read_text()).get('状态')!='全部通过':
        raise RuntimeError('首次最终包安装与目录验收未通过，禁止继续')
    folder=installation.parent/('upgrade-'+time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(3)); folder.mkdir(mode=0o700)
    project=state['project']; docker=shutil.which('docker')
    spec=importlib.util.spec_from_file_location('package_fixture',Path(__file__).with_name('access-package-acceptance.py'))
    fixture_module=importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture_module)
    subnet=fixture_module.free_subnets()[0]
    run([docker,'network','create','--internal','--subnet',subnet,
         '--label','com.docker.compose.project='+project,
         '--label','com.docker.compose.network=gm-review-execution',project+'_gm-review-execution'])
    report={'说明':'仅在最终安装包新建的隔离实例执行；故障注入发生在新版迁移及全部健康验证后。','结果':[]}
    def save():
        (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    def services():
        ids=run([docker,'ps','-q','--filter','label=com.docker.compose.project='+project]).split()
        return {v['Config']['Labels']['com.docker.compose.service']:v for v in json.loads(run([docker,'inspect',*ids]))}
    fixture=folder/'fixture-compose.json'
    fixture.write_text(json.dumps({'services':{'backend':{'labels':{'io.gitea-manager.upgrade-test':'true'}}}}))
    run([docker,'compose','--project-directory',str(installation),'-p',project,'-f',str(installation/'compose.install.json'),
         '-f',str(fixture),'up','-d','--no-build','--pull','never','backend'])
    code="""from pathlib import Path
import sqlite3,hashlib,json,os
from cryptography.fernet import Fernet
paths=[Path('/app/data/access.key'),Path('/app/data/.session-key'),Path(os.environ['AI_REVIEW_MASTER_KEY_FILE']),Path(os.environ['AI_REVIEW_AUTHORIZATION_TOKEN_FILE'])]
c=sqlite3.connect('/app/data/gitea-manager.db'); assert c.execute('pragma integrity_check').fetchone()[0]=='ok'
directory=c.execute('select id,name,enabled,config_json,password_ciphertext,version from access_directory_sources order by id').fetchall()
assert directory and all(Fernet(paths[0].read_bytes()).decrypt(row[4].encode()) for row in directory)
data={table:c.execute('select * from '+table+' order by 1').fetchall() for table in ['access_accounts','access_directory_sources','access_organization_bindings','access_organization_grants']}
review=sqlite3.connect(os.environ['AI_REVIEW_DB']); assert review.execute('pragma integrity_check').fetchone()[0]=='ok'
print(json.dumps({'密钥':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.exists()},'权限数据':hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest(),'两库完整性':'ok'}))
"""
    def snapshot():
        active=services(); backend=active['backend']
        deadline=time.monotonic()+60
        while True:
            value=subprocess.run([docker,'exec',backend['Id'],'python','-c',code],capture_output=True,text=True)
            if value.returncode==0:break
            if time.monotonic()>deadline:raise RuntimeError('完整性及原密钥核对失败')
            time.sleep(.5)
        result=json.loads(value.stdout)
        fixture_module.Client(state['url'],installation/'state/tls/cert.pem')
        result['实际HTTPS会话入口']='证书及主机名验证通过'
        result['TLS挂载']=[m for m in active['frontend']['Mounts'] if m['Destination'].startswith('/etc/nginx/tls/')]
        result['HTTPS端口']=active['frontend']['HostConfig']['PortBindings'].get('443/tcp')
        result['数据卷']=next(m['Name'] for m in backend['Mounts'] if m['Destination']=='/app/data')
        return result
    try:
        before=snapshot()
        for fault in (True,False):
            env=os.environ.copy()
            if fault:
                shim=folder/'fault-injection';shim.mkdir()
                wrapper=shim/'docker'
                wrapper.write_text('#!/usr/bin/env python3\n# 完成真实验证后注入中断，保留原始回退路径。\nimport os,sys,subprocess\na=sys.argv[1:]\nif a[-1:]==["verify"] and any(v.endswith("upgrade_runtime.py") for v in a):\n r=subprocess.run(['+repr(docker)+',*a]);sys.exit(42 if r.returncode==0 else r.returncode)\nos.execv('+repr(docker)+',["docker",*a])\n')
                wrapper.chmod(0o700); env['PATH']=str(shim)+os.pathsep+env.get('PATH','')
            name='迁移完成后中断回退' if fault else '完整一键升级'
            backend=services()['backend']['Name'].lstrip('/')
            started=time.monotonic()
            completed=subprocess.run(['bash',str(package/'upgrade.sh'),'--container',backend,'--desktop-test'],
                env=env,capture_output=True,text=True,timeout=1200)
            output=(completed.stdout+'\n'+completed.stderr).replace(state['admin_password'],'[隔离口令已隐藏]')
            log=folder/('rollback.log' if fault else 'upgrade.log'); log.write_text(output)
            row={'场景':name,'退出码':completed.returncode,'耗时秒':round(time.monotonic()-started,2),'原始日志':log.name}
            report['结果'].append(row); save()
            assert (completed.returncode!=0)==fault,'升级结果不符合故障注入预期'
            if fault:
                assert '全部服务、独立审批展示、模型执行器和历史数据核对通过' in output
                assert '原服务恢复并通过健康检查' in output
            assert snapshot()==before,'升级或回退后数据、密钥、TLS或原卷发生变化'
            active=services()
            assert all('/run/secrets/access_authorization' not in [m['Destination'] for m in active[name]['Mounts']] for name in ['review-executor'])
            for service in ['backend','review-worker','review-progress','review-feedback']:
                assert '/run/secrets/access_authorization' in [m['Destination'] for m in active[service]['Mounts']]
            row.update(结果='通过',权限数据与独立密钥保持=True,TLS入口与原卷保持=True); save()
            print(name+'：通过',flush=True)
        report['状态']='全部通过'
    except Exception as exc:
        report.update(状态='失败，保留首次现场',首次失败=str(exc)); raise
    finally:
        save(); print('最终包升级证据：'+str(folder/'report.json'),flush=True)


if __name__=='__main__':
    main()

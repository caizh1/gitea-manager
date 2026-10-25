#!/usr/bin/env python3
"""以真实 Git 推送和原生合并验证未来分支、重叠规则、强推和标签保护。"""
import argparse,base64,json,os,re,secrets,subprocess,time
from pathlib import Path
import requests
import importlib.util

spec=importlib.util.spec_from_file_location('advanced',Path(__file__).with_name('git-migration-advanced-acceptance.py'));advanced=importlib.util.module_from_spec(spec);spec.loader.exec_module(advanced)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('manager-state','target-state','fixture'):p.add_argument('--'+key,type=Path,required=True)
    p.add_argument('--run-id',type=int,required=True);a=p.parse_args();manager=json.loads(a.manager_state.read_text());target=json.loads(a.target_state.read_text());fixture=json.loads(a.fixture.read_text())
    assert json.loads(advanced.docker('inspect',target['name']))[0]['Config']['Labels']['io.gitea-manager.test']=='git-migration-receiver'
    run=advanced.read_run(manager['name'],a.run_id);assert run['status']=='succeeded';checkpoint=json.loads(run['checkpoint_json'])[str(fixture['project']['id'])]
    path=checkpoint['final_path'];base=target['base'];remote=base+'/'+path+'.git';client=requests.Session();client.trust_env=False;client.auth=('migration-admin',target['password'])
    def api(method,path,data=None,expected=(200,201,204)):
        r=client.request(method,base+'/api/v1'+path,json=data,timeout=60);assert r.status_code in expected, '规则验收 API 失败 '+path+' HTTP '+str(r.status_code)
        return r.json() if r.content else None
    key=secrets.token_hex(4);folder=a.manager_state.parent/('rule-'+str(a.run_id)+'-'+key);folder.mkdir()
    def env(user,password):
        e=dict(os.environ);e.update({'GIT_TERMINAL_PROMPT':'0','GIT_CONFIG_COUNT':'4','GIT_CONFIG_KEY_0':'http.'+remote+'.extraHeader','GIT_CONFIG_VALUE_0':'Authorization: Basic '+base64.b64encode((user+':'+password).encode()).decode(),'GIT_CONFIG_KEY_1':'credential.helper','GIT_CONFIG_VALUE_1':'','GIT_CONFIG_KEY_2':'http.followRedirects','GIT_CONFIG_VALUE_2':'false','GIT_CONFIG_KEY_3':'http.proxy','GIT_CONFIG_VALUE_3':''});return e
    admin=env('migration-admin',target['password']);rows=[]
    def git(*args,identity=admin,expected=True,label=None):
        r=subprocess.run(['git',*args],cwd=folder,env=identity,capture_output=True)
        if label:
            assert (r.returncode==0)==expected, 'Git 保护行为与来源不一致：'+label+'；'+r.stderr.decode(errors='replace')[-300:]
            if not expected:assert re.search(r'protected|protection|permission|not allowed|禁止|不允许|保护|无权',r.stderr.decode(errors='replace'),re.I), '拒绝没有明确的保护或权限原因：'+label
            rows.append({'场景':label,'预期':'允许' if expected else '拒绝','实际':'允许' if r.returncode==0 else '拒绝'})
        elif r.returncode:raise RuntimeError('隔离 Git 夹具初始化失败')
        return r.stdout.decode().strip()
    git('clone',remote,'.');git('config','user.name','迁移规则验收');git('config','user.email','rules@example.invalid')
    main_commit=git('rev-parse','origin/main');(folder/'rule-evidence.txt').write_text('真实保护规则验证\n');git('add','rule-evidence.txt');git('commit','-m','验证迁移保护规则');new_commit=git('rev-parse','HEAD')
    open_branch='release/open-'+key;closed_branch='private/future-'+key;release_branch='release/restricted-'+key
    git('push','origin',new_commit+':refs/heads/'+open_branch,label='未来重叠分支允许正常推送')
    git('push','origin',new_commit+':refs/heads/'+closed_branch,expected=False,label='Nobody 推送规则拒绝未来分支')
    git('push','origin',new_commit+':refs/heads/'+release_branch,expected=fixture['kind']=='gitlab',label='单一保护模式按来源授权')
    git('push','--force','origin',main_commit+':refs/heads/'+open_branch,expected=fixture['kind']=='gitlab',label='重叠模式按来源判断强制推送')
    tag='v-migration-'+key
    git('push','origin',new_commit+':refs/tags/'+tag,label='已映射创建者可创建受保护标签')
    git('push','--force','origin',main_commit+':refs/tags/'+tag,expected=fixture['kind']=='gitea',label='GitLab 受保护标签禁止 Git 改写')
    git('push','origin',':refs/tags/'+tag,expected=fixture['kind']=='gitea',label='GitLab 受保护标签禁止 Git 删除')
    writer='migration-writer-'+key;password=secrets.token_urlsafe(24)
    api('POST','/admin/users',{'username':writer,'password':password,'email':writer+'@example.invalid','must_change_password':False})
    api('PUT','/repos/'+path+'/collaborators/'+writer,{'permission':'write'});identity=env(writer,password)
    git('push','origin',new_commit+':refs/heads/release/open-writer-'+key,identity=identity,expected=fixture['kind']=='gitea',label='未映射普通写入者按来源规则判定')
    git('push','origin',new_commit+':refs/tags/v-writer-'+key,identity=identity,expected=False,label='未映射写入者不能创建受保护标签')
    if fixture['kind']=='gitlab':
        r=requests.delete(base+'/api/v1/repos/'+path+'/tags/'+tag,auth=(writer,password),timeout=30)
        assert r.status_code in (403,404,422), '普通写入者不能通过 API 删除 GitLab 保护标签'
        rows.append({'场景':'普通写入者通过 API 删除受保护标签','预期':'拒绝','实际':'拒绝'})
        api('DELETE','/repos/'+path+'/tags/'+tag);rows.append({'场景':'维护者通过原生 API 删除受保护标签','预期':'允许','实际':'允许'})
    # 私有历史文件不能通过猜测编号绕过仓库读取授权。
    outsider=requests.Session();outsider.trust_env=False
    if checkpoint.get('assets'):
        asset=next(iter(checkpoint['assets'].values()));r=outsider.get(base+'/'+path+'/migration-history/assets/'+str(asset['id']),allow_redirects=False,timeout=30)
        assert r.status_code in (302,303,404);rows.append({'场景':'未登录读取私有历史文件','预期':'拒绝','实际':'拒绝'})
    cases=a.fixture.parent/(a.fixture.stem+'-rule-cases.json')
    if cases.exists():
        case=json.loads(cases.read_text());number=case['请求编号']
        pr=api('GET','/repos/'+path+'/pulls/'+str(number))
        assert pr['base']['ref']==case['分支'] and not pr['merged']
        r=client.post(base+'/api/v1/repos/'+path+'/pulls/'+str(number)+'/merge',json={'Do':'merge'},timeout=60)
        assert r.status_code==405 and 'not allowed to merge' in r.json().get('message','')
        assert not api('GET','/repos/'+path+'/pulls/'+str(number))['merged']
        rows.append({'场景':'现存分支的 Nobody 合并规则阻止管理员合并','预期':'拒绝','实际':'拒绝'})
    output=a.manager_state.parent/('rule-acceptance-'+str(a.run_id)+'.json');output.write_text(json.dumps({'说明':'真实原生 Git/API 行为验收，规则不按现有分支枚举判断','来源':fixture['kind'],'场景':rows,'结果':'通过'},ensure_ascii=False,indent=2)+'\n');print('真实保护行为验收通过：'+str(output))

if __name__=='__main__':main()

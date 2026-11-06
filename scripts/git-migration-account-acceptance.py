#!/usr/bin/env python3
"""在真实隔离 Gitea 中验收整批人员匹配；仅操作带测试标记的实例。"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import secrets
import subprocess
import threading
import time

import requests
from git_migration_acceptance_helpers import create_verified_run

ROOT = Path(__file__).resolve().parents[1]
LOCK = threading.Lock()


def docker(*args):
    result = subprocess.run(['docker', *args], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('隔离 Docker 操作失败：' + args[0])
    return result.stdout.strip()


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)


def api(base, method, path, *, token=None, auth=None, body=None):
    response = requests.request(method, base + '/api/v1' + path, headers={'Authorization': 'token ' + token} if token else {}, auth=auth, json=body, timeout=60)
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f'隔离平台接口失败：{path} HTTP {response.status_code}')
    return response.json() if response.content else None


def wait(read, description, seconds=600):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            result = read()
        except requests.RequestException:
            result = None
        if result:
            return result
        time.sleep(.3)
    raise RuntimeError('等待超时：' + description)


def source_fixture(source_path, fixture_path, automatic):
    source = json.loads(source_path.read_text())
    info = json.loads(docker('inspect', source['name']))[0]
    assert info['Config']['Labels']['io.gitea-manager.test'] == 'git-migration-source'
    state = json.loads(fixture_path.read_text()) if fixture_path.exists() else {'说明':'本次人员匹配隔离夹具，不得发布凭据','prefix':'mapping-' + secrets.token_hex(4),'users':{},'automatic':automatic}
    prefix = state['prefix']; credentials=('source-admin',source['password'])
    repo='/repos/source-admin/source-project'
    save(fixture_path,state)
    def create(index):
        name=f'{prefix}-{index:03}'
        group='automatic' if index<=automatic else 'ambiguous' if index<=automatic+4 else 'missing'
        display=f'匹配验收人员 {index:03}'
        existing = requests.get(source['base'] + '/api/v1/users/'+name, auth=credentials, timeout=30)
        user=existing.json() if existing.status_code==200 else api(source['base'],'POST','/admin/users',auth=credentials,body={'username':name,'password':secrets.token_urlsafe(20),'email':name+'@example.test','full_name':display,'must_change_password':False})
        api(source['base'],'PUT',repo+'/collaborators/'+name,auth=credentials,body={'permission':'write'})
        with LOCK:
            old=state['users'].get(str(index),{})
        if not old.get('issue'):
            issue=api(source['base'],'POST',repo+'/issues',auth=credentials,body={'title':display,'body':'固定人员映射验收样本','assignees':[name]})
        else: issue={'number':old['issue']}
        with LOCK:
            state['users'][str(index)]={'id':user['id'],'login':name,'name':display,'email':name+'@example.test','group':group,'issue':issue['number']}
            save(fixture_path,state)
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(create,range(1,automatic+9)))
    # 旧版空 Wiki 返回 404，需提供可核验的实际页面，不能当作读取成功。
    wiki=requests.get(source['base']+'/api/v1'+repo+'/wiki/page/Home',auth=credentials,timeout=30)
    if wiki.status_code==404:
        import base64
        api(source['base'],'POST',repo+'/wiki/new',auth=credentials,body={'title':'Home',
            'content_base64':base64.b64encode('人员匹配迁移验收 Wiki'.encode()).decode(),'message':'初始化验收 Wiki'})
    else:
        wiki.raise_for_status()
    print('真实来源人员与议题已准备：'+str(len(state['users'])),flush=True)


def full(source_path,target_path,fixture_path,tag,output_root=None):
    source=json.loads(source_path.read_text());target=json.loads(target_path.read_text());fixture=json.loads(fixture_path.read_text())
    for obj,expected in ((source,'git-migration-source'),(target,'git-migration-receiver')):
        assert json.loads(docker('inspect',obj['name']))[0]['Config']['Labels']['io.gitea-manager.test']==expected
    prefix=fixture.get('target_group_path', fixture['prefix']); report={'说明':'真实隔离 Gitea 到增强版人员匹配及执行验收；不代表 GitLab 全版本或生产验收','断言':[]}
    def check(condition,name):
        if not condition: raise AssertionError(name)
        report['断言'].append(name)
    groups=api(target['base'],'GET','/governance/groups?parent_id=0',token=target['token'])
    group=next((g for g in groups if (g.get('path') or g.get('full_path'))==prefix),None)
    if group is None:
        group=api(target['base'],'POST','/governance/groups',token=target['token'],body={'parent_id':0,'path':prefix,'name':'人员匹配隔离验收','visibility':2})
    users=[]
    for item in sorted(fixture['users'].values(),key=lambda u:u['id']):
        if item['group']=='missing':continue
        variants=[0] if item['group']=='automatic' else [1,2]
        for variant in variants:
            login=item['login'] if variant==0 and item['id']%2 else 'target-'+item['login']
            if variant:login+=f'-{variant}'
            email=item['email'] if variant==0 else login+'@example.test'
            existing=requests.get(target['base']+'/api/v1/users/'+login,headers={'Authorization':'token '+target['token']},timeout=30)
            user=existing.json() if existing.status_code==200 else api(target['base'],'POST','/admin/users',token=target['token'],body={'username':login,'password':secrets.token_urlsafe(20),'email':email,'full_name':item['name'],'must_change_password':False})
            assert user['email']==email and user['full_name']==item['name']
            current=api(target['base'],'GET','/governance/groups/'+str(group['id']),token=target['token'])
            api(target['base'],'PUT',f"/governance/groups/{group['id']}/members/{user['id']}",token=target['token'],body={'role':30,'revision':current['revision']})
            users.append(user)
    name='gm-account-mapping-'+time.strftime('%Y%m%d%H%M%S')
    folder=(output_root or ROOT/'work/account-mapping-20261029')/name;folder.mkdir(mode=0o700)
    label='io.gitea-manager.test=git-migration-mapping'
    docker('network','create','--label',label,name);docker('volume','create','--label',label,name)
    password=secrets.token_urlsafe(24)
    env=folder/'manager.env';env.write_text('DATABASE_URL=sqlite:////app/data/gitea-manager.db\nINIT_PASSWORD='+password+'\nSECRET_KEY='+secrets.token_urlsafe(32)+'\nDISABLE_BACKGROUND_TASKS=1\n');env.chmod(0o600)
    common=['--platform','linux/amd64','--label',label,'--network',name,'--add-host','host.docker.internal:host-gateway']
    docker('run','-d',*common,'--name',name+'-backend','--network-alias','backend','--env-file',str(env),'-v',name+':/app/data','gitea-review-backend:'+tag)
    docker('run','-d',*common,'--name',name+'-frontend','-p','127.0.0.1::80','gitea-review-frontend:'+tag)
    base='http://127.0.0.1:'+docker('port',name+'-frontend','80/tcp').rsplit(':',1)[1]
    session=requests.Session();session.trust_env=False
    def call(method,path,body=None,allow=None):
        response=session.request(method,base+'/api'+path,json=body,timeout=90)
        if allow is not None:return response
        if not 200<=response.status_code<300:raise RuntimeError('管理接口失败：'+path+' HTTP '+str(response.status_code)+' '+response.text[:300])
        return response.json()
    wait(lambda:session.get(base+'/api/session',timeout=10).status_code==200,'管理端启动')
    session.headers['X-CSRF-Token']=call('GET','/session')['csrf_token']
    login=call('POST','/login',{'password':password});session.headers['X-CSRF-Token']=login['csrf_token']
    call('POST','/settings',{'host_ip':'127.0.0.1'})
    docker('run','-d',*common,'--name',name+'-worker','--env-file',str(env),'-v',name+':/app/data','gitea-review-git:'+tag)
    wait(lambda:call('GET','/git-migrations/status')['worker']['online'],'执行器启动')
    registered=call('POST','/git-migrations/sources',{'name':'人员匹配真实来源','kind':'gitea','url':source['base'].replace('127.0.0.1','host.docker.internal'),'username':'source-admin','auth_method':'password','secret':source['password']})
    discovery=call('POST',f"/git-migrations/sources/{registered['id']}/discoveries",{'source_version':registered['version'],'request_key':secrets.token_hex(16)})
    found=wait(lambda:(lambda x:x if x['status'] not in ('queued','running') else None)(call('GET',f"/git-migrations/discoveries/{discovery['id']}")),'来源发现')
    check(found['status']=='succeeded','真实来源发现成功')
    project=next(p for p in found['result']['projects'] if p['path']=='source-admin/source-project')
    inventory=call('POST',f"/git-migrations/sources/{registered['id']}/inventories",{'discovery_id':found['id'],'project_ids':[project['id']],'request_key':secrets.token_hex(16),'capture_code':True})
    inventory=wait(lambda:(lambda x:x if x['status'] not in ('queued','running') else None)(call('GET',f"/git-migrations/inventories/{inventory['id']}")),'完整资料盘点')
    check(inventory['status']=='succeeded','真实资料盘点成功')
    server=call('POST','/servers',{'name':'人员匹配增强版','host':'127.0.0.1','ssh_user':'root','gitea_port':3000,'gitea_url':target['base'].replace('127.0.0.1','host.docker.internal'),'api_token':target['token']})
    mapping=call('POST','/git-migrations/account-mappings',{'inventory_id':inventory['id'],'server_id':server['id'],'group_id':group['id'],'request_key':secrets.token_hex(16)})
    endpoint=f"/git-migrations/account-mappings/{mapping['id']}"
    def state():return call('GET',endpoint)
    def completed():return wait(lambda:(lambda x:x if x['status'] not in ('queued','running') else None)(state()),'人员匹配后台操作')
    mapping=completed();check(mapping['status']=='succeeded','真实用户目录匹配成功')
    check(mapping['stats']['automatic']==fixture['automatic'],'自动匹配人数准确')
    check(mapping['stats']['ambiguous']==4 and mapping['stats']['unmatched']==4,'歧义与缺失均保留')
    check(mapping['stats']['confirmed']==0 and not mapping['ready'],'自动匹配不冒充人工确认')
    state_file=folder/'resources.json'
    save(state_file,{'说明':'本次隔离环境凭据，不得发布','name':name,'base':base,'password':password,'mapping_id':mapping['id'],'source_state':str(source_path),'target_state':str(target_path),'fixture':str(fixture_path),'group_id':group['id'],'server_id':server['id'],'inventory_id':inventory['id']})
    save(folder/'initial-report.json',report)
    print('真实人员匹配初始态已就绪：'+str(state_file),flush=True)
    return state_file


def verify(state_path):
    state=json.loads(state_path.read_text());folder=state_path.parent
    target=json.loads(Path(state['target_state']).read_text());fixture=json.loads(Path(state['fixture']).read_text())
    session=requests.Session();session.trust_env=False;base=state['base']
    def call(method,path,body=None,raw=False):
        r=session.request(method,base+'/api'+path,json=body,timeout=90)
        if raw:return r
        if not 200<=r.status_code<300:raise RuntimeError('验收接口失败：'+path+' HTTP '+str(r.status_code)+' '+r.text[:250])
        return r.json()
    session.headers['X-CSRF-Token']=call('GET','/session')['csrf_token']
    session.headers['X-CSRF-Token']=call('POST','/login',{'password':state['password']})['csrf_token']
    endpoint=f"/git-migrations/account-mappings/{state['mapping_id']}"
    def current():return call('GET',endpoint)
    def completed():return wait(lambda:(lambda x:x if x['status'] not in ('queued','running') else None)(current()),'确认后台完成')
    report=json.loads((folder/'initial-report.json').read_text())
    def check(value,name):
        if not value:raise AssertionError(name)
        report['断言'].append(name);save(folder/'account-acceptance.json',report)
    def change(action,**extra):return call('PATCH',endpoint,{'revision':current()['revision'],'action':action,**extra})
    def request_run():return {'inventory_id':state['inventory_id'],'server_id':state['server_id'],'group_id':state['group_id'],'mapping_id':state['mapping_id'],'request_key':secrets.token_hex(16),'source_frozen':True}
    check(call('POST','/git-migrations/runs',request_run(),raw=True).status_code==409,'未确认时服务端阻止执行')
    filtered=call('GET',endpoint+'?status=automatic&page=3&q=验收')
    check(filtered['filtered_total']==fixture['automatic'] and len(filtered['rows'])==50,'真实名单支持搜索与第三页')
    started=time.monotonic();change('confirm_automatic');result=completed()
    check(result['status']=='succeeded' and result['stats']['confirmed']==fixture['automatic'],'一次操作确认全部跨页自动匹配项')
    report['整批确认秒']=round(time.monotonic()-started,3)
    check(not result['ready'],'仍有歧义与缺失时不能继续')
    # 新建确实缺失的目标账号，刷新后只新增建议，保留已有确认。
    missing=next(u for u in fixture['users'].values() if u['group']=='missing')
    user=api(target['base'],'POST','/admin/users',token=target['token'],body={'username':missing['login'],'password':secrets.token_urlsafe(20),'email':missing['email'],'full_name':missing['name'],'must_change_password':False})
    group=api(target['base'],'GET',f"/governance/groups/{state['group_id']}",token=target['token'])
    api(target['base'],'PUT',f"/governance/groups/{state['group_id']}/members/{user['id']}",token=target['token'],body={'role':30,'revision':group['revision']})
    change('refresh');result=completed()
    check(result['stats']['confirmed']==fixture['automatic'] and result['stats']['automatic_pending']==1,'新增目标账号后保留原确认并补充自动建议')
    change('confirm_automatic');completed()
    admin=api(target['base'],'GET','/user',token=target['token'])
    while True:
        result=current()
        rows=[r for r in result['rows'] if not r['confirmed']]
        if not rows:
            rows=call('GET',endpoint+'?status=ambiguous')['rows']+call('GET',endpoint+'?status=unmatched')['rows']
            rows=[r for r in rows if not r['confirmed']]
        if not rows:break
        item=rows[0]
        selected=next((u['id'] for u in item['candidates'] if u['eligible']),admin['id'])
        change('select',key=item['key'],target_ids=[selected]);change('confirm',key=item['key'])
    result=current();check(result['ready'] and result['stats']['confirmed']==result['stats']['total'],'手动补齐后达到百分之百')
    item=result['rows'][0]
    change('select',key=item['key'],target_ids=item['selected_ids'])
    check(not current()['ready'],'改动已确认项会撤销确认')
    check(call('POST','/git-migrations/runs',request_run(),raw=True).status_code==409,'确认回退后后端继续阻止执行')
    change('confirm',key=item['key'])
    check(current()['ready'],'重新确认恢复百分之百')
    all_rows=[]
    for page in range(1,1+(result['stats']['total']+49)//50):all_rows.extend(call('GET',endpoint+'?page='+str(page))['rows'])
    expected={r['source']['id']:r['selected_ids'] for r in all_rows}
    run=create_verified_run(call,request_run())
    run=wait(lambda:(lambda x:x if x['status'] not in ('queued','running') else None)(call('GET',f"/git-migrations/runs/{run['id']}")),'完整迁移交接',1200)
    if run['status']!='succeeded':save(folder/'migration-failure.json',run)
    check(run['status']=='succeeded','确认后真实迁移完成交接')
    for checkpoint in run['checkpoint'].values():
        if not checkpoint.get('repo_id'):continue
        repo=api(target['base'],'GET',f"/repositories/{checkpoint['repo_id']}",token=target['token'])
        issues=[]
        for page in range(1,100):
            batch=api(target['base'],'GET',f"/repos/{repo['full_name']}/issues?state=all&type=issues&page={page}&limit=100",token=target['token'])
            if not batch:break
            issues.extend(batch)
        for original in fixture['users'].values():
            issue=next(i for i in issues if i['title']==original['name'])
            check(sorted(u['id'] for u in issue.get('assignees') or [])==expected[original['id']],f"人员 {original['id']} 的目标负责人正确")
    report.update(结果='通过',真实来源人数=len(fixture['users']),自动匹配人数=fixture['automatic'],初始歧义人数=4,初始缺失人数=4,最终确认人数=result['stats']['total'],自动匹配无需逐人输入比例=fixture['automatic']/len(fixture['users']),迁移任务=run['id'])
    save(folder/'account-acceptance.json',report)
    print('真实人员匹配及迁移交接验收通过：'+str(folder/'account-acceptance.json'),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-state',type=Path);parser.add_argument('--fixture',type=Path)
    parser.add_argument('--target-state',type=Path);parser.add_argument('--image-tag');parser.add_argument('--prepare-source',action='store_true');parser.add_argument('--automatic',type=int,default=200)
    parser.add_argument('--verify-state',type=Path)
    args=parser.parse_args()
    if args.verify_state:verify(args.verify_state)
    elif args.prepare_source:source_fixture(args.source_state,args.fixture,args.automatic)
    else:full(args.source_state,args.target_state,args.fixture,args.image_tag)

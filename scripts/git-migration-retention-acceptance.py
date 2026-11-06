#!/usr/bin/env python3
"""真实隔离环境验证多选确认、历史身份保留和有效权限例外，不连接生产。"""
import argparse
import base64
import importlib.util
import json
from pathlib import Path
import secrets
import time

import requests
from git_migration_acceptance_helpers import create_verified_run

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('account_fixture', Path(__file__).with_name('git-migration-account-acceptance.py'))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def prepare(source_path, target_path, data_path, tag, output):
    source = json.loads(source_path.read_text())
    data = json.loads(data_path.read_text())
    prefix = data['prefix']
    auth = ('source-admin', source['password'])
    repo = '/repos/source-admin/source-project'
    call = lambda method, path, body=None: fixture.api(source['base'], method, path, auth=auth, body=body)
    default = call('GET', repo)['default_branch']
    call('POST', repo + '/contents/retention.c', {'message':'人员身份保留验收变更', 'content':base64.b64encode(b'int main(void) { return 0; }\n').decode(), 'branch':default, 'new_branch':'retention-review'})
    pull = call('POST', repo + '/pulls', {'title':'未绑定评审人保留来源身份', 'head':'retention-review', 'base':default, 'body':'原始合并请求内容'})
    call('PUT', repo + '/collaborators/' + data['users']['201']['login'], {'permission':'read'})
    call('POST', repo + f"/pulls/{pull['number']}/requested_reviewers", {'reviewers':[data['users']['201']['login']]})
    # 四名没有唯一匹配的人员承担有效规则职责，必须继续人工映射。
    call('POST', repo + '/branch_protections', {'branch_name':default, 'rule_name':default,
         'enable_push':True, 'enable_push_whitelist':True, 'push_whitelist_usernames':[data['users']['204']['login'],data['users']['208']['login']],
         'enable_merge_whitelist':True, 'merge_whitelist_usernames':[data['users']['207']['login']],
         'required_approvals':1, 'enable_approvals_whitelist':True, 'approvals_whitelist_username':[data['users']['203']['login']]})
    data['pull_number'] = pull['number']
    fixture.save(data_path, data)
    state = fixture.full(source_path, target_path, data_path, tag, output)
    print('多选与来源身份验收环境：' + str(state), flush=True)


def verify(state_path):
    state = json.loads(state_path.read_text())
    data = json.loads(Path(state['fixture']).read_text())
    target = json.loads(Path(state['target_state']).read_text())
    report = {'说明':'真实隔离 Gitea 到增强版的多选、来源身份保留、权限例外及最终交接验收；非生产规模证明', '断言':[]}
    started = time.monotonic()
    def check(value, name):
        if not value: raise AssertionError(name)
        report['断言'].append(name)
        fixture.save(state_path.parent/'retention-acceptance.json', report)
    session=requests.Session();session.trust_env=False
    def call(method,path,body=None,raw=False):
        r=session.request(method,state['base']+'/api'+path,json=body,timeout=90)
        if raw:return r
        if not 200<=r.status_code<300: raise RuntimeError('验收接口失败：'+path+' HTTP '+str(r.status_code)+' '+r.text[:300])
        return r.json()
    session.headers['X-CSRF-Token']=call('GET','/session')['csrf_token']
    session.headers['X-CSRF-Token']=call('POST','/login',{'password':state['password']})['csrf_token']
    endpoint=f"/git-migrations/account-mappings/{state['mapping_id']}"
    def current():return call('GET',endpoint)
    def done():return fixture.wait(lambda:(lambda x:x if x['status'] not in ('queued','running') else None)(current()),'人员处理完成')
    def change(action,**extra):return call('PATCH',endpoint,{'action':action,'revision':current()['revision'],**extra})
    def pages():
        first=current()
        return [r for page in range(1,1+(first['stats']['total']+49)//50) for r in call('GET',endpoint+'?page='+str(page))['rows']]
    request=lambda:{'inventory_id':state['inventory_id'],'server_id':state['server_id'],'group_id':state['group_id'],'mapping_id':state['mapping_id'],'request_key':secrets.token_hex(16),'source_frozen':True}
    def target_api(method,path,body=None):return fixture.api(target['base'],method,path,token=target['token'],body=body)
    def user_count():
        total=0
        for page in range(1,100):
            batch=target_api('GET',f'/admin/users?page={page}&limit=100')
            if not batch:return total
            total+=len(batch)
        raise AssertionError('目标名单分页超限')
    initial=current(); check(initial['stats']['total']==208 and initial['stats']['rules']==4,'完整盘点208人，四人涉及有效权限')
    rows=pages(); automatic=[r for r in rows if r['match_status']=='automatic']; ordinary=[r for r in rows if not r['critical'] and r['match_status']!='automatic']
    check(len(ordinary)==4,'仅四名普通歧义或缺失人员可保留来源身份')
    selected=automatic[:10]+automatic[-10:]
    change('confirm_selected',selection={'mode':'keys','keys':[r['key'] for r in selected]}); result=done()
    check(result['stats']['confirmed']==20 and result['stats']['automatic_pending']==180,'跨页选择20人只确认20人，上方全部确认剩180')
    query=call('GET',endpoint+'?status=automatic&q='+data['prefix']+'&page=3')
    check(query['selectable_total']==180 and len(query['rows'])==50,'筛选与分页返回完整可选总数')
    excluded=next(r['key'] for r in pages() if r['match_status']=='automatic' and not r['confirmed'])
    change('confirm_selected',selection={'mode':'filtered','status':'automatic','query':data['prefix'],'excluded':[excluded]});result=done()
    check(result['stats']['confirmed']==199 and result['stats']['automatic_pending']==1,'全部筛选结果支持排除一人且不确认范围外人员')
    change('confirm_automatic');result=done()
    check(result['stats']['confirmed']==200 and result['stats']['automatic_pending']==0,'全量自动确认独立于多选范围')
    before_users=user_count(); preview=result['retention_preview']
    check(preview['count']==4 and preview['blocked']==4,'影响预览明确普通人员与有效规则例外')
    denied=call('PATCH',endpoint,{'revision':result['revision'],'action':'retain_unmatched','impact_hash':preview['impact_hash'],'acknowledged':False},True)
    check(denied.status_code==409,'未确认影响时不能保留来源身份')
    change('retain_unmatched',impact_hash=preview['impact_hash'],acknowledged=True)
    result=current();check(result['stats']['retained']==4 and result['stats']['confirmed']==204 and not result['ready'],'快速保留后仍阻止四项权限例外')
    check(user_count()==before_users,'保留来源身份未创建任何目标账号')
    check(call('POST','/git-migrations/runs',request(),True).status_code==409,'权限例外未完成时后端拒绝迁移')
    change('refresh');result=done();check(result['stats']['retained']==4 and result['stats']['mapped']==200,'刷新保留既有两类确认结果')
    blocked=call('GET',endpoint+'?status=rules')['rows']; check(len(blocked)==4 and all(not r['preservable'] for r in blocked),'权限名单不能当成历史身份处理')
    # 单独指定四名有效目标人员，随后一次确认；不是统一映射到管理员。
    for row, user in zip(blocked, automatic[:4]): change('select',key=row['key'],target_ids=user['selected_ids'])
    change('confirm_selected',selection={'mode':'keys','keys':[r['key'] for r in blocked]});result=done()
    check(result['ready'] and result['stats']['confirmed']==208 and result['stats']['mapped']==204 and result['stats']['retained']==4,'所有处理方式确认后才达到百分之百')
    source_id=current()['source_id']
    all_rows=pages(); expected={r['source']['id']:r for r in all_rows}
    run=create_verified_run(call,request())
    run=fixture.wait(lambda:(lambda x:x if x['status'] not in ('queued','running') else None)(call('GET',f"/git-migrations/runs/{run['id']}")),'完整迁移交接',1200)
    if run['status']!='succeeded':fixture.save(state_path.parent/'migration-failure.json',run)
    check(run['status']=='succeeded','真实迁移及原生对象回读完整通过')
    repo_checkpoints=[c for c in run['checkpoint'].values() if isinstance(c,dict) and c.get('repo_id')]
    check(len(repo_checkpoints)==1,'单仓库完整交接')
    repo=target_api('GET',f"/repositories/{repo_checkpoints[0]['repo_id']}")
    issues=[]
    for page in range(1,100):
        batch=target_api('GET',f"/repos/{repo['full_name']}/issues?state=all&type=issues&page={page}&limit=100")
        if not batch:break
        issues.extend(batch)
    for original in data['users'].values():
        row=expected[original['id']];issue=next(i for i in issues if i['title']==original['name'])
        check(sorted(u['id'] for u in issue.get('assignees') or [])==row['selected_ids'],f"人员{original['id']}的原生负责人关系正确")
        if row.get('resolution')=='source_identity':
            check('迁移来源人员（未绑定目标账号）' in issue['body'] and original['login'] in issue['body'] and f'来源：gitea/{source_id}' in issue['body'] and f"编号：{original['id']}" in issue['body'],f"人员{original['id']}的来源身份在事项正文可见")
    pull=target_api('GET',f"/repos/{repo['full_name']}/pulls/{data['pull_number']}")
    check(not pull.get('requested_reviewers') and data['users']['201']['login'] in pull['body'],'保留未绑定评审来源记录且不伪造目标评审请求')
    rules=target_api('GET',f"/repos/{repo['full_name']}/branch_protections")
    check(bool(rules) and rules[0]['required_approvals']==1,'原审批要求与有效规则仍保留')
    check(user_count()==before_users,'完整迁移未新增业务账号')
    report.update(结果='通过',真实来源人数=208,多选确认人数=20,保留来源人数=4,有效规则例外人数=4,迁移任务=run['id'],验收耗时秒=round(time.monotonic()-started,2))
    fixture.save(state_path.parent/'retention-acceptance.json',report)
    print('多选与来源身份完整验收通过：'+str(state_path.parent/'retention-acceptance.json'),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-state',type=Path);parser.add_argument('--target-state',type=Path)
    parser.add_argument('--fixture',type=Path);parser.add_argument('--image-tag');parser.add_argument('--output',type=Path)
    parser.add_argument('--verify-state',type=Path)
    args=parser.parse_args()
    if args.verify_state:verify(args.verify_state)
    else:prepare(args.source_state,args.target_state,args.fixture,args.image_tag,args.output)

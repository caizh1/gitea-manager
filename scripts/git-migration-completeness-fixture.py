#!/usr/bin/env python3
"""为真实隔离来源补齐评论附件、人员、讨论及全部已定义的合并方式组合。"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
from urllib.parse import quote
import requests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-state', type=Path, required=True)
    parser.add_argument('--fixture', type=Path, required=True)
    args = parser.parse_args()
    state, fixture = json.loads(args.source_state.read_text()), json.loads(args.fixture.read_text())
    info = json.loads(subprocess.check_output(['docker','inspect',state['name']]))[0]
    assert info['Config']['Labels'].get('io.gitea-manager.test') == 'git-migration-source', '仅可修改独立测试来源'
    assert 'completeness' not in fixture, '完整性夹具已经创建，不能盲目重复写入'
    kind, base, project = state['kind'], state['base'], fixture['project']
    owner = 'root' if kind == 'gitlab' else 'source-admin'
    client = requests.Session(); client.trust_env = False
    if kind == 'gitea': client.auth = (owner,state['password'])
    else:
        r = client.post(base+'/oauth/token',data={'grant_type':'password','username':owner,'password':state['password'],'scope':'api'},timeout=60)
        r.raise_for_status(); client.headers['Authorization'] = 'Bearer '+r.json()['access_token']
    prefix = '/api/v4' if kind == 'gitlab' else '/api/v1'
    def api(method,path,**kwargs):
        r = client.request(method,base+prefix+path,timeout=90,**kwargs)
        if not 200 <= r.status_code < 300: raise RuntimeError('完整性夹具接口失败：'+method+' '+path+' HTTP '+str(r.status_code))
        return r.json() if r.content and r.headers.get('Content-Type','').startswith('application/json') else None
    root = '/projects/'+str(project['id']) if kind == 'gitlab' else '/repos/'+project['full_name']
    key = secrets.token_hex(4)
    people = {}
    for role in ('assignee','reviewer'):
        name, password = role+'-'+key, secrets.token_urlsafe(24)
        user = api('POST','/users' if kind=='gitlab' else '/admin/users',json={
            'username':name,'name':name,'password':password,'email':name+'@example.invalid',
            **({'skip_confirmation':True} if kind=='gitlab' else {'must_change_password':False})})
        api('POST',root+'/members',json={'user_id':user['id'],'access_level':30}) if kind=='gitlab' else api('PUT',root+'/collaborators/'+name,json={'permission':'write'})
        people[role] = user
    issue = api('POST',root+'/issues',json=({'title':'完整性与人员映射','description':'保留负责人、评论及附件','assignee_ids':[people['assignee']['id']]}
        if kind=='gitlab' else {'title':'完整性与人员映射','body':'保留负责人、评论及附件','assignees':[people['assignee']['login']]}))
    number = issue['iid'] if kind=='gitlab' else issue['number']
    files = []
    if kind == 'gitea':
        comment = api('POST',root+'/issues/'+str(number)+'/comments',json={'body':'评论附件清单'})
        body = '两个同名附件应分别保留\n'
        for index in range(2):
            data = ('独立评论附件 '+str(index)+'\n').encode()
            asset = api('POST',root+'/issues/comments/'+str(comment['id'])+'/assets',files={'attachment':('build.log',data,'text/plain')})
            body += '[编译日志 '+str(index)+']('+asset['browser_download_url']+')\n'
            files.append({'source_id':'attachment-'+str(asset['id']),'sha256':hashlib.sha256(data).hexdigest()})
        api('PATCH',root+'/issues/comments/'+str(comment['id']),json={'body':body})
        pull_number = fixture['pull']['number']
        api('PATCH',root+'/pulls/'+str(pull_number),json={'assignees':[people['assignee']['login']]})
        api('POST',root+'/pulls/'+str(pull_number)+'/requested_reviewers',json={'reviewers':[people['reviewer']['login']]})
        review = api('POST',root+'/pulls/'+str(pull_number)+'/reviews',json={'event':'COMMENT','body':'需要保留的行内讨论',
            'comments':[{'path':'cross.txt','new_position':1,'body':'原始行内意见'}]})
        inline = api('GET',root+'/pulls/'+str(pull_number)+'/reviews/'+str(review['id'])+'/comments')[0]
        data = '行内评论的附件\n'.encode()
        asset = api('POST',root+'/issues/comments/'+str(inline['id'])+'/assets',files={'attachment':('inline.log',data,'text/plain')})
        files.append({'source_id':'attachment-'+str(asset['id']),'sha256':hashlib.sha256(data).hexdigest()})
    else:
        upload = api('POST',root+'/uploads',files={'file':('build.log','来源评论日志\n'.encode(),'text/plain')})
        api('POST',root+'/issues/'+str(number)+'/notes',json={'body':'评论上传文件\n'+upload['markdown']})
        pull_number = fixture['pull']['iid']
        pull = api('PUT',root+'/merge_requests/'+str(pull_number),json={'assignee_ids':[people['assignee']['id']],'reviewer_ids':[people['reviewer']['id']]})
        refs = pull['diff_refs']
        discussion = api('POST',root+'/merge_requests/'+str(pull_number)+'/discussions',json={'body':'必须解决的行内讨论','position':{
            'position_type':'text','base_sha':refs['base_sha'],'start_sha':refs['start_sha'],'head_sha':refs['head_sha'],
            'old_path':'cross.txt','new_path':'cross.txt','new_line':1}})
        fixture['unresolved_discussion'] = {'pull':pull_number,'discussion':discussion['id']}
        api('PUT',root,json={'only_allow_merge_if_pipeline_succeeds':True,'only_allow_merge_if_all_discussions_are_resolved':True})
    data = '通用 C 构建资料\n'.encode(); name = 'generic-'+key
    path = root+'/packages/generic/'+name+'/1.0.0/build.txt' if kind=='gitlab' else None
    if kind=='gitlab': api('PUT',path,data=data)
    else:
        r = client.put(base+'/api/packages/'+owner+'/generic/'+name+'/1.0.0/build.txt',data=data,timeout=60); r.raise_for_status()
        api('POST','/packages/'+owner+'/generic/'+name+'/-/link/'+project['name'])
    fixture['packages'].append({'type':'generic','name':name,'version':'1.0.0','files':{'build.txt':hashlib.sha256(data).hexdigest()}})
    cases = []
    combinations = [(method,squash,style) for method in ('merge','rebase_merge','ff') for squash in ('never','always','default_on','default_off')
                    for style in ((['fast-forward-only'] if method=='ff' else ['merge']) if squash=='never' else ['squash'] if squash=='always' else
                                  (['fast-forward-only','squash'] if method=='ff' else ['merge','squash']))] if kind=='gitlab' else [('', '',style) for style in ('merge','rebase','rebase-merge','squash','fast-forward-only')]
    for index,(method,squash,style) in enumerate(combinations):
        repo_name = 'merge-'+key+'-'+str(index)
        repo = api('POST','/projects' if kind=='gitlab' else '/user/repos',json={'name':repo_name, 'default_branch':'main',
            **({'visibility':'private','initialize_with_readme':True} if kind=='gitlab' else {'private':True,'auto_init':True})})
        route = '/projects/'+str(repo['id']) if kind=='gitlab' else '/repos/'+repo['full_name']
        if kind=='gitlab':
            api('PUT',route,json={'merge_method':method,'squash_option':squash})
            api('POST',route+'/wikis',json={'title':'Home','content':'合并条件验收百科'})
            api('POST',route+'/repository/branches',json={'branch':'feature','ref':'main'})
            for revision in (1,2):
                api('POST',route+'/repository/commits',json={'branch':'feature','commit_message':'合并图验收 '+str(revision),
                    'actions':[{'action':'create' if revision==1 else 'update','file_path':'merge.txt','content':'第 '+str(revision)+' 个提交\n'}]})
            pull = api('POST',route+'/merge_requests',json={'source_branch':'feature','target_branch':'main','title':'合并条件矩阵'})
        else:
            config = {field:False for field in ('allow_merge_commits','allow_rebase','allow_rebase_explicit','allow_squash_merge','allow_fast_forward_only_merge','allow_manual_merge')}
            fields = {'merge':'allow_merge_commits','rebase':'allow_rebase','rebase-merge':'allow_rebase_explicit','squash':'allow_squash_merge','fast-forward-only':'allow_fast_forward_only_merge'}
            config[fields[style]]=True;config.update(default_merge_style=style,has_wiki=True)
            api('PATCH',route,json=config)
            api('POST',route+'/wiki/new',json={'title':'Home','content_base64':base64.b64encode('合并条件验收百科\n'.encode()).decode(),'message':'百科初始化'})
            api('POST',route+'/contents/merge.txt',json={'branch':'main','new_branch':'feature','content':base64.b64encode('合并图验收\n'.encode()).decode(),'message':'合并条件验收'})
            pull = api('POST',route+'/pulls',json={'base':'main','head':'feature','title':'合并条件矩阵'})
        cases.append({'project_id':repo['id'],'path':repo.get('path_with_namespace') or repo['full_name'],'pull':pull.get('iid') or pull['number'],
                      'method':method,'squash':squash,'style':style})
    fixture['completeness'] = {'说明':'所有合并方式组合均创建真实开放请求；不是只验证配置接口','issue':number,'comment_files':files,
        'principal_map':{kind+':person:'+str(user['id']):['migration-'+role] for role,user in people.items()},'merge_cases':cases}
    args.fixture.write_text(json.dumps(fixture,ensure_ascii=False,indent=2)+'\n')
    print('完整性真实夹具已建立，合并变体：'+str(len(cases)))


if __name__ == '__main__': main()

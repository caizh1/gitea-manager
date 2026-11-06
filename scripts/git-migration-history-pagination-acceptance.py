#!/usr/bin/env python3
"""在标记隔离实例构造分页夹具，验证原生删除不会阻断后续历史页。"""
import argparse,json,re,secrets,subprocess
from pathlib import Path
import requests
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--target-state',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
a=p.parse_args();state=json.loads(a.target_state.read_text())
info=json.loads(subprocess.check_output(['docker','inspect',state['name']]))[0]
assert info['Config']['Labels']['io.gitea-manager.test']=='git-migration-receiver'
client=requests.Session();client.trust_env=False;client.headers['Authorization']='token '+state['token']
def api(method,path,value=None):
 r=client.request(method,state['base']+'/api/v1'+path,json=value,timeout=60)
 assert r.ok,(method,path,r.status_code)
 return r.json() if r.content else None
def sql(value):
 return subprocess.check_output(['docker','exec',state['name'],'sqlite3','-cmd','.timeout 30000','-json','/data/gitea/gitea.db',value],text=True)
def q(value):return "'"+str(value).replace("'","''")+"'"
actor=api('GET','/user');name='history-pagination-'+secrets.token_hex(4)
repo=api('POST','/user/repos',{'name':name,'private':True,'auto_init':True})
route='/repos/'+repo['full_name'];issue=api('POST',route+'/issues',{'title':'分页删除父议题'})
key=secrets.token_hex(16)
# 仅为列表分页构造持久化夹具；不是迁移导入或容量基准。
sql('INSERT INTO governance_migration_session (key,repo_id,actor_id,source_digest,state) VALUES ('+q(key)+','+str(repo['id'])+','+str(actor['id'])+','+q('0'*64)+",'handed_off');")
sid=json.loads(sql('SELECT id FROM governance_migration_session WHERE key='+q(key)))[0]['id']
sql('INSERT INTO governance_migration_receipt (session_id,kind,source_id,digest,target_id,payload) VALUES ('+str(sid)+",'issue','parent',"+q('0'*64)+','+str(issue['id'])+",'{}');")
statements=[]
for index in range(50):
 source='deleted-thread-'+str(index)
 data=json.dumps({'thread_id':source,'parent_kind':'issue','parent_source_id':'parent','notes':[]})
 statements.append('INSERT INTO governance_migration_history (repo_id,session_id,source_id,kind,name,parent_id,data) VALUES ('+str(repo['id'])+','+str(sid)+','+q(source)+",'discussion_snapshot','删除后应隐藏','parent',"+q(data)+');')
statements.append('INSERT INTO governance_migration_history (repo_id,session_id,source_id,kind,name,data) VALUES ('+str(repo['id'])+','+str(sid)+",'visible','ci_pipeline','保留分页记录','{}');")
sql('BEGIN IMMEDIATE;'+''.join(statements)+'COMMIT;')
ids=json.loads(sql('SELECT id FROM governance_migration_history WHERE repo_id='+str(repo['id'])+' ORDER BY id'))
api('DELETE',route+'/issues/'+str(issue['number']))
client.auth=(actor['login'],state['password'])
client.get(state['base']+'/user/login',timeout=30)
login=client.post(state['base']+'/user/login',headers={'Origin':state['base']},data={'user_name':actor['login'],'password':state['password']},allow_redirects=False,timeout=30)
assert login.status_code in (302,303)
url=state['base']+'/'+repo['full_name']+'/migration-history'
first=client.get(url,timeout=30);assert first.status_code==200 and 'deleted-thread-' not in first.text
match=re.search(r'href="\?after_id=(\d+)"',first.text);assert match
second=client.get(url+'?after_id='+match[1],timeout=30);assert second.status_code==200 and '保留分页记录' in second.text
assert client.get(url+'/'+str(ids[0]['id']),timeout=30).status_code==404
assert len(json.loads(sql('SELECT id FROM governance_migration_history WHERE repo_id='+str(repo['id']))))==51
result={'说明':'新建隔离仓库，用数据库夹具构造51条历史，并通过原生接口删除父议题；不代表51条实际迁移或容量验收','结果':'通过','隐藏记录数':50,'后续可见记录数':1,'历史记录仍保存':51,'第一页HTTP':first.status_code,'第二页HTTP':second.status_code,'删除详情HTTP':404}
a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print('跨页删除历史验收通过：'+str(a.output))

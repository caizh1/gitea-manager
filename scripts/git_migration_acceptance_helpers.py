"""验收脚本共用正式入口；必须先经过真实预迁移，禁止绕过门禁。"""
import secrets
import time


def create_verified_run(call, request, seconds=1800, on_poll=None):
    data={k:v for k,v in request.items() if k in ('inventory_id','server_id','group_id','mapping_id')}
    data['request_key']=secrets.token_hex(20)
    before={r['id'] for r in call('GET','/git-migrations/runs')}
    trial=call('POST','/git-migrations/preflights',data)
    path='/git-migrations/preflights/'+str(trial['id'])
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        trial=call('GET',path)
        if on_poll: trial=on_poll(trial,path)
        if trial['status'] not in ('queued','running'):break
        time.sleep(.3)
    if trial['status']=='blocked' and trial.get('compatibility',{}).get('differences'):
        # 固定隔离基准明确采用目标原生讨论规则，真实管理员页面仍须人工确认。
        call('POST',path+'/compatibility-acknowledgements',{'digest':trial['compatibility']['digest']})
        while time.monotonic()<deadline:
            trial=call('GET',path)
            if on_poll: trial=on_poll(trial,path)
            if trial['status'] not in ('queued','running'):break
            time.sleep(.3)
    if trial['status']!='ready' or not trial['can_start']:
        raise RuntimeError('真实预迁移未通过：'+str(trial))
    if before!={r['id'] for r in call('GET','/git-migrations/runs')}:
        raise AssertionError('预迁移提前创建了正式任务')
    call('POST',path+'/start',{'source_frozen':True,'request_key':request['request_key']})
    while time.monotonic()<deadline:
        trial=call('GET',path)
        if on_poll: trial=on_poll(trial,path)
        if trial['run_id']:return call('GET','/git-migrations/runs/'+str(trial['run_id']))
        if trial['status'] not in ('queued','running'):raise RuntimeError('正式启动前复检未通过：'+str(trial))
        time.sleep(.3)
    raise RuntimeError('预迁移及正式启动等待超时')


def target_rows(target, query):
    """隔离验收的只读查询，SQLite 与 PostgreSQL 返回一致的行对象。"""
    import json, subprocess
    assert query.lstrip().upper().startswith('SELECT '), '验收读取仅接受 SELECT'
    if target.get('database') == 'postgres':
        command=['docker','exec',target['name']+'-db','psql','-U','gitea','-d','gitea','-At','-v','ON_ERROR_STOP=1','-c',"SELECT COALESCE(json_agg(row_value),'[]'::json)::text FROM ("+query+") row_value"]
    else:
        command=['docker','exec',target['name'],'sqlite3','-json','/data/gitea/gitea.db',query]
    return json.loads(subprocess.check_output(command,text=True) or '[]')

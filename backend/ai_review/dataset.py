"""仓库内人工审查数据集：不可变修订、定量检索与离线导出。"""
import contextlib
import hashlib
import io
import json
import re
import time
import zipfile
from .common import ReviewError, digest, encode


def migrate(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS review_examples (
        id TEXT PRIMARY KEY, repository INTEGER NOT NULL, number INTEGER NOT NULL,
        origin_comment INTEGER NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL,
        target TEXT NOT NULL, author INTEGER NOT NULL, value TEXT NOT NULL,
        updated REAL NOT NULL, disabled INTEGER NOT NULL DEFAULT 0);
    CREATE INDEX IF NOT EXISTS examples_repo ON review_examples(repository,status,updated);
    CREATE TABLE IF NOT EXISTS review_example_history (
        id TEXT NOT NULL, revision INTEGER NOT NULL, value TEXT NOT NULL,
        PRIMARY KEY(id,revision));
    CREATE TABLE IF NOT EXISTS review_finding_archive (
        id TEXT PRIMARY KEY, repository INTEGER NOT NULL, number INTEGER NOT NULL,
        value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS review_feedback_events (
        repository INTEGER NOT NULL, delivery TEXT NOT NULL, value TEXT NOT NULL,
        due REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
        error TEXT NOT NULL DEFAULT '', PRIMARY KEY(repository,delivery));
    CREATE TABLE IF NOT EXISTS review_feedback_receipts (
        repository INTEGER NOT NULL, comment INTEGER NOT NULL, ack_id INTEGER,
        attempted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(repository,comment));
    CREATE TABLE IF NOT EXISTS review_archive_jobs (job TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS review_example_validation (id TEXT PRIMARY KEY, expires REAL NOT NULL, checked REAL NOT NULL);
    ''')


def finding_id(job, finding):
    return 'AR-' + digest([job['id'], finding.get('candidate_id') or finding['fingerprint']])[:24]


def observe_origin(store, repository, origin):
    with store.tx() as c:
        row=c.execute('SELECT value FROM repositories WHERE id=?',(repository,)).fetchone()
        if not row:return
        value=json.loads(row[0]);known=value.get('feedback_origins',[])
        if origin not in known and len(known)<8:
            value['feedback_origins']=known+[origin]
            c.execute('UPDATE repositories SET value=? WHERE id=?',(encode(value),repository))


def archive(store, job, snapshot, result):
    repo = store.repo(job['repository'])
    origin=(repo.get('feedback_origins') or [repo.get('gitea_url','').rstrip('/')+'/'+repo['repo_name']])[0]
    with store.tx() as c:
        for f in result.get('findings', []):
            if f.get('quality_version', 0) < 3:
                continue
            source = next((e['input'] for e in result.get('evidence', []) if e.get('scope', {}).get('name') == f.get('scope') and e.get('path') == f['path']), {})
            # 持久化必要上下文；不归档模型密钥或管理端配置。
            value = dict(id=finding_id(job, f), repository=repo['repo_name'], server_id=repo['server_id'],
                         pr=job['number'], job=job['id'], head=snapshot['head'], base=snapshot['base'],
                         merge_base=snapshot['merge_base'], original=f, context=source,
                         model=(job.get('configuration') or {}).get('model'), analysis_version=3,
                         source_url=origin+f'/pulls/{job["number"]}'+('#'+job['review_fragment'] if job.get('review_fragment') else ''),
                         source_review=job.get('review_id'), source_bot=repo.get('bot_id'))
            c.execute('INSERT OR IGNORE INTO review_finding_archive VALUES (?,?,?,?)',
                      (value['id'], repo['id'], job['number'], encode(value)))
        c.execute('INSERT OR IGNORE INTO review_archive_jobs VALUES (?)', (job['id'],))


def archive_pending(store):
    with contextlib.closing(store.connect()) as c:
        rows=c.execute("SELECT id FROM jobs WHERE status='succeeded' AND snapshot IS NOT NULL AND result IS NOT NULL AND id NOT IN (SELECT job FROM review_archive_jobs) LIMIT 10").fetchall()
    for row in rows:
        job=store.job(row[0]);archive(store,job,job['snapshot'],job['result'])


def enqueue(store, repo, delivery, payload, event):
    comment = payload.get('comment', {})
    pr = payload.get('pull_request') or payload.get('issue') or {}
    cid, number = comment.get('id'), pr.get('number')
    action = payload.get('action')
    if action == 'reviewed':
        cid = 0
    if type(cid) is not int or cid < 0 or (cid == 0 and action != 'reviewed') or type(number) is not int or number <= 0:
        raise ReviewError('评论事件缺少有效PR或评论编号')
    if action not in ('created', 'edited', 'deleted', 'reviewed'):
        return
    # 只保存处理事件所需的身份，不将整个Webhook仓库对象复制进数据集。
    value = {'comment': cid, 'number': number, 'action': action, 'event': event,
             'sender': payload.get('sender', {}), 'received': time.time(),
             'body_hash': digest(comment.get('body','')), 'updated_at': comment.get('updated_at')}
    value['sender'] = {k: value['sender'].get(k) for k in ('id', 'login')}
    with store.tx() as c:
        inserted=c.execute('INSERT OR IGNORE INTO review_feedback_events(repository,delivery,value,due) VALUES (?,?,?,?)',
                           (repo['id'], delivery, encode(value), time.time())).rowcount
        if inserted and action in ('edited','deleted'):
            invalidate(c,repo['id'],cid,'来源或确认发生变更，等待核对')


def invalidate(c, repository, comment, reason):
    rows=c.execute("SELECT * FROM review_examples WHERE repository=? AND status='active' AND (origin_comment=? OR json_extract(value,'$.source_comment')=?)",(repository,comment,comment)).fetchall()
    for row in rows:
        value=json.loads(row['value']);value.update(status='withdrawn',invalidation=reason,revision=row['revision']+1,updated_at=time.time())
        c.execute('UPDATE review_examples SET status=?,revision=?,updated=?,value=? WHERE id=?',('withdrawn',value['revision'],value['updated_at'],encode(value),row['id']))
        c.execute('INSERT INTO review_example_history VALUES (?,?,?)',(row['id'],value['revision'],encode(value)))


def put(store, repository, number, origin_comment, value, author=0):
    ident = 'DS-' + digest([repository, origin_comment])[:24]
    with store.tx() as c:
        previous = c.execute('SELECT * FROM review_examples WHERE id=?', (ident,)).fetchone()
        # 采集原评论的刷新不能覆盖在同一评论中明确给出的收录命令。
        value = dict(value, schema_version=1, id=ident, repository_id=repository, pr=number, origin_comment=origin_comment)
        if previous and previous['disabled']:
            value['status'] = 'disabled'
        comparable = dict(value)
        if previous:
            old = json.loads(previous['value'])
            for key in ('revision', 'created_at', 'updated_at'):
                old.pop(key, None)
            if old == comparable:
                return json.loads(previous['value'])
        now = time.time();revision = previous['revision'] + 1 if previous else 1
        value.update(revision=revision, created_at=json.loads(previous['value'])['created_at'] if previous else now, updated_at=now)
        c.execute('''INSERT INTO review_examples(id,repository,number,origin_comment,revision,status,target,author,value,updated)
                     VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,status=excluded.status,
                     target=excluded.target,author=excluded.author,value=excluded.value,updated=excluded.updated''',
                  (ident,repository,number,origin_comment,revision,value['status'],value.get('target',''),author,encode(value),now))
        c.execute('INSERT INTO review_example_history VALUES (?,?,?)', (ident,revision,encode(value)))
        if value['status']=='active':
            c.execute('INSERT OR REPLACE INTO review_example_validation VALUES (?,?,?)',(ident,time.time()+300,time.time()))
        return value


def withdraw(store, repository, comment, reason):
    with contextlib.closing(store.connect()) as c:
        rows = c.execute('SELECT * FROM review_examples WHERE repository=?', (repository,)).fetchall()
    for row in rows:
        value=json.loads(row['value'])
        if row['origin_comment'] == comment or value.get('source_comment') == comment:
            put(store, repository, row['number'], row['origin_comment'],
                {k:v for k,v in dict(value,status='withdrawn',invalidation=reason).items() if k not in ('revision','created_at','updated_at','schema_version','id','repository_id','pr','origin_comment')}, row['author'])


def list_examples(store, repository, status=None, page=1):
    where='repository=?';args=[repository]
    if status:
        where+=' AND status=?';args.append(status)
    with contextlib.closing(store.connect()) as c:
        total=c.execute('SELECT count(*) FROM review_examples WHERE '+where,args).fetchone()[0]
        rows=c.execute('SELECT value FROM review_examples WHERE '+where+' ORDER BY updated DESC LIMIT 30 OFFSET ?',args+[(page-1)*30]).fetchall()
        conflicts=conflict_targets(c,repository)
        backlog=c.execute("SELECT count(*),max(error) FROM review_feedback_events WHERE repository=? AND status='pending'",(repository,)).fetchone()
        valid={r[0] for r in c.execute('SELECT v.id FROM review_example_validation v JOIN review_examples e ON e.id=v.id WHERE e.repository=? AND v.expires>?',(repository,time.time()))}
    items=[json.loads(r[0]) for r in rows]
    for item in items:
        item['conflict']=item.get('target') in conflicts
        item['reference_ready']=item['status']=='active' and not item['conflict'] and item['id'] in valid
    return {'items':items,'total':total,'pending_events':backlog[0],'sync_error':backlog[1] or ''}


def conflict_targets(c, repository):
    return {r[0] for r in c.execute("""SELECT target FROM review_examples WHERE repository=? AND status='active' AND disabled=0
        GROUP BY target HAVING count(DISTINCT json_extract(value,'$.label') || ':' || json_extract(value,'$.correction')) > 1""",(repository,))}


def active_rows(store, repository):
    with contextlib.closing(store.connect()) as c:
        rows=[dict(r) for r in c.execute("SELECT e.* FROM review_examples e JOIN review_example_validation v ON v.id=e.id WHERE repository=? AND status='active' AND disabled=0 AND v.expires>? ORDER BY updated DESC LIMIT 2000", (repository,time.time()))]
        conflicts=conflict_targets(c,repository)
    result=[];size=0
    for r in rows:
        size+=len(r['value'])
        if size>32*1024*1024:break
        if r['target'] not in conflicts:result.append(json.loads(r['value']))
    return result


def scope_symbols(finding):
    """归档的所属函数由源码解析器确定，赋值和字面量返回也能关联同函数反馈。"""
    scope=finding.get('scope','')
    # 解析器保存完整声明（含参数），早期导入记录也可能只有函数名。
    name=scope.split('(',1)[0].strip().lstrip('*& ').strip() if isinstance(scope,str) else ''
    if re.fullmatch(r'[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*',name):
        return {part for part in name.split('::') if len(part)>=3}
    return set()


def retrieve(store, repository, item, records=None):
    if records is None:records=active_rows(store,repository)
    if not records:return []
    sources=[item['before'],item['after'],*item.get('context',[])]
    code='\n'.join(s.get('code','') for s in sources)
    paths={item['path'],*(s.get('path') for s in sources)}
    symbols=set(re.findall(r'\b[A-Za-z_]\w{2,}\b',code))
    ranked=[]
    for v in records:
        # 兼容早期归档中没有操作标识的赋值/返回意见，不改写原始修订。
        relevance=(set(v.get('symbols',[])) | scope_symbols(v.get('original',{}))) & symbols
        # 同文件或相同操作符号提供明确关联；禁止纯标题关键词全库泛匹配。
        if not relevance or (v.get('path') not in paths and len(relevance)<2):
            continue
        item_value={k:v.get(k) for k in ('id','revision','label','correction','conditions','path','source_url')}
        original=v.get('original',{})
        item_value['prior_issue']=(original.get('title','')+' '+original.get('explanation',''))[:650]
        ranked.append((len(relevance)+(10 if v.get('path')==item['path'] else 0),v['updated_at'],item_value))
    chosen=[]
    for _,_,v in sorted(ranked,key=lambda x:(x[0],x[1]),reverse=True):
        if len(encode(chosen+[v])) <= 2000:
            chosen.append(v)
        if len(chosen)==3:break
    return chosen


def disable(store, repository, ident):
    with store.tx() as c:
        row=c.execute('SELECT * FROM review_examples WHERE id=? AND repository=?',(ident,repository)).fetchone()
        if not row:raise ReviewError('样本不存在于所选仓库')
        c.execute('UPDATE review_examples SET disabled=1 WHERE id=?',(ident,))
    value=json.loads(row['value'])
    return put(store,repository,row['number'],row['origin_comment'],
               {k:v for k,v in dict(value,status='disabled').items() if k not in ('revision','created_at','updated_at','schema_version','id','repository_id','pr','origin_comment')},row['author'])


def export(store, repository, history=False):
    with contextlib.closing(store.connect()) as c:
        if history:
            rows=c.execute('SELECT h.value FROM review_example_history h JOIN review_examples e ON e.id=h.id WHERE e.repository=? ORDER BY h.id,h.revision',(repository,))
            values=[];size=0
            for row in rows:
                size+=len(row[0])
                if len(values)>=10000 or size>32*1024*1024:raise ReviewError('单次导出超过10000条或32MB，请先按仓库归档数据')
                values.append(json.loads(row[0]))
        else:
            values=active_rows(store,repository)
    raw=(''.join(encode(v)+'\n' for v in values)).encode()
    manifest={'说明':'仓库内人工Review与AI纠正数据集，UTF-8 JSONL；原文与纠正分开，未进行模型训练。','schema_version':1,
              'repository_id':repository,'历史修订':history,'样本数':len(values),'SHA256':hashlib.sha256(raw).hexdigest()}
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('审查数据集.jsonl',raw);z.writestr('清单.json',encode(manifest))
        z.writestr('格式说明.md','# 审查经验数据集\n\n一行一个JSON对象。schema_version为格式版本，revision为样本修订。original是原始建议；correction为人工纠正；conditions是适用条件；context为固定版本上下文；source_url和confirmed_by为来源。status为active时才可参考，冲突记录不用于后续审查。严禁仅凭历史标签认定当前代码存在或不存在缺陷。\n')
    stream.seek(0);return stream

"""管理侧反馈同步进程：仅用管理凭据核实权限，不执行模型或仓库命令。"""
import contextlib
import json
import logging
import os
import re
import signal
import sqlite3
import threading
import time
from datetime import datetime
from urllib.parse import quote, urlsplit
from .common import ReviewError, digest, encode
from .gitea import Gitea
from .store import Store
from . import dataset

LABELS={'误报':'false_positive','排除':'exclude_by_policy','部分错误':'partial_error','定位错误':'wrong_location','有效':'valid','撤销':'withdrawn'}


class CommandError(ReviewError):
    """已完成核实的无效命令；网络或权限服务异常仍须异步重试。"""
    pass


def manager_client(repo):
    path=os.environ.get('AI_REVIEW_MANAGER_DB','/app/data/gitea-manager.db')
    with contextlib.closing(sqlite3.connect('file:'+path+'?mode=ro',uri=True,timeout=2)) as c:
        row=c.execute('SELECT gitea_url,api_token FROM gitea_servers WHERE id=?',(repo['server_id'],)).fetchone()
    if not row or row[0].rstrip('/') != repo['gitea_url'].rstrip('/'):
        raise ReviewError('管理端服务器身份不一致，未接受反馈')
    return Gitea(row[0],row[1],repo['repo_name'],repo['repo_id'])


def can_write(client, user):
    if type(user.get('id')) is not int or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',user.get('login','')):
        return False
    actual=client.request('GET','/users/'+quote(user['login'],safe=''),missing=True)
    if (not actual or actual.get('id') != user['id'] or
            not actual.get('active',True) or actual.get('prohibit_login',False)):
        return False
    permission=client.request('GET',client.prefix+'/collaborators/'+quote(user['login'],safe='')+'/permission',missing=True)
    if permission and permission.get('permission') in ('write','admin','owner'):
        return True
    # Gitea 分单元团队总体权限可能为 none，沿用策略中心的代码单元核验方式。
    owner=client.repository().get('owner',{}).get('login')
    if not owner or not client.request('GET','/orgs/'+quote(owner,safe=''),missing=True):
        return False
    for team in client.pages(client.prefix+'/teams',max_pages=20):
        units=team.get('units_map')
        mode=units.get('repo.code') if units else team.get('permission')
        if mode not in ('write','admin','owner') or type(team.get('id')) is not int:
            continue
        member=client.request('GET',f'/teams/{team["id"]}/members/'+quote(user['login'],safe=''),missing=True)
        if member and member.get('id')==user['id']:
            return True
    return False


def comment_at(client, number, ident):
    # 按PR集合读取，不能仅凭任意评论ID建立跨PR引用。
    for c in client.pages(client.prefix+f'/issues/{number}/comments',max_pages=20):
        if c.get('id')==ident:return c,None
    reviews=client.pages(client.prefix+f'/pulls/{number}/reviews',max_pages=10)
    for review in reviews:
        if review.get('state') not in ('COMMENT','APPROVED','REQUEST_CHANGES'):
            continue
        for c in client.pages(client.prefix+f'/pulls/{number}/reviews/{review["id"]}/comments',max_pages=10):
            if c.get('id')==ident:return c,review
    return None,None


def symbol_keys(text):
    # 记录操作相关标识，排除C关键词与通用短词；不使用模型生成的分类路由。
    return sorted(set(re.findall(r'\b([A-Za-z_]\w{2,})\s*(?:\(|->|\.)',text)))[:40]


def source_value(client, repo, number, comment, review):
    pr=client.pr(number)
    head=review.get('commit_id') if review else None
    reliable=bool(head and re.fullmatch(r'[0-9a-f]{40}',head))
    # 普通评论的当前PR head不等于评论当时head，明确保留未知版本，不猜历史行。
    context={};path=comment.get('path','');line=comment.get('position') or comment.get('original_position')
    if reliable and path and comment.get('position'):
        code=client.file(path,head).decode('utf-8')
        lines=code.splitlines()
        if type(line) is int and 0 < line <= len(lines):
            start=max(0,line-16);context={'version':head,'path':path,'start':start+1,'code':'\n'.join(lines[start:line+15])}
        else:reliable=False
    elif reliable and path:
        # 旧侧base会移动；评论hunk保留原文，但无可确认旧SHA时只作为候选。
        reliable=False
    original={'title':comment.get('body','').split('\n')[0][:200], 'explanation':comment.get('body','')[:6000]}
    return dict(repository=repo['repo_name'],server_id=repo['server_id'],head=head if reliable else None,
                observed_head=pr['head']['sha'],path=path,source_comment=comment['id'],
                source_hash=digest(comment.get('body','')),source_url=comment.get('html_url',''),
                original=original,context=context,version_reliable=reliable,
                symbols=symbol_keys(original['explanation']+' '+context.get('code','')),
                provenance={'source_author':comment.get('user',{}).get('id'),'review_id':review.get('id') if review else None})


def apply_command(store, client, repo, number, comment, sender):
    body=comment.get('body','').strip()
    if len(body)>6000:raise CommandError('反馈内容超过6000字，未用于数据集')
    parts=body.split(maxsplit=4)
    if not parts or parts[0]!='/ai-review':return None
    if not can_write(client,sender):raise CommandError('反馈人没有目标仓库代码写入权限，未用于数据集')
    if len(parts)>=4 and parts[1]=='反馈' and parts[3] in LABELS:
        target,label=parts[2],LABELS[parts[3]];reason=parts[4].strip() if len(parts)>4 else ''
        if target.startswith('DS-'):
            with contextlib.closing(store.connect()) as c:
                previous=c.execute('SELECT * FROM review_examples WHERE id=? AND repository=? AND number=?',(target,repo['id'],number)).fetchone()
            if not previous or label!='withdrawn' or previous['author']!=sender['id']:raise CommandError('样本编号只用于撤销；其他反馈请引用意见编号')
            dataset.withdraw(store,repo['id'],previous['origin_comment'],'开发者明确撤销')
            return json.loads(previous['value']) | {'status':'withdrawn'}
        with contextlib.closing(store.connect()) as c:
            row=c.execute('SELECT value FROM review_finding_archive WHERE id=? AND repository=? AND number=?',(target,repo['id'],number)).fetchone()
        if not row:raise CommandError('未找到当前PR中已发布且已归档的意见编号')
        original=json.loads(row[0]);finding=original['original']
        base={k:v for k,v in original.items() if k not in ('id','pr','repository_id')}
        base.update(label=label,target=target,correction=reason,conditions=reason,
                    status='withdrawn' if label=='withdrawn' else 'active' if label=='valid' or (len(reason)>=8 and (label!='exclude_by_policy' or '条件=' in reason)) else 'pending',
                    path=finding.get('path',''),symbols=sorted(set(symbol_keys(finding.get('quote','')+' '+finding.get('scope',''))) | dataset.scope_symbols(finding)),
                    confirmed_by=sender['id'],confirmed_login=sender['login'],confirmation_url=comment.get('html_url',''),confirmation_hash=digest(body),
                    sample_type='ai_feedback',source_comment=comment['id'])
        if label=='withdrawn':
            # 撤销同一作者针对该意见的旧标注，不能把他人的反对意见清掉。
            with contextlib.closing(store.connect()) as c:
                old=c.execute('SELECT origin_comment FROM review_examples WHERE repository=? AND target=? AND author=?',(repo['id'],target,sender['id'])).fetchall()
            for r in old:dataset.withdraw(store,repo['id'],r[0],'标注人撤销')
        else:
            # 同一人再次标注是修订，不制造多数票。
            with contextlib.closing(store.connect()) as c:
                old=c.execute('SELECT origin_comment FROM review_examples WHERE repository=? AND target=? AND author=? AND origin_comment!=?',(repo['id'],target,sender['id'],comment['id'])).fetchall()
            for r in old:dataset.withdraw(store,repo['id'],r[0],'已由标注人的后续反馈替代')
        return dataset.put(store,repo['id'],number,comment['id'],base,sender['id'])
    if len(parts)>=4 and parts[1]=='收录':
        url=urlsplit(parts[2]);repository=client.repository()
        roots=[urlsplit(v) for v in [repository['html_url'],*repo.get('feedback_origins',[])]]
        if (url.scheme not in ('http','https') or url.username or url.query or
                not any(url.scheme==root.scheme and url.netloc==root.netloc and
                        url.path in (root.path+f'/pulls/{number}',root.path+f'/pulls/{number}/files') for root in roots) or
                not re.fullmatch(r'issuecomment-[1-9][0-9]*',url.fragment)):
            raise CommandError('只能收录当前仓库当前PR的评论链接')
        source_id=int(url.fragment.split('-')[1]);source,review=comment_at(client,number,source_id)
        if not source or source.get('user',{}).get('id')==repo['bot_id'] or source_id==comment['id']:
            raise CommandError('人工来源评论不存在或不是人工Review')
        reason=body.split(maxsplit=3)[3]
        base=source_value(client,repo,number,source,review)
        base['source_url']=parts[2]
        with contextlib.closing(store.connect()) as c:
            old=c.execute('SELECT value FROM review_examples WHERE repository=? AND origin_comment=?',(repo['id'],comment['id'])).fetchone()
        if old:
            prior=json.loads(old[0])
            if prior.get('confirmation_hash')==digest(body) and prior.get('source_hash')!=base['source_hash']:
                raise CommandError('原评论已修改，请编辑收录命令重新说明并确认')
        # 普通评论允许由开发者明确补充固定SHA和文件，不把当前版本冒充历史版本。
        supplied=re.search(r'版本=([0-9a-f]{40})\s+文件=("[^"\n]+"|[^\s]+)',reason)
        if not base['version_reliable'] and supplied:
            head,path=supplied.groups();path=path.strip(chr(34));commits=client.pages(client.prefix+f'/pulls/{number}/commits',max_pages=20)
            if head not in {v['sha'] for v in commits}:raise CommandError('补充的版本不属于此PR提交集合')
            code=client.file(path,head).decode('utf-8')
            if len(code)>12000:raise CommandError('补充上下文超过12000字，请缩小文件范围')
            base.update(head=head,path=path,version_reliable=True,context={'version':head,'path':path,'code':code})
            base['symbols']=symbol_keys(code)
        base.update(label='confirmed_defect',sample_type='human_review',target='comment:'+str(source_id),
                    correction=reason,conditions=reason,status='active' if base['version_reliable'] and len(reason)>=8 else 'pending',
                    confirmed_by=sender['id'],confirmed_login=sender['login'],confirmation_url=comment.get('html_url',''),confirmation_hash=digest(body),
                    repair={'status':'人工确认，尚无修复验证'})
        repair=re.search(r'修复=([0-9a-f]{40})',reason)
        if repair:
            sha=repair.group(1);commit=client.request('GET',client.prefix+'/git/commits/'+sha)
            patch=client.request('GET',client.prefix+'/git/commits/'+sha+'.diff',raw=True,limit=64000)
            base['repair']={'status':'已关联修复，未自动认定测试通过','sha':commit['sha'],'diff':patch.decode('utf-8')}
        return dataset.put(store,repo['id'],number,comment['id'],base,sender['id'])
    raise CommandError('命令格式无效，请使用反馈或收录命令')


def acknowledge(store, client, repo, number, comment, text):
    marker='<!-- ai-review:feedback:'+str(comment)+' -->'
    body=marker+'\n'+text
    existing=[c for c in client.pages(client.prefix+f'/issues/{number}/comments',max_pages=20)
              if c.get('user',{}).get('id')==repo['bot_id'] and c.get('body','').startswith(marker)]
    if len(existing)>1:raise ReviewError('反馈回执不唯一，需要核对')
    if existing:
        if existing[0]['body']!=body:client.request('PATCH',client.prefix+'/issues/comments/'+str(existing[0]['id']),{'body':body})
        return
    with store.tx() as c:
        record=c.execute('SELECT attempted FROM review_feedback_receipts WHERE repository=? AND comment=?',(repo['id'],comment)).fetchone()
        if record and record[0]:raise ReviewError('反馈回执创建结果待核对，未重复发送')
        c.execute('INSERT OR REPLACE INTO review_feedback_receipts(repository,comment,attempted) VALUES (?,?,1)',(repo['id'],comment))
    posted=client.request('POST',client.prefix+f'/issues/{number}/comments',{'body':body})
    with store.tx() as c:c.execute('UPDATE review_feedback_receipts SET ack_id=? WHERE repository=? AND comment=?',(posted['id'],repo['id'],comment))


def process_event(store, repo, event, admin, bot):
    number=event['number'];cid=event['comment']
    if not cid:
        scan_reviews(store,repo,number,admin)
        return
    admin.pr(number)  # 普通Issue不能成为审查数据集来源。
    source,review=comment_at(admin,number,cid)
    if not source:
        dataset.withdraw(store,repo['id'],cid,'来源或确认评论已删除')
        return
    if source.get('user',{}).get('id')==repo['bot_id']:return
    body=source.get('body','')
    if event['action'] == 'deleted':
        return  # 已删除事件不能作用到随后出现的来源。
    if event.get('body_hash') != digest(body) or event.get('updated_at') != source.get('updated_at'):
        return  # 迟到事件不能借旧编辑者身份确认新内容。
    if body.strip().startswith('/ai-review'):
        try:
            sample=apply_command(store,admin,repo,number,source,event['sender'])
            state={'active':'已用于本仓库后续相关审查','pending':'已保存，需补充原因或固定版本后生效','withdrawn':'已撤销','disabled':'已由管理员停用'}
            if sample['status']=='active':
                with contextlib.closing(store.connect()) as c:
                    if sample.get('target') in dataset.conflict_targets(c,repo['id']):
                        state['active']='已保存；存在相互矛盾标注，暂停提供给模型'
            message='审查数据集样本 `'+sample['id']+'`：'+state.get(sample['status'],sample['status'])+'。\n\n普通评论收录如缺版本，可补充 `版本=<完整SHA> 文件=<仓库路径>`；可选 `修复=<提交SHA>`。'
        except CommandError as exc:
            dataset.withdraw(store,repo['id'],cid,'确认命令失效或权限核验未通过')
            message=str(exc)
        acknowledge(store,bot,repo,number,cid,message)
    else:
        # 原文改变会使已有确认失效，需要开发者重新收录。
        with contextlib.closing(store.connect()) as c:
            used=[json.loads(r[0]) for r in c.execute("SELECT value FROM review_examples WHERE repository=? AND status='active'",(repo['id'],))]
        if any(v.get('source_comment')==cid and v.get('source_hash')!=digest(body) for v in used):
            dataset.withdraw(store,repo['id'],cid,'人工原评论已修改，需重新确认')
        base=source_value(admin,repo,number,source,review)
        base.update(status='candidate',label='unconfirmed',sample_type='human_candidate',target='comment:'+str(cid),correction='',conditions='')
        dataset.put(store,repo['id'],number,cid,base,source.get('user',{}).get('id',0))


def scan_reviews(store,repo,number,client):
    """正式Review事件没有评论ID；回读其集合，只采集启用以后的人工意见。"""
    for review in client.pages(client.prefix+f'/pulls/{number}/reviews',max_pages=10):
        if review.get('state') not in ('COMMENT','APPROVED','REQUEST_CHANGES') or review.get('user',{}).get('id')==repo['bot_id']:
            continue
        for comment in client.pages(client.prefix+f'/pulls/{number}/reviews/{review["id"]}/comments',max_pages=10):
            created=datetime.fromisoformat(comment['created_at'].replace('Z','+00:00')).timestamp()
            if created < repo.get('feedback_since',time.time()) or comment.get('user',{}).get('id')==repo['bot_id']:
                continue
            if comment.get('body','').strip().startswith('/ai-review'):
                continue  # 只有带实际编辑者身份的评论事件可以执行确认命令。
            base=source_value(client,repo,number,comment,review)
            base.update(status='candidate',label='unconfirmed',sample_type='human_candidate',target='comment:'+str(comment['id']),correction='',conditions='')
            dataset.put(store,repo['id'],number,comment['id'],base,comment.get('user',{}).get('id',0))


def run_one(store, factory=manager_client):
    with contextlib.closing(store.connect()) as c:
        row=c.execute("SELECT * FROM review_feedback_events WHERE status='pending' AND due<=? ORDER BY due LIMIT 1",(time.time(),)).fetchone()
    if not row:return False
    repo=store.repo(row['repository']);admin=bot=None
    from .authorization import authorize
    try:
        authorize(repo)
        if not repo['enabled'] or not store.config()['enabled']:
            raise ReviewError('仓库已关闭，反馈等待重新启用后核对')
        admin=factory(repo)
        admin.access_authorize = lambda action: authorize(repo, action=action)
        admin.repository()
        bot=Gitea(repo['gitea_url'],store.unseal(repo['bot_token']),repo['repo_name'],repo['repo_id'])
        bot.access_authorize = lambda action: authorize(repo, action=action)
        process_event(store,repo,json.loads(row['value']),admin,bot)
        with store.tx() as c:c.execute("UPDATE review_feedback_events SET status='done',error='' WHERE repository=? AND delivery=?",(repo['id'],row['delivery']))
    except Exception as exc:
        message=str(exc)[:500] if isinstance(exc,ReviewError) else '反馈处理异常：'+type(exc).__name__
        with store.tx() as c:c.execute('UPDATE review_feedback_events SET due=?,attempts=attempts+1,error=? WHERE repository=? AND delivery=?',
                                      (time.time()+min(300,5*2**min(row['attempts'],6)),message,repo['id'],row['delivery']))
    finally:
        if admin:admin.close()
        if bot:bot.close()
    return True


def audit_one(store, factory=manager_client):
    """补偿丢失的编辑/删除通知；权限或来源超过五分钟未复核的样本自动停止参考。"""
    if not store.config()['enabled']:return
    with contextlib.closing(store.connect()) as c:
        row=c.execute("""SELECT e.* FROM review_examples e JOIN repositories r ON r.id=e.repository
            LEFT JOIN review_example_validation v ON e.id=v.id WHERE e.status='active' AND r.enabled=1
            AND (v.checked IS NULL OR v.checked<?) ORDER BY v.checked LIMIT 1""",(time.time()-30,)).fetchone()
    if not row:return
    value=json.loads(row['value']);repo=store.repo(row['repository']);client=None
    from .authorization import authorize
    try:
        authorize(repo)
        client=factory(repo);client.repository()
        permission=can_write(client,{'id':value['confirmed_by'],'login':value['confirmed_login']})
        confirmation,_=comment_at(client,row['number'],row['origin_comment'])
        source,_=comment_at(client,row['number'],value['source_comment'])
        if (not permission or not confirmation or digest(confirmation.get('body','').strip())!=value['confirmation_hash']
                or not source or (value.get('source_hash') and digest(source.get('body',''))!=value['source_hash'])):
            dataset.withdraw(store,repo['id'],row['origin_comment'],'来源、确认或写权限已变化，需要重新确认')
        else:
            with store.tx() as c:c.execute('INSERT OR REPLACE INTO review_example_validation VALUES (?,?,?)',(row['id'],time.time()+300,time.time()))
    finally:
        # 失败的来源不续有效期，降低其重试优先级，避免饿死其他样本。
        with store.tx() as c:c.execute('UPDATE review_example_validation SET checked=? WHERE id=?',(time.time(),row['id']))
        if client:client.close()


def main():
    logging.basicConfig(level=logging.INFO);store=Store();store.validate_storage();stop=threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:stop.set());signal.signal(signal.SIGINT,lambda *_:stop.set())
    with store.lock('人工反馈同步进程'):
        while not stop.is_set():
            try:
                store.health('feedback','人工反馈同步进程运行中')
                from services.upgrade_maintenance import active
                if active():
                    stop.wait(1)
                    continue
                dataset.archive_pending(store)
                audit_one(store)
                if not run_one(store):stop.wait(1)
            except Exception as exc:
                logging.warning('人工反馈同步异常：%s',type(exc).__name__);stop.wait(2)


if __name__=='__main__':main()

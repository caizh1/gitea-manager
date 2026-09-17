"""展示快照合并读取：跨工作进程短租约、两秒复用、权限逐请求核验；门禁不调用本模块。"""
from contextlib import suppress
import json
import secrets
import time

from sqlalchemy.dialects.sqlite import insert

from models import db, ApprovalViewCache
from .rules import ApprovalError, digest, encode

TTL=2.0
WAIT=9.0
MAX_ROWS=4096
MAX_PAYLOAD=65536


def context_hash(policy):
    return digest([policy.id,policy.server_id,policy.owner_id,policy.repo_id,policy.repo_name,policy.revision,policy.mode,
                   policy.rule_json,policy.preview_json,policy.grandfather_json,policy.hook_token_hash])


def display_status(policy,number,compute,generation=None,deadline=None):
    """调用者必须先完成当前 Gitea 会话与 PR 权限核验；返回值绝不包含 viewer_id。"""
    identity=(policy.id,number)
    policy_context=context_hash(policy)
    expected=digest([policy_context,generation]) if generation is not None else policy_context
    deadline=min(time.monotonic()+WAIT,deadline) if deadline is not None else time.monotonic()+WAIT
    def require_budget():
        if time.monotonic()>=deadline:
            raise ApprovalError('展示读取超出本次请求预算，未沿用旧结果')
    token=secrets.token_hex(32)
    while time.monotonic()<deadline:
        db.session.refresh(policy)
        if context_hash(policy)!=policy_context:raise ApprovalError('等待展示结果期间接入配置变化')
        now=time.time()
        row=db.session.get(ApprovalViewCache,identity,populate_existing=True)
        if row and row.context_hash==expected and row.expires_at>now:
            payload=json.loads(row.payload)
            db.session.rollback()
            if 'error' in payload:raise ApprovalError(payload['error'])
            value=payload['value']
            if value.get('valid_until',0)<=now:raise ApprovalError('展示快照已失效')
            require_budget()
            return value
        # 代际变化仍须等待活跃计算；不同摘要不能抢占有效租约。
        if row and row.lease_until>now:
            db.session.rollback()  # 等待期间不持有数据库事务或写锁。
            time.sleep(.025)
            continue
        # 写入会先取得 SQLite 锁；清理后检查容量与领取租约处在同一事务中。
        expired=db.select(ApprovalViewCache.policy_id,ApprovalViewCache.number).where(
            ApprovalViewCache.expires_at<now-60,ApprovalViewCache.lease_until<now).limit(100)
        from sqlalchemy import tuple_
        db.session.execute(db.delete(ApprovalViewCache).where(
            tuple_(ApprovalViewCache.policy_id,ApprovalViewCache.number).in_(expired)))
        existing=db.session.get(ApprovalViewCache,identity,populate_existing=True)
        if existing is None and db.session.query(ApprovalViewCache).count()>=MAX_ROWS:
            db.session.rollback()
            raise ApprovalError('展示快照达到容量上限，请稍后重新读取')
        require_budget()
        lease_until=time.time()+(deadline-time.monotonic())
        statement=insert(ApprovalViewCache).values(policy_id=identity[0],number=number,context_hash=expected,
            payload='{}',expires_at=0,lease=token,lease_until=lease_until)
        statement=statement.on_conflict_do_update(index_elements=['policy_id','number'],
            set_={'context_hash':expected,'payload':'{}','expires_at':0,'lease':token,'lease_until':lease_until},
            where=(ApprovalViewCache.lease_until<=now)&
                  ((ApprovalViewCache.expires_at<=now)|(ApprovalViewCache.context_hash!=expected)))
        claimed=db.session.execute(statement).rowcount==1
        db.session.commit()
        if not claimed:continue
        try:
            require_budget()
            value=compute()
            require_budget()
            if context_hash(policy)!=policy_context:raise ApprovalError('展示计算期间接入配置变化')
            if 'viewer_id' in value:raise ApprovalError('共享展示快照不能包含浏览器身份')
            payload={'value':value}
        except ApprovalError as exc:
            payload={'error':str(exc)[:500]}
        except Exception:
            # 程序失败不能留下可复用的通过结果；租约有独立过期时间。
            with suppress(Exception):
                db.session.rollback()
                db.session.query(ApprovalViewCache).filter_by(policy_id=identity[0],number=number,lease=token).delete()
                db.session.commit()
            raise
        serialized=encode(payload)
        if len(serialized.encode())>MAX_PAYLOAD:
            payload={'error':'展示结果超出已验证大小上限'};serialized=encode(payload)
        now=time.time()
        # 提交可能等待数据库锁；到达原请求期限后，已写入的快照也必须不可复用。
        expiry=min(now+TTL,payload.get('value',{}).get('valid_until',now+TTL),
                   now+max(0,deadline-time.monotonic()))
        # 领取成功不重置等待预算；超时结果不能进入共享缓存。
        if time.monotonic()>=deadline:
            db.session.rollback()
            db.session.query(ApprovalViewCache).filter_by(policy_id=identity[0],number=number,lease=token).delete()
            db.session.commit()
            require_budget()
        published=db.session.query(ApprovalViewCache).filter_by(policy_id=identity[0],number=number,
            context_hash=expected,lease=token).filter(ApprovalViewCache.lease_until>time.time()).update(
                {'payload':serialized,'expires_at':expiry,'lease':'','lease_until':0})
        db.session.commit()
        if not published:raise ApprovalError('展示读取租约已失效，未发布旧结果')
        require_budget()
        if 'error' in payload:raise ApprovalError(payload['error'])
        return payload['value']
    raise ApprovalError('展示读取排队超时，未沿用过期结果')

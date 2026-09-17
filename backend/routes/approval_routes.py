"""管理接口、受信 Hook 校验和原生会话只读 PR 展示接口。"""
from contextlib import closing
from datetime import datetime
import hashlib
import hmac
from html.parser import HTMLParser
import json
import re
import secrets
import time
from urllib.parse import urlsplit, quote

from flask import Blueprint, jsonify, request, session, send_file
from flask_login import current_user
from sqlalchemy.exc import IntegrityError

from models import db, GiteaServer, ApprovalPolicy, ApprovalNonce, ApprovalAudit
from approval.rules import ApprovalError, normalize, positive, encode, digest
from approval.gitea import Reader
from approval.native_uninstall import build_native_uninstall_bundle
from approval.display_cache import display_status,context_hash
from approval.service import serialize, inspect_policy, status, audit, prepare_bundle, verify_isolation, RELEASE_BLOCKERS

approval_bp = Blueprint('approval', __name__)
approval_internal_bp = Blueprint('approval_internal', __name__)
approval_view_bp = Blueprint('approval_view', __name__)


def body():
    if request.content_length is None or request.content_length > 32768:
        raise ApprovalError('请求缺少长度或超过 32 KB')
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ApprovalError('需要 JSON 对象')
    return value


def error(exc):
    db.session.rollback()
    return jsonify(error=str(exc), allow=False), 409


for blueprint in (approval_bp, approval_internal_bp, approval_view_bp):
    blueprint.register_error_handler(ApprovalError, error)

    @blueprint.after_request
    def no_cache(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response


@approval_bp.before_request
def protect():
    if not current_user.is_authenticated:
        return jsonify(error='请先登录'), 401
    origin = request.headers.get('Origin')
    if (origin and urlsplit(origin).netloc != request.host) or request.headers.get('Sec-Fetch-Site') == 'cross-site':
        return jsonify(error='禁止跨站访问'), 403
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        expected = session.get('approval_csrf', '')
        if not expected or not hmac.compare_digest(expected, request.headers.get('X-Approval-CSRF', '')):
            return jsonify(error='安全令牌失效，请刷新'), 403


@approval_bp.get('/csrf')
def csrf():
    session.setdefault('approval_csrf', secrets.token_urlsafe(32))
    return jsonify(token=session['approval_csrf'])


@approval_bp.get('/policies')
def policies():
    return jsonify(items=[serialize(p) for p in ApprovalPolicy.query.order_by(ApprovalPolicy.id.desc()).limit(500)],
                   release_allowed=False, blockers=RELEASE_BLOCKERS)


@approval_bp.get('/native-governance')
def native_governance():
    """给新原生实例提供直达入口，不要求先创建 Manager 旧规则。"""
    items = []
    deadline = time.monotonic() + 3
    for server in GiteaServer.query.order_by(GiteaServer.id).all():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            items.append({'server_id':server.id, 'server_name':server.name, 'status':'not_checked'})
            continue
        try:
            with closing(Reader(server, min(.75, remaining))) as client:
                capabilities = client.governance_capabilities()
        except ApprovalError as exc:
            items.append({'server_id':server.id, 'server_name':server.name, 'status':'unavailable', 'error':str(exc)})
            continue
        if not capabilities:
            items.append({'server_id':server.id, 'server_name':server.name, 'status':'legacy'})
            continue
        base = server.gitea_url.rstrip('/')
        items.append({'server_id':server.id, 'server_name':server.name, 'status':'native', 'capabilities':capabilities,
                      'groups_url':base + '/governance/groups', 'audit_url':base + '/governance/audit'})
    return jsonify(items=items)


@approval_bp.get('/native-governance/<int:server_id>')
def native_governance_server(server_id):
    """单独探测一个实例，避免列表总预算让后续实例长期停留在未探测状态。"""
    server = db.get_or_404(GiteaServer, server_id)
    try:
        with closing(Reader(server, 3)) as client:
            capabilities = client.governance_capabilities()
    except ApprovalError as exc:
        return jsonify(server_id=server.id, server_name=server.name, status='unavailable', error=str(exc))
    if not capabilities:
        return jsonify(server_id=server.id, server_name=server.name, status='legacy')
    base = server.gitea_url.rstrip('/')
    return jsonify(server_id=server.id, server_name=server.name, status='native', capabilities=capabilities,
                   groups_url=base + '/governance/groups', audit_url=base + '/governance/audit')


@approval_bp.post('/policies')
def create_policy():
    data = body()
    rule = normalize(data.get('rule'))
    server = db.get_or_404(GiteaServer, positive(data.get('server_id'), '实例 ID'))
    repo_id = positive(data.get('repo_id'), '仓库 ID')
    with closing(Reader(server)) as client:
        repo = client.request('/repositories/' + str(repo_id))
    if repo.get('id') != repo_id or not repo.get('permissions', {}).get('admin'):
        raise ApprovalError('仓库身份或管理权限不符')
    policy = ApprovalPolicy(server_id=server.id, repo_id=repo_id, repo_name=repo['full_name'],
                            owner_id=repo['owner']['id'], rule_json=encode(rule))
    try:
        db.session.add(policy)
        db.session.flush()
        audit(policy, 'policy.create', {'rule': rule})
        db.session.commit()
    except IntegrityError:
        raise ApprovalError('该仓库已有审批配置，请编辑现有配置') from None
    return jsonify(serialize(policy)), 201


@approval_bp.put('/policies/<int:pid>')
def update_policy(pid):
    data = body()
    policy = db.get_or_404(ApprovalPolicy, pid)
    if policy.mode not in ('draft', 'disabled'):
        raise ApprovalError('已安装或准备中的配置不能直接编辑；请先停用并卸载门禁，保留原安装凭据用于回退')
    rule = normalize(data.get('rule'))
    revision = positive(data.get('revision'), '规则版本')
    changed = ApprovalPolicy.query.filter_by(id=pid, revision=revision, mode=policy.mode).update(
        {'rule_json': encode(rule), 'revision': revision+1, 'preview_json': '{}', 'updated_at': datetime.utcnow()})
    if not changed:
        raise ApprovalError('规则已被并发修改，请刷新后编辑')
    audit(policy, 'policy.update', {'from_revision': revision, 'rule': rule})
    db.session.commit()
    db.session.refresh(policy)
    return jsonify(serialize(policy))


@approval_bp.post('/policies/<int:pid>/preview')
def preview(pid):
    policy = db.get_or_404(ApprovalPolicy, pid)
    if policy.mode.startswith('prepared_') or policy.mode == 'disable_pending':
        raise ApprovalError('请先完成当前安装或停用操作，不能改写其预检依据')
    revision = policy.revision
    mode = policy.mode
    value = inspect_policy(policy)
    if not ApprovalPolicy.query.filter_by(id=pid, revision=revision, mode=mode).update({'preview_json': encode(value)}):
        raise ApprovalError('预检期间规则发生变化')
    audit(policy, 'preview.complete', {'revision': revision, 'fingerprint': value['fingerprint']})
    db.session.commit()
    return jsonify(value)


@approval_bp.delete('/policies/<int:pid>')
def delete_policy(pid):
    policy = db.get_or_404(ApprovalPolicy, pid)
    if policy.mode not in ('draft', 'disabled'):
        raise ApprovalError('必须先停用并完成门禁卸载，才能删除配置')
    audit(policy, 'policy.delete', {'repo_name': policy.repo_name, 'revision': policy.revision})
    ApprovalNonce.query.filter_by(policy_id=pid).delete()
    db.session.delete(policy)
    db.session.commit()
    return jsonify(ok=True)


@approval_bp.post('/policies/<int:pid>/bundle')
def bundle(pid):
    policy = db.get_or_404(ApprovalPolicy, pid)
    stream = prepare_bundle(policy, body())
    return send_file(stream, as_attachment=True, download_name=f'审批组件-{pid}.zip', mimetype='application/zip')


@approval_bp.post('/policies/<int:pid>/enable')
def enable(pid):
    # 不提供来自浏览器的 release_override/test=true 等旁路。
    db.get_or_404(ApprovalPolicy, pid)
    raise ApprovalError('正式强制启用被阻断：' + '；'.join(RELEASE_BLOCKERS))


@approval_bp.post('/policies/<int:pid>/disable')
def disable(pid):
    policy = db.get_or_404(ApprovalPolicy, pid)
    data = body()
    if data.get('revision') != policy.revision:
        raise ApprovalError('配置已变化，请刷新后停用')
    reason = data.get('reason')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise ApprovalError('请记录停用原因')
    if policy.mode == 'isolated_enforced':
        verify_isolation(policy)
    if ApprovalPolicy.query.filter_by(id=pid, revision=policy.revision, mode=policy.mode,
            hook_token_hash=policy.hook_token_hash).update({'mode': 'disable_pending'}) != 1:
        raise ApprovalError('停用期间状态已变化，请重新核对')
    audit(policy, 'policy.disable', {'reason': reason})
    db.session.commit()
    return jsonify(serialize(policy))


@approval_bp.get('/policies/<int:pid>/status/<int:number>')
def admin_status(pid, number):
    return jsonify(status(db.get_or_404(ApprovalPolicy, pid), number))


@approval_bp.get('/policies/<int:pid>/audit')
def audits(pid):
    before = request.args.get('before', type=int)
    query = ApprovalAudit.query.filter_by(policy_id=pid)
    if before:
        query = query.filter(ApprovalAudit.id < before)
    rows = query.order_by(ApprovalAudit.id.desc()).limit(100).all()
    return jsonify(items=[{'id': r.id, 'actor': r.actor, 'action': r.action, 'detail': json.loads(r.detail_json),
                           'created_at': r.created_at.isoformat()+'Z'} for r in rows])


@approval_bp.get('/policies/<int:pid>/native-import')
def native_import_preview(pid):
    policy = db.get_or_404(ApprovalPolicy, pid)
    with closing(Reader(db.get_or_404(GiteaServer, policy.server_id), 12)) as client:
        return jsonify(client.native_rule_plan(policy))


def legacy_hook_pending(policy):
    return bool(policy.mode == 'native_pending_uninstall' or
                (policy.hook_token_hash and policy.mode in ('shadow', 'isolated_enforced', 'prepared_shadow',
                                                            'prepared_isolated_enforced', 'disable_pending')))


@approval_bp.post('/policies/<int:pid>/native-import')
def native_import_apply(pid):
    policy = db.get_or_404(ApprovalPolicy, pid)
    data = body()
    expected = data.get('rules')
    with closing(Reader(db.get_or_404(GiteaServer, policy.server_id), 20)) as client:
        plan = client.native_rule_plan(policy)
        if expected != plan['rules']:
            raise ApprovalError('导入预览已变化，请重新预览')
        rules_path = '/governance/repositories/' + str(policy.repo_id) + '/approval-rules'
        settings_path = '/governance/repositories/' + str(policy.repo_id) + '/approval-settings'
        current = client.governance_read(rules_path)
        existing = current.get('rules', [])
        for rule in plan['rules']:
            same = next((item for item in existing if all(item.get(k) == v for k, v in rule.items())), None)
            if same:
                continue
            try:
                client.write('POST', rules_path, {'revision':0, 'rule':rule})
            except ApprovalError:
                # 超时回执的提交结果未知：只回读，不盲目重放写入。
                recovered = client.governance_read(rules_path)
                if not any(all(item.get(k) == v for k, v in rule.items()) for item in recovered.get('rules', [])):
                    raise
            existing = client.governance_read(rules_path).get('rules', [])
        verified = client.governance_read(rules_path)
        if any(not any(all(item.get(k) == v for k, v in rule.items()) for item in verified.get('rules', [])) for rule in plan['rules']):
            raise ApprovalError('原生规则写入后回读不一致')
        settings = dict(plan['settings'], revision=verified.get('settings_revision', 0))
        effective = (verified.get('settings') or {}).get('settings') or verified.get('settings') or {}
        if any(effective.get(key) != value for key, value in plan['settings'].items()):
            try:
                client.write('PUT', settings_path, settings)
            except ApprovalError:
                recovered = client.governance_read(rules_path)
                recovered_effective = (recovered.get('settings') or {}).get('settings') or recovered.get('settings') or {}
                if any(recovered_effective.get(key) != value for key, value in plan['settings'].items()):
                    raise
        # 历史仓库必须安装引用事务 Hook；处理函数会安装后立即回读。
        try:
            client.write('POST', '/governance/repositories/' + str(policy.repo_id) + '/reference-hook', {})
        except ApprovalError:
            recovered = client.governance_read(rules_path)
            if not recovered.get('reference_enforcement_ready'):
                raise
        final = client.governance_read(rules_path)
        final_settings = (final.get('settings') or {}).get('settings') or final.get('settings') or {}
        if not final.get('reference_enforcement_ready'):
            raise ApprovalError('原生引用强制链路写入后未就绪')
        if any(final_settings.get(key) != value for key, value in plan['settings'].items()):
            raise ApprovalError('原生审批设置写入后回读不一致')
        repo = client.request('/repositories/' + str(policy.repo_id))
        for pull in client.pages(client.path(repo) + '/pulls', {'state':'open'}):
            pull_rules = client.governance_read('/governance/pulls/' + str(pull['id']) + '/approval-rules')
            version_rules = (pull_rules.get('version') or {}).get('rules') or []
            if any(not any(all(item.get(k) == v for k, v in rule.items()) for item in version_rules)
                   for rule in plan['rules']):
                raise ApprovalError('存量 PR 的原生规则版本回读不一致')
            pull_state = client.governance_read('/governance/pulls/' + str(pull['id']) + '/approval-state')
            if not pull_state.get('merge_enforcement_ready'):
                raise ApprovalError('存量 PR 的两端 Hook 或当前规则版本未就绪')
        old_hook_present = legacy_hook_pending(policy)
        next_mode = 'native_pending_uninstall' if old_hook_present else 'native'
        native_state = {'native':True, 'repository_id':policy.repo_id, 'full_path':plan['full_path'],
                        'native_url':plan['native_url'], 'enforcement_ready':True,
                        'rules':len(plan['rules']), 'settings':plan['settings'],
                        'legacy_hook_removal_pending':old_hook_present}
        if ApprovalPolicy.query.filter_by(id=policy.id, revision=policy.revision).update(
                {'mode':next_mode, 'preview_json':encode(native_state), 'updated_at':datetime.utcnow()}) != 1:
            raise ApprovalError('导入期间 Manager 规则已变化，请重新回读')
        audit(policy, 'native.import', {'repository_id':policy.repo_id, 'rules':len(plan['rules']),
                                       'settings':plan['settings'], 'enforcement_ready':True})
        db.session.commit()
        return jsonify(ok=True, native=True, mode=next_mode, native_url=plan['native_url'], full_path=plan['full_path'],
                       repository_id=policy.repo_id, rules=plan['rules'], settings=plan['settings'],
                       enforcement_ready=True, legacy_hook_removal_pending=old_hook_present)


@approval_bp.post('/policies/<int:pid>/native-uninstall-bundle')
def native_uninstall_bundle(pid):
    policy = db.get_or_404(ApprovalPolicy, pid)
    if policy.mode != 'native_pending_uninstall':
        raise ApprovalError('当前没有等待卸载的旧审批 Hook')
    data = body(); gate_url = data.get('gate_url', '')
    parsed = urlsplit(gate_url)
    if (parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or
            parsed.query or parsed.fragment or parsed.path.rstrip('/') != '/api/approval-internal'):
        raise ApprovalError('请输入包含 /api/approval-internal 的回执地址')
    server = db.get_or_404(GiteaServer, policy.server_id)
    if parsed.scheme != 'https' and not server.is_local:
        raise ApprovalError('非本机实例的卸载回执必须使用 HTTPS')
    token = secrets.token_urlsafe(32)
    old_hash = policy.native_uninstall_token_hash
    new_hash = hashlib.sha256(token.encode()).hexdigest()
    stream = build_native_uninstall_bundle(policy, token, gate_url, server.gitea_url)
    changed = ApprovalPolicy.query.filter_by(id=pid, revision=policy.revision, mode='native_pending_uninstall',
            native_uninstall_token_hash=old_hash).update({'native_uninstall_token_hash':new_hash, 'updated_at':datetime.utcnow()})
    if changed != 1:
        raise ApprovalError('生成卸载包期间状态变化，请刷新')
    audit(policy, 'native.uninstall_bundle', {'repository_id':policy.repo_id,
          'notice':'专用包仅按稳定项目ID删除本组件Hook'})
    db.session.commit()
    return send_file(stream, as_attachment=True, download_name=f'原生迁移卸载-{pid}.zip', mimetype='application/zip')


def trusted(pid, data, allow_native_uninstall=False):
    policy = db.get_or_404(ApprovalPolicy, pid)
    supplied = request.headers.get('Authorization', '')
    supplied_hash = hashlib.sha256(supplied[7:].encode()).hexdigest() if supplied.startswith('Bearer ') else ''
    normal = bool(policy.hook_token_hash and hmac.compare_digest(policy.hook_token_hash, supplied_hash))
    uninstall = bool(allow_native_uninstall and policy.mode in ('native_pending_uninstall', 'native') and
                     policy.native_uninstall_token_hash and hmac.compare_digest(policy.native_uninstall_token_hash, supplied_hash))
    if not normal and not uninstall:
        raise ApprovalError('Hook 身份不可信')
    nonce = data.get('nonce', '')
    stamp = data.get('timestamp')
    if not isinstance(nonce, str) or not re.fullmatch('[0-9a-f]{32}', nonce) or type(stamp) is not int or abs(time.time()-stamp) > 30:
        raise ApprovalError('Hook 请求过期或随机标识无效')
    if data.get('repo_id') != policy.repo_id or data.get('revision') != policy.revision:
        raise ApprovalError('Hook 仓库或安装版本不匹配')
    try:
        ApprovalNonce.query.filter(ApprovalNonce.expires_at < time.time()).delete()
        db.session.add(ApprovalNonce(policy_id=pid, nonce=nonce, expires_at=time.time()+60))
        db.session.commit()
    except IntegrityError:
        raise ApprovalError('拒绝重放 Hook 请求') from None
    return policy


@approval_internal_bp.post('/<int:pid>/check')
def gate_check(pid):
    data = body()
    policy = trusted(pid, data)
    number = positive(data.get('number'), 'PR 序号')
    positive(data.get('pr_id'), 'PR ID')
    for field in ('old', 'new'):
        if not isinstance(data.get(field), str) or not re.fullmatch('[0-9a-f]{40}', data[field]):
            raise ApprovalError('Git 对象标识无效')
    if policy.mode == 'native_pending_uninstall':
        audit(policy, 'gate.bypassed_after_native', {'number': number, 'new': data['new'],
              'notice': '原生强制链路已回读，旧受管 Hook 等待卸载'})
        db.session.commit()
        return jsonify(allow=True, nonce=data['nonce'], revision=policy.revision,
                       head=data.get('new'), base='', new=data.get('new'), error='')
    if policy.mode != 'isolated_enforced':
        raise ApprovalError('强制门禁尚未激活或已停用，需要核对安装配置')
    value = status(policy, number, data)
    # 已确认存量应由本地 Hook 清单直接豁免；远程收到豁免表示安装清单不一致。
    allowed = bool(value['satisfied'] and not value['grandfathered'] and value['applicable'])
    audit(policy, 'gate.check', {'number': number, 'allowed': allowed, 'head': value['head'], 'new': data['new'],
                                'missing': value['missing'], 'revision': policy.revision})
    db.session.commit()
    return jsonify(allow=allowed, nonce=data['nonce'], revision=policy.revision, head=value['head'],
                   base=value['base'], new=data['new'], error='；'.join(value['missing']))


@approval_internal_bp.post('/<int:pid>/installed')
def installed(pid):
    data = body()
    policy = trusted(pid, data, allow_native_uninstall=data.get('removal') is True)
    if data.get('removal') is True:
        if policy.mode == 'native':
            return jsonify(ok=True, nonce=data['nonce'], already_removed=True)
        if policy.mode not in ('disable_pending', 'disabled', 'native_pending_uninstall'):
            raise ApprovalError('必须先在 Manager 记录停用原因')
        completed_mode = 'native' if policy.mode == 'native_pending_uninstall' else 'disabled'
        if ApprovalPolicy.query.filter_by(id=pid, revision=policy.revision, mode=policy.mode,
                hook_token_hash=policy.hook_token_hash).update({'mode': completed_mode}) != 1:
            raise ApprovalError('卸载回执期间状态变化，未覆盖新配置')
        audit(policy, 'installation.removed', {'notice': '安装器报告已移除受管 Hook'})
        db.session.commit()
        return jsonify(ok=True, nonce=data['nonce'])
    mode = policy.mode.removeprefix('prepared_')
    if policy.mode not in ('prepared_shadow', 'prepared_isolated_enforced', 'shadow', 'isolated_enforced'):
        raise ApprovalError('没有待完成的组件安装')
    if mode == 'isolated_enforced':
        verify_isolation(policy)
    if data.get('runtime_marker') != 'gm-approval-v1' or data.get('hooks_verified') is not True:
        raise ApprovalError('磁盘与运行时安装证据不完整')
    previous = policy.mode
    if ApprovalPolicy.query.filter_by(id=pid, revision=policy.revision, mode=previous,
             hook_token_hash=policy.hook_token_hash).update({'mode': mode}) != 1:
        raise ApprovalError('回执期间接入状态变化，未覆盖新状态')
    audit(policy, 'installation.reported', {'mode': mode, 'runtime_marker': data['runtime_marker'],
                                           'notice': '安装器回执；真实 PR 页面和持续业务另行验收'})
    db.session.commit()
    return jsonify(ok=True, nonce=data['nonce'])


@approval_internal_bp.post('/<int:pid>/uninstall-ready')
def uninstall_ready(pid):
    data = body()
    policy = trusted(pid, data, allow_native_uninstall=True)
    if policy.mode not in ('disable_pending', 'disabled', 'native_pending_uninstall', 'native'):
        raise ApprovalError('必须先在 Manager 记录停用原因，尚未移除门禁')
    return jsonify(ok=True, nonce=data['nonce'])


class ContextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.contexts = []
        self.viewers = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'template' and attrs.get('id') == 'gm-approval-context':
            self.contexts.append(attrs)
        if tag == 'meta' and attrs.get('name') == 'gm-approval-viewer':
            self.viewers.append(attrs.get('content',''))


def view_scope(server):
    # 实例连接重配不能复用原地址的展示快照；摘要仅作为内部隔离键，不返回浏览器。
    return digest([server.id,server.gitea_url,server.host,server.ssh_port,server.ssh_user,
        server.gitea_container,server.pg_container,server.pg_dbname,server.pg_user,server.is_local,server.api_token])


@approval_view_bp.get('/<int:sid>/<int:rid>/<int:number>')
def view(sid, rid, number):
    # 从进入应用开始共享预算；原生会话、缓存等待和完整读取不能各自重新计时。
    # 不包含 Gunicorn 接入队列，也不把浏览器提供的时间戳作为授权依据。
    request_deadline=time.monotonic()+10
    policy = ApprovalPolicy.query.filter_by(server_id=sid, repo_id=rid).first_or_404()
    server = db.get_or_404(GiteaServer, sid)
    scope=view_scope(server)
    origin = request.headers.get('Origin')
    if origin and origin != (urlsplit(server.gitea_url).scheme+'://'+urlsplit(server.gitea_url).netloc):
        return jsonify(error='不允许跨站读取审批状态'), 403
    cookie = request.headers.get('Cookie', '')
    if not cookie or len(cookie) > 8192:
        return jsonify(error='需要现有 Gitea 登录会话'), 401
    native_url=server.gitea_url.rstrip('/')
    repo_name=policy.repo_name
    policy_scope=context_hash(policy)
    # 原生网络等待不占用 Manager 的只读连接；核验结束后仍回读配置并拒绝变化。
    db.session.rollback()
    # REST 不接受网页 Cookie；原生轻量 issue/info 核对 PR 权限，个人设置页确认真实登录身份。
    # 每个请求均重新核验，避免反复渲染完整 PR 时间线造成下游请求放大。
    import requests
    with requests.Session() as http:
        http.trust_env = False
        path = '/' + '/'.join(quote(p, safe='') for p in repo_name.split('/')) + '/pulls/' + str(number)
        deadline=min(request_deadline,time.monotonic()+4)
        def native(path):
            remaining=deadline-time.monotonic()
            if remaining<=0:raise ApprovalError('原生会话核验超时')
            with http.get(native_url+path,headers={'Cookie':cookie},
                          timeout=(min(2,remaining),min(3,remaining)),allow_redirects=False,stream=True) as response:
                if response.status_code!=200:raise ApprovalError('Gitea 登录失效或无权读取该 PR')
                raw=bytearray()
                for chunk in response.iter_content(32768):
                    raw.extend(chunk)
                    if len(raw)>2*1024*1024 or time.monotonic()>deadline:
                        raise ApprovalError('原生会话响应超过读取预算')
                return bytes(raw)
        try:
            info=json.loads(native(path+'/info'))['convertedIssue']
            raw=native('/user/settings')
        except (requests.RequestException,ValueError,KeyError,TypeError):
            raise ApprovalError('无法确认当前 Gitea 会话') from None
    if context_hash(policy)!=policy_scope:
        raise ApprovalError('会话核验期间接入配置变化，未沿用旧权限对应的规则')
    if (not isinstance(info,dict) or (info.get('repository') or {}).get('id')!=rid
            or info.get('number')!=number or not isinstance(info.get('pull_request'),dict)):
        return jsonify(error='原生 PR 权限与身份核验失败'),403
    parser = ContextParser()
    parser.feed(raw.decode('utf-8'))
    if len(parser.viewers)!=1 or not parser.viewers[0].isdigit() or int(parser.viewers[0])<=0:
        return jsonify(error='原生页面身份核验失败'), 403
    if info.get('state') not in ('open','closed') or type(info['pull_request'].get('merged')) is not bool:
        raise ApprovalError('原生 PR 状态不完整')
    generation=[scope,positive(info.get('id'),'原生问题 ID'),info.get('updated_at'),info['state'],info['pull_request']['merged']]
    # 原生更新时间和关闭状态变化直接失效；其余展示快照最多复用两秒且保留原核对时间。
    # 完整计算仍核验代码，浏览器还要匹配 PR 页的 head；合并门禁完全绕开展示快照。
    def verify_scope():
        db.session.refresh(server)
        if view_scope(server)!=scope:raise ApprovalError('读取期间实例连接配置变化，未复用其他实例的结果')
    def calculate():
        verify_scope()
        result=status(policy,number,deadline=request_deadline)
        verify_scope()
        return result
    value = display_status(policy,number,calculate,generation=generation,deadline=request_deadline)
    verify_scope()
    if value.get('closed')!=(info['state']!='open' or info['pull_request']['merged']):
        raise ApprovalError('当前 PR 状态与展示快照不同，请重新读取')
    if time.monotonic()>=request_deadline:
        raise ApprovalError('展示核对超出本次请求预算，未返回过期结果')
    value['viewer_id'] = int(parser.viewers[0])
    return jsonify(value)

"""显式接口清单与统一浏览器权限入口；遗漏的接口默认拒绝。"""
import hmac
from urllib.parse import urlsplit

from flask import g, jsonify, request, session
from flask_login import current_user

from access.errors import AccessError
from access.scopes import current_binding


PUBLIC = {'auth.check_session', 'auth.login'}
MACHINE = {'ai_review.webhook', 'ai_review.setup_probe', 'approval_internal.gate_check',
           'approval_internal.installed', 'approval_internal.uninstall_ready', 'approval_view.view',
           'integrations.health', 'integrations.events', 'integrations.task_detail', 'access_internal.check'}
PLATFORM = {
    'servers': 'get_server add_server delete_info update_server delete_server check_server refresh_server get_server_detail_route',
    'backups': 'list_backups create_backup delete_backup download_backup get_backup_steps',
    'restore': 'list_restore_tasks get_restore_steps execute_restore get_verification trigger_verify get_backup_commits',
    'schedule': 'list_schedules create_schedule update_schedule delete_schedule run_schedule get_schedule_logs',
    'mirror': 'list_mirrors create_mirror update_mirror delete_mirror setup_mirror sync_mirror mirror_status mirror_audit_logs sync_repo',
    'settings': 'get_settings update_settings',
    'management': 'status public_key fingerprint trust discovery connect reports download_report report_steps',
    'ai_review': 'save_config connection_test progress_assets create_repository install_webhook setup_status setup_submit setup_detail setup_resume raw_diagnostics_download',
    'approval': 'native_governance native_governance_server bundle native_import_preview native_import_apply native_uninstall_bundle',
}
ORGANIZATION = {
    'access_context': 'context',
    'repository_policies': 'csrf schema servers organizations candidate_users candidate_teams repositories repository_rules templates create_template update_template delete_template verify_template_identities preview_batch apply_batch batches batch_detail',
    'commit_gate': 'list_rules create_rule update_rule delete_rule list_repos apply_gate remove_gate_route test_rule',
    'ai_review': 'csrf config servers catalog repositories update_repository tasks dataset_list dataset_disable dataset_export dataset_history task task_events task_events_export task_units task_context_reads rerun reconcile cancel_task attention',
    'approval': 'csrf policies create_policy update_policy preview delete_policy enable disable admin_status audits',
    'statistics': 'overview commit_trend repo_ranking author_ranking author_detail author_repos author_trend refresh_statistics',
    'alert': 'list_alerts alert_summary clear_alert clear_resolved',
    'dashboard': 'recent_activity',
    'servers': 'list_servers',
}


def endpoints(mapping):
    return {f'{blueprint}.{name}' for blueprint, names in mapping.items() for name in names.split()}


def csrf_valid():
    if getattr(g, 'csrf_verified', False):
        return True
    expected = session.get('access_csrf', '')
    supplied = request.headers.get('X-CSRF-Token', '')
    return bool(isinstance(expected, str) and expected and supplied and
                hmac.compare_digest(expected.encode('utf-8'), supplied.encode('utf-8')))


def install(app):
    from access.authentication import csrf_token
    platform = endpoints(PLATFORM)
    organization = endpoints(ORGANIZATION)
    # 由认证模块显式提供清单，不将任意新增 access 路由自动放行。
    from access.admin import PLATFORM_ENDPOINTS, ACCOUNT_ENDPOINTS
    platform.update(PLATFORM_ENDPOINTS)
    account = {'auth.logout', *ACCOUNT_ENDPOINTS}
    registry = PUBLIC | MACHINE | platform | organization | account
    app.extensions['access_route_registry'] = registry

    @app.errorhandler(AccessError)
    def access_error(exc):
        from models import db
        db.session.rollback()
        return jsonify(error=str(exc)), exc.status

    @app.before_request
    def authorize():
        endpoint = request.endpoint
        if endpoint is None:
            return None
        if endpoint not in registry:
            raise AccessError('接口未登记权限，已拒绝访问', 403)
        if endpoint in MACHINE:
            return None  # 这些接口各自验证签名或专用服务令牌。
        origin = request.headers.get('Origin')
        if ((origin and urlsplit(origin).netloc != request.host)
                or request.headers.get('Sec-Fetch-Site') == 'cross-site'):
            raise AccessError('不允许跨站访问管理接口', 403)
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if not csrf_valid():
                raise AccessError('安全令牌无效，请刷新页面', 403)
            g.csrf_verified = True
        if endpoint in PUBLIC:
            return None
        if not current_user.is_authenticated:
            raise AccessError('请先登录', 401)
        if current_user.must_change_password and endpoint not in account:
            raise AccessError('请先修改初始管理员密码', 403)
        if endpoint in account:
            return None
        if endpoint in platform and not current_user.is_platform_admin:
            raise AccessError('此功能仅平台管理员可用', 403)
        if endpoint in organization:
            binding = current_binding()
            if binding:
                from access.organization_api import guard
                return guard(endpoint, binding)
        return None

    @app.after_request
    def protect_cache(response):
        if request.path.startswith('/api/') and request.endpoint not in MACHINE:
            response.headers['Cache-Control'] = 'no-store'
            response.headers['Vary'] = 'Cookie, X-Organization-ID'
        return response

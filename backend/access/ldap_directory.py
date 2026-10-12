"""与 Gitea 的 LDAP 绑定、过滤器、属性和群组校验语义一致。"""
import re
import ssl
import time
import uuid
from contextlib import contextmanager

import ldap3
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

from access.errors import AccessError

DEFAULTS = {
    'name': '公司目录', 'type': 'binddn', 'enabled': False,
    'host': '', 'port': 636, 'security_protocol': 'ldaps', 'skip_verify': False,
    'bind_dn': '', 'bind_password': '', 'user_base': '', 'user_dn': '', 'filter': '',
    'attribute_username': '', 'attribute_name': '', 'attribute_surname': '', 'attribute_mail': '',
    'attributes_in_bind': False, 'groups_enabled': False, 'group_dn': '',
    'group_filter': '', 'group_member_uid': '', 'user_uid': '',
    'paged_search_enabled': False, 'paged_search_size': 500, 'ca_cert': '',
}
BOOL_FIELDS = {key for key, value in DEFAULTS.items() if isinstance(value, bool)}
ATTRIBUTE_FIELDS = {'attribute_username', 'attribute_name', 'attribute_surname', 'attribute_mail',
                    'group_member_uid', 'user_uid'}


def format_template(template, value):
    """仅接受一个 Go 字符串参数及其显式重复引用；拒绝错误格式而非生成畸形查询。"""
    output, index, next_argument = [], 0, 1
    while index < len(template):
        if template[index] != '%':
            output.append(template[index]); index += 1; continue
        if template.startswith('%%', index):
            output.append('%'); index += 2; continue
        if template.startswith('%[1]s', index):
            output.append(value); index += 5; next_argument = 2; continue
        if template.startswith('%s', index) and next_argument == 1:
            output.append(value); index += 2; next_argument = 2; continue
        raise AccessError('过滤器或用户 DN 模板只支持 %s、%[1]s 和 %%；重复参数请使用 %[1]s')
    if next_argument == 1:
        raise AccessError('过滤器或用户 DN 模板必须包含 %s 或 %[1]s')
    return ''.join(output)


def validate_config(payload, existing=None):
    if not isinstance(payload, dict):
        raise AccessError('目录配置必须为对象')
    data = dict(DEFAULTS) | (existing or {})
    aliases = {'group_enabled': 'groups_enabled', 'user_filter': 'filter'}
    for key, value in payload.items():
        key = aliases.get(key, key)
        if key in {'id', 'bind_password_set', 'version', 'stages'}:
            continue
        if key not in DEFAULTS:
            raise AccessError('目录配置包含不支持的字段')
        if key in BOOL_FIELDS:
            if not isinstance(value, bool):
                raise AccessError('目录开关必须使用布尔值')
        elif key in {'port', 'paged_search_size'}:
            if isinstance(value, bool):
                raise AccessError('目录端口和分页大小必须为整数')
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise AccessError('目录端口和分页大小必须为整数') from None
        elif not isinstance(value, str) or len(value) > (65536 if key == 'ca_cert' else 4096):
            raise AccessError('目录字段格式或长度不正确')
        data[key] = value
    if data['type'] not in {'binddn', 'simple'}:
        raise AccessError('请选择 LDAP via BindDN 或 LDAP simple auth')
    if data['security_protocol'] not in {'unencrypted', 'ldaps', 'starttls'}:
        raise AccessError('目录安全协议不受支持')
    if not 1 <= data['port'] <= 65535 or not 1 <= data['paged_search_size'] <= 10000:
        raise AccessError('端口或分页大小超出范围')
    if not data['host'].strip() or any(char in data['host'] for char in '/@\x00\r\n'):
        raise AccessError('请填写不含协议、路径和凭据的目录主机名')
    if not data['name'].strip() or not data['filter'].strip():
        raise AccessError('认证名称和用户过滤器不能为空')
    if data['type'] == 'binddn' and not data['user_base'].strip():
        raise AccessError('BindDN 模式必须填写用户搜索基准')
    if data['type'] == 'simple' and not data['user_dn'].strip():
        raise AccessError('Simple auth 模式必须填写用户 DN 模板')
    for key in ATTRIBUTE_FIELDS:
        value = data[key]
        if value and not re.fullmatch(r'[A-Za-z][A-Za-z0-9;-]*|[0-9]+(?:\.[0-9]+)+', value):
            raise AccessError('目录属性名称格式不正确')
    if data['groups_enabled']:
        if not all(data[key] for key in ('group_dn', 'group_member_uid', 'user_uid')):
            raise AccessError('群组校验必须填写搜索基准、群组成员属性和用户对应属性')
        if any(char in data['group_filter'] for char in '\x00*\\'):
            raise AccessError('群组过滤器包含 Gitea 不支持的字符')
        if any(char in data['group_dn'] for char in '\x00()*\\\'"#+;<>') or data['group_dn'] != data['group_dn'].strip():
            raise AccessError('群组搜索基准包含 Gitea 不支持的字符')
    format_template(data['filter'], 'manager-validation')
    if data['type'] == 'simple':
        format_template(data['user_dn'], 'manager-validation')
    return data


def _validate_username(username, direct=False):
    if not isinstance(username, str) or not username or len(username) > 200:
        raise AccessError('用户名或密码不正确', 401)
    rejected = '\x00()*\\' + (',=\'"#+;<>' if direct else '')
    if any(char in username for char in rejected):
        raise AccessError('用户名或密码不正确', 401)


@contextmanager
def connection(config):
    conn = None
    try:
        tls = ldap3.Tls(validate=ssl.CERT_NONE if config['skip_verify'] else ssl.CERT_REQUIRED,
                        ca_certs_data=config['ca_cert'] or None, version=ssl.PROTOCOL_TLS_CLIENT)
        server = ldap3.Server(config['host'], port=config['port'],
                             use_ssl=config['security_protocol'] == 'ldaps', tls=tls,
                             connect_timeout=3, get_info=ldap3.NONE)
        conn = ldap3.Connection(server, auto_referrals=False, receive_timeout=5,
                                raise_exceptions=True, check_names=False)
        conn.open()
        if config['security_protocol'] == 'starttls' and not conn.start_tls():
            raise AccessError('目录加密连接失败', 503)
        yield conn
    except AccessError:
        raise
    except (LDAPException, OSError, ValueError, ssl.SSLError):
        raise AccessError('目录连接、查询或认证失败，请核对配置和目录状态', 401) from None
    finally:
        if conn is not None:
            try:
                conn.unbind()
            except (LDAPException, OSError):
                pass


def _bind(conn, dn, password):
    if not password or not conn.rebind(user=dn, password=password, authentication=ldap3.SIMPLE):
        raise AccessError('用户名或密码不正确', 401)


def _query_bind(conn, config):
    # Gitea 在 BindDN 或密码任意一项为空时使用匿名查询。
    if config['bind_dn'] and config['bind_password']:
        _bind(conn, config['bind_dn'], config['bind_password'])
    elif not conn.rebind(authentication=ldap3.ANONYMOUS, user=None, password=None):
        raise AccessError('目录匿名查询不可用', 401)


def _attributes(config):
    return list(dict.fromkeys(['objectGUID', 'entryUUID', 'userAccountControl',
                             'msDS-User-Account-Control-Computed', 'accountExpires'] + [config[key] for key in ATTRIBUTE_FIELDS
                             if config[key] and config[key].lower() != 'dn']))


def _search(conn, base, query, attributes, size_limit=0):
    conn.search(base, query, search_scope=ldap3.SUBTREE, attributes=attributes,
                dereference_aliases=ldap3.DEREF_NEVER, size_limit=size_limit, time_limit=5)
    if conn.result.get('result') != 0:
        raise AccessError('目录查询未完整成功，未使用部分结果', 503)
    return [row for row in conn.response if row.get('type') == 'searchResEntry']


def _values(entry, name, raw=False):
    values = next((value for key, value in entry.get('raw_attributes' if raw else 'attributes', {}).items()
                   if key.lower() == name.lower()), [])
    return values if isinstance(values, (list, tuple)) else [values]


def _value(entry, name):
    values = _values(entry, name) if name else []
    if not values:
        return ''
    value = values[0]
    return value.decode('utf-8', errors='strict') if isinstance(value, bytes) else str(value)


def _identity(entry, config, login_name=''):
    guids = _values(entry, 'objectGUID', raw=True)
    subject = ''
    if guids and isinstance(guids[0], bytes) and len(guids[0]) == 16:
        subject = 'objectGUID:' + str(uuid.UUID(bytes_le=guids[0]))
    if not subject:
        entry_uuid = _value(entry, 'entryUUID')
        if entry_uuid:
            try:
                subject = 'entryUUID:' + str(uuid.UUID(entry_uuid))
            except ValueError:
                raise AccessError('目录稳定身份格式不正确', 401) from None
    if not subject:
        raise AccessError('目录未返回 objectGUID 或 entryUUID，无法安全建立账号', 401)
    username = _value(entry, config['attribute_username']) or login_name
    if not username:
        raise AccessError('目录用户未返回用户名属性，无法建立待授权账号')
    name = ' '.join(value for value in (_value(entry, config['attribute_name']),
                                         _value(entry, config['attribute_surname'])) if value)
    return {'subject': subject, 'username': username, 'display_name': name or username,
            'email': _value(entry, config['attribute_mail']), 'dn': entry['dn']}


def _account_active(entry, identity):
    """AD 的 Bind 会检查禁用/到期；无密码的会话复核必须读取对应确定性状态。"""
    if not identity['subject'].startswith('objectGUID:'):
        return True
    control = _value(entry, 'userAccountControl')
    if not control:
        raise AccessError('目录未返回 AD 账号状态，无法安全复核已有会话', 401)
    try:
        flags = int(control)
        computed = int(_value(entry, 'msDS-User-Account-Control-Computed') or '0')
        expires = int(_value(entry, 'accountExpires') or '0')
    except ValueError:
        raise AccessError('目录 AD 账号状态格式不正确', 401) from None
    if flags & 2 or computed & (16 | 8388608):
        return False
    if expires not in (0, 9223372036854775807) and expires <= int((time.time() + 11644473600) * 10000000):
        return False
    return True


def _group_allowed(conn, config, entry):
    if not config['groups_enabled'] or not config['group_filter']:
        return True
    uid = entry['dn'] if config['user_uid'].lower() == 'dn' else _value(entry, config['user_uid'])
    if not uid:
        return False
    query = '(&(' + config['group_filter'] + ')(' + config['group_member_uid'] + '=' + escape_filter_chars(uid) + '))'
    return bool(_search(conn, config['group_dn'], query, ['1.1']))


def authenticate(config, username, password):
    stages, phase = [], 'configuration'
    try:
        config = validate_config(config)
        direct = config['type'] == 'simple'
        phase = 'authenticate'
        _validate_username(username, direct)
        if not isinstance(password, str) or not password:
            raise AccessError('用户名或密码不正确', 401)
        phase = 'connect'
        with connection(config) as conn:
            stages.append({'stage': 'connect', 'ok': True, 'message': '目录连接成功'})
            query = format_template(config['filter'], username)
            if direct:
                dn = format_template(config['user_dn'], username)
                phase = 'authenticate'
                _bind(conn, dn, password)
                stages.append({'stage': 'authenticate', 'ok': True, 'message': '用户认证成功'})
            else:
                phase = 'bind'
                _query_bind(conn, config)
                stages.append({'stage': 'bind', 'ok': True, 'message': '查询身份绑定成功'})
            phase = 'search'
            if not direct or config['user_base']:
                rows = _search(conn, config['user_base'], query, ['1.1'], size_limit=2)
                if len(rows) != 1 or not rows[0].get('dn'):
                    raise AccessError('用户名或密码不正确', 401)
                dn = rows[0]['dn']
            stages.append({'stage': 'search', 'ok': True, 'message': '用户定位成功'})
            if not config['attributes_in_bind']:
                phase = 'authenticate'
                _bind(conn, dn, password)
                if not direct:
                    stages.append({'stage': 'authenticate', 'ok': True, 'message': '用户认证成功'})
            phase = 'attributes'
            rows = _search(conn, dn, query, _attributes(config), size_limit=2)
            if len(rows) != 1:
                raise AccessError('用户未满足目录登录条件', 401)
            identity = _identity(rows[0], config, username)
            stages.append({'stage': 'attributes', 'ok': True, 'message': '用户属性和稳定身份已读取'})
            phase = 'groups'
            if not _group_allowed(conn, config, rows[0]):
                raise AccessError('用户未满足群组登录条件', 401)
            stages.append({'stage': 'groups', 'ok': True, 'message': '群组登录条件通过'})
            if not direct and config['attributes_in_bind']:
                phase = 'authenticate'
                _bind(conn, dn, password)
                stages.append({'stage': 'authenticate', 'ok': True, 'message': '用户认证成功'})
            return identity | {'stages': stages}
    except AccessError as error:
        error.steps = stages + [{'stage': phase, 'ok': False, 'message': str(error)}]
        raise


def test_connection(config):
    result, phase = [], 'configuration'
    try:
        config = validate_config(config)
        phase = 'connect'
        with connection(config) as conn:
            result.append({'stage': 'connect', 'ok': True, 'message': '目录连接成功'})
            if config['type'] == 'binddn':
                phase = 'bind'
                _query_bind(conn, config)
                result.append({'stage': 'bind', 'ok': True, 'message': '查询身份绑定成功'})
            return result
    except AccessError as error:
        error.steps = result + [{'stage': phase, 'ok': False, 'message': str(error)}]
        raise


def search_users(config, query=''):
    config = validate_config(config)
    if config['type'] == 'simple' and not (config['bind_dn'] and config['bind_password']):
        raise AccessError('Simple auth 未配置查询凭据，请先让用户登录，再分配组织权限', 409)
    if query:
        _validate_username(query)
    results = []
    with connection(config) as conn:
        _query_bind(conn, config)
        ldap_filter = format_template(config['filter'], query or '*')
        if config['paged_search_enabled'] and not query:
            iterator = conn.extend.standard.paged_search(config['user_base'], ldap_filter,
                search_scope=ldap3.SUBTREE, attributes=_attributes(config),
                paged_size=config['paged_search_size'], generator=False, time_limit=5)
            if conn.result.get('result') != 0:
                raise AccessError('目录分页查询未完整成功', 503)
            rows = [row for row in iterator if row.get('type') == 'searchResEntry']
        else:
            rows = _search(conn, config['user_base'], ldap_filter, _attributes(config), size_limit=2 if query else 0)
        if query and len(rows) > 1:
            raise AccessError('目录查询返回多个用户，请使用唯一登录名')
        for row in rows:
            identity = _identity(row, config, query)
            if _account_active(row, identity) and _group_allowed(conn, config, row):
                results.append(identity)
    return results


def revalidate(config, username, subject):
    rows = search_users(config, username)
    if len(rows) != 1 or rows[0]['subject'] != subject:
        raise AccessError('目录账号状态或稳定身份已变化，请重新登录', 401)
    return rows[0]

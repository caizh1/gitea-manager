"""旧版 GitLab 私有上传文件的受限网页登录，不改变来源业务数据。"""
from html.parser import HTMLParser
import requests
from svn_migration.common import MigrationError, require


class LoginForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.token = ''
    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if tag == 'input' and data.get('name') == 'authenticity_token':
            self.token = data.get('value', '')


def authenticate_web(provider):
    require(provider.kind == 'gitlab' and provider.auth_method == 'password',
            '此旧版私有上传文件需要网页登录；当前令牌不能证明具有下载权限', 'attachment_auth')
    if getattr(provider, 'web_authenticated', False):
        return
    http = provider.http
    try:
        with http.session.get(http.base + '/users/sign_in', allow_redirects=False, timeout=(10, 30), verify=http.verify, stream=True) as response:
            require(response.status_code == 200, '无法读取来源登录表单', 'attachment_auth')
            body = bytearray()
            for chunk in response.iter_content(65536):
                http.tick()
                body.extend(chunk)
                require(len(body) <= 1024 * 1024, '来源登录表单异常过大')
        parser = LoginForm()
        parser.feed(body.decode('utf-8', errors='strict'))
        require(parser.token, '来源未提供登录校验信息', 'attachment_auth')
        with http.session.post(http.base + '/users/sign_in', data={
            'authenticity_token': parser.token, 'user[login]': provider.username,
            'user[password]': provider.secret, 'user[remember_me]': '0'},
            allow_redirects=False, timeout=(10, 30), verify=http.verify) as response:
            require(response.status_code in (302, 303), '来源网页登录未完成，可能要求二次验证', 'attachment_auth')
        # 不跟随来源返回的任意地址；通过固定 API 回读会话身份。
        with http.session.get(http.base + '/api/v4/user', allow_redirects=False,
                              timeout=(10, 30), verify=http.verify) as response:
            require(response.status_code == 200 and response.json().get('id') == provider.actor['id'],
                    '来源网页会话身份与已确认管理员不一致', 'attachment_auth')
        provider.web_authenticated = True
    except (requests.RequestException, ValueError, UnicodeError):
        raise MigrationError('来源网页登录失败，未建立可信附件下载会话', 'attachment_auth') from None

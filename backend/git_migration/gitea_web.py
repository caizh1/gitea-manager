"""通过受限网页会话读取旧版 Actions 制品；不执行任何业务写入。"""
from html.parser import HTMLParser
import requests
from svn_migration.common import require, MigrationError


class LoginForm(HTMLParser):
    def __init__(self):super().__init__();self.token='';self.username=''
    def handle_starttag(self,tag,attrs):
        data=dict(attrs)
        if tag=='input' and data.get('name')=='_csrf':self.token=data.get('value','')
        if tag=='input' and data.get('name')=='name':self.username=data.get('value','')


def authenticate_web(provider):
    require(provider.kind=='gitea' and provider.auth_method=='password',
            '旧版 Actions 制品需要来源网页登录读取；请使用专用迁移管理员密码', 'artifact_auth')
    if getattr(provider,'web_authenticated',False):return
    http=provider.http
    try:
        r=http.session.get(http.base+'/user/login',allow_redirects=False,timeout=(10,30),verify=http.verify)
        require(r.status_code==200 and len(r.content)<=1024*1024,'来源登录表单无效')
        form=LoginForm();form.feed(r.text)
        data={'user_name':provider.username,'password':provider.secret}
        if form.token:data['_csrf']=form.token
        r=http.session.post(http.base+'/user/login',data=data,headers={'Origin':http.base},allow_redirects=False,timeout=(10,30),verify=http.verify)
        require(r.status_code in (302,303),'来源网页登录失败，可能要求二次验证','artifact_auth')
        r=http.session.get(http.base+'/user/settings',allow_redirects=False,timeout=(10,30),verify=http.verify)
        require(r.status_code==200 and len(r.content)<=1024*1024,'网页会话无法读取已登录资料','artifact_auth')
        profile=LoginForm();profile.feed(r.text)
        require(profile.username==provider.actor['username'],'网页与 API 来源身份不同','artifact_auth')
        provider.web_authenticated=True
    except (requests.RequestException,ValueError):
        raise MigrationError('来源网页会话无法核对','artifact_auth') from None


def read_run(provider, project_path, index):
    """Gitea 网页以 POST 提供只读运行视图，不能复用为任意来源写入通道。"""
    import json,time
    from urllib.parse import quote
    require(type(index) is int and index>0,'流水线编号无效')
    parts=project_path.split('/');require(len(parts)>=2 and all(p and p not in ('.','..') for p in parts),'来源路径无效')
    authenticate_web(provider);http=provider.http
    path='/'+ '/'.join(quote(p,safe='') for p in parts)+'/actions/runs/'+str(index)+'/jobs/0'
    try:
        with http.session.post(http.base+path,json={},headers={'Origin':http.base},allow_redirects=False,timeout=(10,30),verify=http.verify,stream=True) as r:
            require(r.status_code==200,'旧版 Actions 运行视图不可读取')
            body=bytearray()
            for chunk in r.iter_content(65536):
                http.tick();require(time.monotonic()<http.deadline,'运行视图读取超时')
                body.extend(chunk);require(len(body)<=http.max_bytes,'运行视图容量超限')
        return json.loads(body)
    except (requests.RequestException,ValueError):raise MigrationError('旧版 Actions 运行视图读取失败') from None

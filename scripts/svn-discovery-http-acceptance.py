"""真实 Apache SVN 列表、鉴权与完整项目纳管验收，仅创建隔离临时数据。"""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from flask import Flask
from models import db
from access.control import install
from tests.access_helpers import init_test_auth, login_platform
from routes.svn_migration_routes import svn_migration_bp
from svn_migration import discovery, service

checks=[]
def check(name,valid):
    checks.append({'检查':name,'通过':bool(valid)});print(('通过：' if valid else '失败：')+name,flush=True)
    Path('/evidence/http-acceptance.json').write_text(json.dumps({'说明':'真实 Apache SVN、Basic 鉴权与 Manager API 隔离验收','检查':checks},ensure_ascii=False,indent=2))
    assert valid,name

with tempfile.TemporaryDirectory() as temp:
    root=Path(temp);root.chmod(0o755)
    repositories=root/'repositories';repositories.mkdir()
    for name in ('emmc_production','project_a','project_b'):
        subprocess.run(['svnadmin','create',str(repositories/name)],check=True)
        tree=root/('tree-'+name);(tree/'trunk').mkdir(parents=True);(tree/'branches').mkdir();(tree/'tags').mkdir();(tree/'test_spec').mkdir()
        (tree/'trunk'/'main.c').write_text('项目代码\n');(tree/'test_spec'/'test.txt').write_text('项目测试规范\n')
        subprocess.run(['svn','import',str(tree),'file://'+str(repositories/name),'-m','创建验收项目','--username','reader'],check=True,stdout=subprocess.DEVNULL)
    subprocess.run(['htpasswd','-bc',str(root/'passwd'),'reader','fixture-only'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    (root/'passwd').chmod(0o644)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
    web=root/'web';web.mkdir();(web/'index.html').write_text('<a href="/svn/">Repositories</a>')
    conf=root/'apache.conf'
    conf.write_text(f'''ServerRoot "{root}"
PidFile "{root}/http.pid"
ErrorLog "{root}/error.log"
Listen 127.0.0.1:{port}
ServerName localhost
User www-data
Group www-data
LoadModule mpm_event_module /usr/lib/apache2/modules/mod_mpm_event.so
LoadModule authn_core_module /usr/lib/apache2/modules/mod_authn_core.so
LoadModule authn_file_module /usr/lib/apache2/modules/mod_authn_file.so
LoadModule authz_core_module /usr/lib/apache2/modules/mod_authz_core.so
LoadModule authz_user_module /usr/lib/apache2/modules/mod_authz_user.so
LoadModule auth_basic_module /usr/lib/apache2/modules/mod_auth_basic.so
LoadModule dir_module /usr/lib/apache2/modules/mod_dir.so
DocumentRoot "{web}"
DirectoryIndex index.html
<Directory "{web}">
Require all granted
</Directory>
LoadModule dav_module /usr/lib/apache2/modules/mod_dav.so
LoadModule dav_svn_module /usr/lib/apache2/modules/mod_dav_svn.so
<Location /svn>
DAV svn
SVNParentPath "{repositories}"
SVNListParentPath on
AuthType Basic
AuthName "隔离验收"
AuthUserFile "{root}/passwd"
Require valid-user
</Location>
''')
    server=subprocess.Popen(['apache2','-f',str(conf),'-DFOREGROUND'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    try:
        for _ in range(50):
            try:
                with socket.create_connection(('127.0.0.1',port),.2):break
            except OSError:time.sleep(.1)
        assert server.poll() is None,server.stderr.read().decode()
        app=Flask(__name__);app.secret_key='仅用于本次隔离验收'*8
        app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:///'+str(root/'manager.db'),TESTING=True)
        init_test_auth(app);app.register_blueprint(svn_migration_bp,url_prefix='/api/svn-migrations');install(app)
        with app.app_context():
            client=app.test_client();csrf=login_platform(client)
            def post(path,data):return client.post('/api/svn-migrations'+path,json=data,headers={'X-CSRF-Token':csrf})
            result=post('/sources',{'name':'真实 HTTP SVN','slug':'real-http','url':f'http://127.0.0.1:{port}','username':'reader','password':'fixture-only'})
            check('创建实例不要求项目 URL',result.status_code==201)
            source=result.get_json();response=post(f"/sources/{source['id']}/discoveries",{})
            check('无已登记仓库也可以连接并发现',response.status_code==202)
            discovery.step(lambda:None)
            result=client.get('/api/svn-migrations/discoveries/'+str(response.get_json()['id'])).get_json()
            check('根页导航链接不会遮挡真实 /svn 列表，自动发现三个项目',result['status']=='ready' and len(result['result']['projects'])==3)
            projects=result['result']['projects'];emmc=next(p for p in projects if p['name']=='emmc_production')
            check('测试规范目录保持在 emmc 内部',any(e['name']=='test_spec' for e in emmc['entries']) and not any(p['name']=='test_spec' for p in projects))
            selected=[p['key'] for p in projects if p['name']!='project_b']
            registered=post(f"/sources/{source['id']}/enroll",{'discovery_id':result['id'],'keys':selected})
            check('多选两个项目只生成两个完整项目仓库',registered.status_code==201 and len(registered.get_json())==2 and all(r['layout']['project'] for r in registered.get_json()))
            again=post(f"/sources/{source['id']}/enroll",{'discovery_id':result['id'],'keys':selected})
            check('重复提交不会重复登记',again.get_json()==registered.get_json())
            bad=service.source_edit({'name':'错误账号','slug':'bad-http','url':f'http://127.0.0.1:{port}/svn','username':'reader','password':'wrong-password'})
            badrow=discovery.queue(bad);discovery.step(lambda:None)
            check('错误密码为失败，不显示为空项目成功',badrow.status=='failed' and '认证' in badrow.error)
            conf.write_text(conf.read_text().replace('SVNListParentPath on','SVNListParentPath off'))
            server.terminate();server.wait(10);server=subprocess.Popen(['apache2','-f',str(conf),'-DFOREGROUND'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE);time.sleep(.3)
            unavailable=discovery.queue(db.session.get(__import__('svn_migration.models',fromlist=['SvnSource']).SvnSource,source['id']))
            discovery.step(lambda:None)
            check('服务未开放列表时显示备用入口而非空仓库成功',unavailable.status=='unavailable')
            fallback=post(f"/sources/{source['id']}/discoveries",{'url':emmc['url']})
            discovery.step(lambda:None)
            fallback=client.get('/api/svn-migrations/discoveries/'+str(fallback.get_json()['id'])).get_json()
            check('列表关闭后补充具体项目地址可继续',fallback['status']=='ready' and len(fallback['result']['projects'])==1)
            conf.write_text(conf.read_text().replace('SVNListParentPath off','SVNListParentPath on').replace('<Location /svn>','<Location /repos>'))
            (web/'index.html').write_text('<a href="/repos/">Repositories</a>')
            server.terminate();server.wait(10);server=subprocess.Popen(['apache2','-f',str(conf),'-DFOREGROUND'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE);time.sleep(.3)
            custom=service.source_edit({'name':'自定义入口','slug':'custom-prefix','url':f'http://127.0.0.1:{port}','username':'reader','password':'fixture-only'})
            custom_row=discovery.queue(custom);discovery.step(lambda:None)
            custom_result=discovery.public(custom_row)
            check('真实自定义 /repos 入口可从首页导航发现',custom_row.status=='ready' and len(custom_result['result']['projects'])==3 and all('/repos/' in row['url'] for row in custom_result['result']['projects']))
            conf.write_text(conf.read_text().replace('<Location /repos>','<Location />'))
            server.terminate();server.wait(10);server=subprocess.Popen(['apache2','-f',str(conf),'-DFOREGROUND'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE);time.sleep(.3)
            no_prefix=service.source_edit({'name':'无前缀入口','slug':'empty-prefix','url':f'http://127.0.0.1:{port}','username':'reader','password':'fixture-only'})
            root_row=discovery.queue(no_prefix);discovery.step(lambda:None)
            check('真实无前缀根列表可发现三个项目',root_row.status=='ready' and len(discovery.public(root_row)['result']['projects'])==3)
    finally:
        server.terminate();server.wait(10)

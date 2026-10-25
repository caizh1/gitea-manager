"""本次独立验收的 HTTP SVN 服务，不接触任何现有仓库。"""
import os
from pathlib import Path
import subprocess
root=Path('/srv/fixture');root.mkdir(parents=True,exist_ok=True)
for name in ('emmc_production','project_a','project_b'):
    repo=root/name;subprocess.run(['svnadmin','create',str(repo)],check=True)
    tree=Path('/tmp')/name
    for entry in ('trunk','branches','tags','test_spec'):(tree/entry).mkdir(parents=True,exist_ok=True)
    (tree/'trunk/code.c').write_text('代码内容\n');(tree/'test_spec/spec.txt').write_text('完整保留的测试规范\n')
    subprocess.run(['svn','import',str(tree),'file://'+str(repo),'-m','创建完整项目','--username','alice'],check=True,stdout=subprocess.DEVNULL)
    subprocess.run(['svn','copy','file://'+str(repo)+'/trunk','file://'+str(repo)+'/branches/dev','-m','建立分支','--username','alice'],check=True,stdout=subprocess.DEVNULL)
    subprocess.run(['svn','copy','file://'+str(repo)+'/trunk','file://'+str(repo)+'/tags/v1','-m','建立标签','--username','alice'],check=True,stdout=subprocess.DEVNULL)
pw=subprocess.check_output(['openssl','passwd','-apr1','-stdin'],input=(os.environ['SVN_TEST_PASSWORD']+'\n').encode()).decode().strip()
password=root/'passwords';password.write_text('migration:'+pw+'\n');password.chmod(0o644)
modules=['mpm_event','authn_core','authn_file','authz_core','authz_user','auth_basic','dav','dav_svn']
conf=root/'apache.conf';conf.write_text('ServerRoot /srv/fixture\nPidFile /srv/fixture/http.pid\nErrorLog /srv/fixture/error.log\nListen 8080\nServerName localhost\nUser www-data\nGroup www-data\n'+''.join(f'LoadModule {m}_module /usr/lib/apache2/modules/mod_{m}.so\n' for m in modules)+'''<Location /svn>
DAV svn
SVNParentPath /srv/fixture
SVNListParentPath on
AuthType Basic
AuthName "隔离验收"
AuthUserFile /srv/fixture/passwords
Require valid-user
</Location>
''')
os.execvp('apache2',['apache2','-f',str(conf),'-DFOREGROUND'])

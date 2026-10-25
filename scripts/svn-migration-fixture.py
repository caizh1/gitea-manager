"""仅在独立 Linux SVN 容器中创建固定验收数据，不访问业务实例。"""
import os
from pathlib import Path
import subprocess
import tempfile
import shutil


def run(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


root = Path('/svn')
root.mkdir(exist_ok=True)
password = os.environ['SVN_TEST_PASSWORD']
for index in range(3):
    repository = root / ('project-' + str(index))
    run('svnadmin', 'create', str(repository))
    (repository / 'conf' / 'svnserve.conf').write_text('[general]\nanon-access = none\nauth-access = read\npassword-db = passwd\nrealm = ' + os.environ['SVN_TEST_SOURCE'] + '\n')
    (repository / 'conf' / 'passwd').write_text('[users]\nmigration = ' + password + '\n')
    trunk, branches, tags = ('trunk','branches','tags') if index==0 else ('mainline','devlines','releases') if index==1 else ('src','','')
    with tempfile.TemporaryDirectory() as folder:
        tree = Path(folder) / 'seed'; tree.mkdir()
        for path in (trunk,branches,tags):
            if path:(tree/path).mkdir()
        (tree/trunk/'README.md').write_text('验收项目 ' + os.environ['SVN_TEST_SOURCE'] + '\n')
        (tree/trunk/'中文目录').mkdir()
        (tree/trunk/'中文目录'/'含 空格.txt').write_text('原始内容\n')
        (tree/trunk/'binary.bin').write_bytes(bytes(range(256))*32)
        (tree/trunk/'empty').mkdir()
        (tree/trunk/'.gitea'/'workflows').mkdir(parents=True)
        (tree/trunk/'.gitea'/'workflows'/'verify.yml').write_text('name: 迁移后手动启用\non: push\njobs:\n  verify:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo 验证\n')
        run('svn','import',str(tree),'file://'+str(repository),'-m','初始导入','--username','alice')
        checkout=Path(folder)/'checkout'
        run('svn','checkout','file://'+str(repository),str(checkout))
        (checkout/trunk/'run.sh').write_text('#!/bin/sh\nprintf "测试\\n"\n')
        (checkout/trunk/'link').symlink_to('README.md')
        run('svn','add',str(checkout/trunk/'run.sh'),str(checkout/trunk/'link'))
        run('svn','propset','svn:executable','*',str(checkout/trunk/'run.sh'))
        run('svn','propset','svn:keywords','Id',str(checkout/trunk/'README.md'))
        run('svn','commit',str(checkout),'-m','增加脚本和符号链接','--username','bob')
        if branches:
            run('svn','copy','file://'+str(repository)+'/'+trunk,'file://'+str(repository)+'/'+branches+'/feature','-m','创建功能分支','--username','alice')
            run('svn','copy','file://'+str(repository)+'/'+trunk,'file://'+str(repository)+'/'+tags+'/v1','-m','创建标签','--username','alice')
            run('svn','copy','file://'+str(repository)+'/'+trunk,'file://'+str(repository)+'/'+branches+'/retired','-m','创建历史分支','--username','bob')
            run('svn','delete','file://'+str(repository)+'/'+branches+'/retired','-m','删除历史分支','--username','bob')
        run('svn','update',str(checkout))
        run('svn','move',str(checkout/trunk/'中文目录'/'含 空格.txt'),str(checkout/trunk/'中文目录'/'重命名.txt'))
        (checkout/trunk/'README.md').write_text('最终内容\n$Id$\n')
        run('svn','commit',str(checkout),'-m','重命名与更新','--username','alice')
for name in ('project-later-a', 'project-later-b', 'external-current', 'external-history'):
    repository=root/name
    shutil.copytree(root/'project-2',repository)
    run('svnadmin','setuuid',str(repository))
    if name.startswith('external'):
        with tempfile.TemporaryDirectory() as folder:
            checkout=Path(folder)/'checkout'
            run('svn','checkout','file://'+str(repository),str(checkout))
            run('svn','propset','svn:externals','^/src external',str(checkout/'src'))
            run('svn','commit',str(checkout),'-m','增加外部依赖','--username','alice')
            if name=='external-history':
                run('svn','propdel','svn:externals',str(checkout/'src'))
                run('svn','commit',str(checkout),'-m','移除外部依赖','--username','alice')
print('已创建基础仓库、后续批次和外部依赖负面样本')

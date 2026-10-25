"""在已有隔离验收网络中复跑来源工具，保留原脚本及原断言不变。"""
import argparse
import json
from pathlib import Path
import subprocess

root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--prefix',required=True)
parser.add_argument('--image',default='gitea-review-svn:svn-20261022-r2')
parser.add_argument('--tools',type=Path,default=root.parent/'gitea-enhancement'/'tools')
args=parser.parse_args()
state=json.loads((root/'work/svn-acceptance'/args.prefix/'resources.json').read_text())
volume=args.prefix+'-original-regression'
source=args.prefix+'-original-source'


def docker(*argv,data=None):
    return subprocess.check_output(['docker',*argv],input=data).decode().strip()


docker('volume','create','--label','io.gitea-manager.test=svn',volume)
code='import json,sys;from pathlib import Path;v=json.load(sys.stdin);p=Path("/lab");(p/"svn").mkdir(exist_ok=True);(p/"svn-password").write_text(v["svn_password"]);(p/"token").write_text(v["gitea_token"])'
docker('run','--rm','-i','-v',volume+':/lab',args.image,'python','-c',code,data=json.dumps(state).encode())
docker('run','-d','--name',source,'--network',args.prefix,'--network-alias','original-source',
       '--label','io.gitea-manager.test=svn','-v',volume+':/lab',args.image,'svnserve','--daemon','--foreground','--root','/lab/svn')
subprocess.run(['docker','run','--rm','--network',args.prefix,'--label','io.gitea-manager.test=svn',
    '-v',volume+':/lab','-v',str(args.tools)+':/original:ro',args.image,'python','/original/test-svn-migration-lab.py',
    '--svn-base','svn://original-source','--gitea-url','http://gitea:3000','--owner','migration-admin'],check=True)
raw=docker('run','--rm','-v',volume+':/lab',args.image,'python','-c',
           'from pathlib import Path;import json;p=sorted(Path("/lab").glob("acceptance-*/acceptance.json"))[-1];r=json.loads(p.read_text());print(json.dumps({"状态":r["status"],"断言数":len(r["checks"]),"已转换提交数":r["git_commits"]},ensure_ascii=False))')
(root/'work/svn-acceptance'/args.prefix/'original-regression.json').write_text(raw+'\n')
print('原工具回归结果：'+raw)

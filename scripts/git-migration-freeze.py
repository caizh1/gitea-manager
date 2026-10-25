#!/usr/bin/env python3
"""从已提交基线叠加本轮迁移文件，固定构建输入，排除并行备份恢复开发。"""
import argparse,hashlib,io,json,shutil,subprocess,tarfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);a=p.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    archive=subprocess.check_output(['git','archive','--format=tar','HEAD'],cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:tar.extractall(out)
    paths=[ROOT/'backend/git_migration',ROOT/'backend/routes/git_migration_routes.py',ROOT/'frontend/src/views/GitMigrations.vue',ROOT/'frontend/src/components/GitMigrationExecution.vue',ROOT/'frontend/dist',ROOT/'review-runtime/vendor']
    paths+=list((ROOT/'backend/tests').glob('test_git_migration_*.py'))
    paths+=list((ROOT/'scripts').glob('git-migration-*.py'))+list((ROOT/'scripts').glob('gitlab-migration-*.py'))
    paths+=list((ROOT/'docs').glob('git-migration-*.md'))
    for source in paths:
        target=out/source.relative_to(ROOT)
        if source.is_dir():shutil.copytree(source,target,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__'))
        else:target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
    rows=[]
    for source in sorted([*(out/'backend/git_migration').rglob('*.py'), out/'backend/routes/git_migration_routes.py']):
        rows.append({'文件':str(source.relative_to(out)),'SHA256':hashlib.sha256(source.read_bytes()).hexdigest()})
    (out/'docs/git-migration-source-snapshot.json').write_text(json.dumps({'说明':'迁移构建输入的固定基线与模块摘要，不包含未提交的其他业务修改','基线':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),'迁移模块':rows},ensure_ascii=False,indent=2)+'\n')
    print('独立迁移源码快照：'+str(out))

if __name__=='__main__':main()

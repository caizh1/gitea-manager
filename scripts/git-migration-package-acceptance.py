#!/usr/bin/env python3
"""核对最终包全部清单、排除运行凭据，装载后逐文件比较真实镜像与包内代码。"""
import argparse,hashlib,json,subprocess,tarfile
from pathlib import Path


def docker(*args):return subprocess.check_output(['docker',*args],text=True).strip()
def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--package',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--load-images',action='store_true');a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
    with tarfile.open(a.package) as archive:
        members=archive.getmembers();names=[m.name for m in members];assert len(names)==len(set(names))
        manifest=json.load(archive.extractfile('docs/AIReview交付清单.json'));expected={r['文件']:r for r in manifest['文件']}
        allowed=set(expected)|{'docs/AIReview交付清单.json'}
        for member in members:
            parts=Path(member.name).parts
            assert not Path(member.name).is_absolute() and '..' not in parts and not member.issym() and not member.islnk()
            if member.isfile():
                assert not any(p in ('.git','work','node_modules','.env','data','__pycache__') for p in parts)
                assert Path(member.name).suffix not in ('.db','.key','.pyc','.log')
                assert member.name in allowed, '出现清单之外的文件'
                if member.name in expected:
                    h=hashlib.sha256();count=0
                    with archive.extractfile(member) as stream:
                        for block in iter(lambda:stream.read(1024*1024),b''):count+=len(block);h.update(block)
                    assert h.hexdigest()==expected[member.name]['SHA256'] and count==expected[member.name]['字节']
            else:assert member.isdir()
        assert set(expected)<={m.name for m in members if m.isfile()}
        archive.extractall(a.output)
    images=json.loads((a.output/'offline/镜像清单.json').read_text())['镜像']
    if a.load_images:docker('image','load','--input',str((a.output/'offline/运行镜像.tar').resolve()))
    actual=[]
    for image in images:
        info=json.loads(docker('image','inspect','--platform','linux/amd64',image['镜像']))[0]
        assert info['Architecture']=='amd64' and info['Os']=='linux'
        assert info['Id'] in (image['ID'],image['配置ID']), '装载镜像身份与导出清单不一致'
        actual.append({'名称':image['镜像'],'身份':info['Id'],'架构':'Linux amd64'})
    code="import json,hashlib;from pathlib import Path;p=Path('/app');print(json.dumps({str(f.relative_to(p)):hashlib.sha256(f.read_bytes()).hexdigest() for f in p.rglob('*.py') if '__pycache__' not in f.parts}))"
    expected_python={str(f.relative_to(a.output/'backend')):sha(f) for f in (a.output/'backend').rglob('*.py') if '__pycache__' not in f.parts}
    for name in ('gitea-review-backend:offline','gitea-review-git:offline','gitea-review-svn:offline'):
        got=json.loads(docker('run','--rm','--platform','linux/amd64','--network','none','--read-only','--entrypoint','python',name,'-c',code))
        assert got==expected_python, '镜像内 Python 代码与包内来源不一致：'+name
    output={'说明':'最终离线包逐文件摘要、敏感运行数据排除、真实镜像装载与运行时代码一致性核对；不代表生产验收','结果':'通过','包SHA256':sha(a.package),'字节':a.package.stat().st_size,'核对文件数':len(expected),'每个管理镜像Python文件数':len(expected_python),'镜像':actual}
    (a.output/'package-acceptance.json').write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n');print('最终包与镜像一致性验收通过：'+str(a.output/'package-acceptance.json'))

if __name__=='__main__':main()

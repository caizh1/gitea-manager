#!/usr/bin/env python3
"""默认只校验离线依赖；显式 --download 仅用于联网准备机，运行服务不会调用本脚本。"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import tarfile
import urllib.request

root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description='验证固定版本的离线运行依赖')
parser.add_argument('--download',action='store_true',help='仅在联网准备机下载缺少或损坏的固定依赖')
args=parser.parse_args()
manifest=json.loads((root/'review-runtime/依赖清单.json').read_text())
for row in manifest['依赖']:
    path=root/row['文件'];ok=path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==row['SHA256']
    if not ok and args.download:
        with urllib.request.urlopen(row['来源'],timeout=90) as response:data=response.read()
        expected=row.get('归档SHA256',row['SHA256'])
        if hashlib.sha256(data).hexdigest()!=expected:raise RuntimeError('下载文件哈希不匹配：'+row['组件'])
        if '归档SHA256' in row:
            with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as archive:
                files=[m for m in archive.getmembers() if m.isfile() and Path(m.name).name=='opencode']
                if len(files)!=1:raise RuntimeError('OpenCode 归档结构异常')
                data=archive.extractfile(files[0]).read()
        if hashlib.sha256(data).hexdigest()!=row['SHA256']:raise RuntimeError('运行文件哈希不匹配')
        path.parent.mkdir(exist_ok=True,parents=True);path.write_bytes(data)
        if row['组件']=='OpenCode':path.chmod(0o555)
        ok=True
    if not ok:raise RuntimeError('缺少固定依赖或哈希不匹配：'+row['文件']+'；请在联网准备机使用 --download')
    print(row['组件']+' '+row['版本']+'：SHA-256 校验通过')

#!/usr/bin/env python3
"""导出审批分项证据到离线包；排除私有安装包、凭据、Compose 与运行数据。"""
import hashlib
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
target=ROOT/'docs/审批规则开发验收证据/分项记录'
target.mkdir(parents=True,exist_ok=True)


def sanitize(value):
    if isinstance(value,dict):
        return {k:('[私有凭据已隐藏]' if re.fullmatch(r'(?i)(password|cookie|authorization|api_token|token|secret|口令|密码)',k)
                   else sanitize(v)) for k,v in value.items()}
    if isinstance(value,list):return [sanitize(v) for v in value]
    return value


records=[]
for path in sorted((ROOT/'work').glob('审批可行性-*/可行性结果.json')):
    value=json.loads(path.read_text())
    if value.get('状态') in ('准备中','运行中') and not value.get('结果') and not value.get('矩阵子项'):continue
    name=path.parent.name.removeprefix('审批可行性-')+'.json'
    clean=sanitize(value)
    (target/name).write_text(json.dumps({'说明':'原始断言与失败内容保留；凭据字段脱敏，源码快照摘要用于区分历史版本',
        '原记录SHA256':hashlib.sha256(path.read_bytes()).hexdigest(),'记录':clean},ensure_ascii=False,indent=2)+'\n')
    records.append({'运行标识':name[:-5],'离线证据':'分项记录/'+name,'原记录SHA256':hashlib.sha256(path.read_bytes()).hexdigest()})
(target.parent/'离线证据目录.json').write_text(json.dumps({'说明':'只导出指定的分项结果 JSON；不导出 work 私有现场',
    '记录':records},ensure_ascii=False,indent=2)+'\n')
print('已导出中文审批证据记录：'+str(len(records)))

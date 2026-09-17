#!/usr/bin/env python3
"""汇总已留存证据与矩阵缺口；候选关联不自动变成通过，不覆盖首次失败。"""
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import time

ROOT=Path(__file__).resolve().parents[1]
rows=[]
for path in sorted((ROOT/'work').glob('审批可行性-*/可行性结果.json')):
    value=json.loads(path.read_text())
    if '矩阵子项' not in value:continue
    cases=value['矩阵子项']
    rows.append({'运行标识':value['运行标识'],'分项':value.get('分项','补充专项'),
        '路径':str(path.relative_to(ROOT)),'证据SHA256':hashlib.sha256(path.read_bytes()).hexdigest(),
        '通过子项':sum(r['结果']=='通过' for r in cases),'失败子项':sum(r['结果']!='通过' for r in cases),
        '前置失败':len(value.get('首次失败',[])),'状态原文':value.get('状态','未记录'),
        '子项':[{'编号':r['编号'],'场景':r['场景'],'结果':r['结果']} for r in cases],
        '执行源码摘要':value.get('执行源码摘要',{})})

requirements=[]
for name in ('审批规则测试矩阵.md','审批规则容量验收基线.md'):
    for line in (ROOT/'docs'/name).read_text().splitlines():
        match=re.match(r'^\| ((?:可行性|接入|规则|资格|生命周期|兼容|入口|并发|故障|安全|页面|原生|交付|效率|容量)[^| ]*) \| (.*?) \| (.*?) \|',line)
        if not match:continue
        if not re.search(r'[0-9-]',match.group(1)):continue
        key,scenario,expected=match.groups()
        prefix=key.split('-')[0]
        candidates=[]
        for row in rows:
            found=[c for c in row['子项'] if c['编号']==key or c['编号'].startswith(key+'-')]
            if found:candidates.append({'运行标识':row['运行标识'],'路径':row['路径'],'子项':found})
        status='已有候选证据，尚未完成全部子场景闭环' if candidates else '缺少已关联的执行证据'
        if key in ('接入24-A','接入24-B','容量05-A','容量05-B'):status='用户指定发包后执行；未通过、未删项'
        if key.startswith(('并发','容量')) or key.startswith(('接入13','接入23','接入24')):status='发包后继续：多用户并发或长测；历史结果保留，未标通过'
        if key in ('可行性-最终撤回','可行性-最终移组','并发01','并发02'):status='包含 APR-RACE-001 已知失败；原始断言及十项证据保留'
        requirements.append({'编号':key,'来源':name,'场景':scenario,'原断言':expected,'状态':status,'候选关联':candidates})

out=ROOT/'docs/审批规则矩阵证据索引.json'
result={'说明':'仅索引已执行的独立子项，不把分项通过推导为整行或完整矩阵通过；最终包复验另行执行。',
    '生成时间':time.strftime('%Y-%m-%d %H:%M:%S %Z'),
    '发包结论':'完整矩阵未通过；用户后置多用户并发与长测，内网功能测试包依据独立交付报告判定，本索引不签发生产许可',
    '后置项':['多用户并发与容量','并发接入边界','影子模式24小时','隔离强制模式24小时'],
    '已知暂缓':'APR-RACE-001 的10项原始失败未豁免生产强制发布',
    '原始条目数':len(requirements),'运行记录':rows,'矩阵原条目':requirements}
out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
lines=['# 审批规则完整矩阵执行进度','',
    '**完整矩阵尚未通过；内网功能测试包与生产强制验收分别记录。**本表保留首次失败，只记录已经取得的证据，不将单测数量、分项通过或安装回执当作完整验收。','',
    '用户最新调整：先交付功能测试包；多用户并发、容量、并发接入及影子与隔离强制各 24 小时发包后继续，不阻塞本次内网测试包。失败保留，不据此批准生产强制接入。','',
    '## 已执行记录','',
    '以下是不同时间、不同代码快照的记录，不能累加成最终版本通过率。同一用例的失败和修复后回归均保留。运行标识对应 `work/审批可行性-运行标识/可行性结果.json`。','',
    '| 运行标识 | 分项 | 通过子项 | 失败子项 | 前置失败 |','|---|---|---:|---:|---:|']
for row in rows:
    lines.append('| '+row['运行标识']+' | '+row['分项']+' | '+str(row['通过子项'])+' | '+str(row['失败子项'])+' | '+str(row['前置失败'])+' |')
lines+=['','## 矩阵条目逐项核对','',
    '详细子项、原始断言、源码及证据摘要见《审批规则矩阵证据索引.json》。带组合条件的条目仍需全部展开，候选证据不会自动标为整行通过。','',
    '| 原编号 | 场景 | 当前状态 |','|---|---|---|']
for row in requirements:
    lines.append('| '+row['编号']+' | '+row['场景'].replace('|','/')+' | '+row['状态']+' |')
lines+=['','## 仍需完成的验收边界','',
    '- 正式接入的并发切换、全部安装中断/并发修改、批量执行与运维漂移、真实后台任务共存仍需完整实测。',
    '- 页面需要真实代理、账户切换、故障、主题与浏览器验证；独立展示和缓存改动后需要重跑相关回归。',
    '- 千人规模需要原生同负载基线和独立资源；原生或扩展出现失败时停止升档，不能用延长超时或降低人数宣称达标。',
    '- 目标现场和人工精力减少80%缺少实际输入，不伪造现场结果或人工测量。',
    '- 最终代码与部署配置稳定后，执行全量回归和最终离线包接入闭环；旧包及旧镜像不能当作本轮新交付。','']
(ROOT/'docs/审批规则完整矩阵执行进度.md').write_text('\n'.join(lines))
print(json.dumps({'说明':'证据索引已更新，没有签发完整矩阵通过结论','运行记录数':len(rows),'原条目数':len(requirements),'文件':str(out)},ensure_ascii=False))

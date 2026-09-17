#!/usr/bin/env python3
"""从离线包验收辅助意见和反馈闭环；协议替身不计入模型质量。"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import runpy
import time
import zipfile

boundary=runpy.run_path(str(Path(__file__).with_name('review-boundary-acceptance.py')))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name',default='dataset-offline-acceptance')
    args=parser.parse_args();s=boundary['Suite'](args.name);s.prepare();h=s.h
    if h.manager('GET','/ai-review/config')['config']['engine']!='opencode_cli':
        raise RuntimeError('请先配置OpenCode引擎，才能完整验证两种执行方式')
    code=b'int protocol_probe(void) {\n    return 1; /* REVIEW_TEST_TARGET */\n}\n'
    s.control('natural_only');pr=s.branch('自然语言保留',{'probe.c':code})
    task=s.check('缺少结构化证据仍发布真实行内意见',pr,[('probe.c','new',2)])
    f=task['result']['findings'][0]
    s.record('确实未提供证据字段',{'通过':not f['support'] and not f['trace'] and bool(f['explanation'])})
    endpoint=f'/ai-review/repositories/{s.repo["id"]}/dataset'
    command=h.gitea('POST',f'/repos/{s.full}/issues/{pr["number"]}/comments',{'body':'/ai-review 反馈 '+f['finding_id']+' 排除 条件=此函数的返回值仅用于本机协议验收，不代表真实设备的错误语义。'})
    for _ in range(90):
        rows=h.manager('GET',endpoint)['items'];sample=next((v for v in rows if v['origin_comment']==command['id']),None)
        if sample and sample['reference_ready']:break
        time.sleep(1)
    else:raise RuntimeError('真实评论尚未进入可参考数据集，保留现场')
    s.record('真实Webhook确认与字面量函数关联',{'通过':'protocol_probe' in sample['symbols'],'样本':sample['id'],'修订':sample['revision']})
    next_pr=s.branch('参考进入新版本',{'probe.c':code.replace(b'return 1;',b'return 2;')})
    used=s.check('同函数新提交重新定位',next_pr,[('probe.c','new',2)])
    refs=[r for e in used['result']['evidence'] for r in e['input'].get('historical_samples',[])]
    s.record('实际模型输入含冻结修订',{'通过':any(r['id']==sample['id'] and r['revision']==sample['revision'] for r in refs),'实际参考':refs})
    raw=h.request('GET',h.manager_url+'/api'+endpoint+'/export?history=1',opener=h.opener,raw=True)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        lines=z.read('审查数据集.jsonl');manifest=json.loads(z.read('清单.json'))
        s.record('离线数据集导出可校验',{'通过':manifest['SHA256']==hashlib.sha256(lines).hexdigest(),'样本数':manifest['样本数']})
    h.manager('POST',endpoint+'/'+sample['id']+'/disable',{})
    disabled=next(v for v in h.manager('GET',endpoint)['items'] if v['id']==sample['id'])
    s.record('停用保留原文且不再参考',{'通过':disabled['status']=='disabled' and not disabled['reference_ready'] and disabled['original']==sample['original']})
    s.control('refutation_incomplete');unresolved=s.branch('反证缺失',{'uncertain.c':code})
    advisory=s.check('反证缺失保留普通意见',unresolved,[])
    s.record('普通意见明确不确定且不宣称完全覆盖',{'通过':len(advisory['result']['findings'])==1 and advisory['result']['findings'][0]['assessment']=='uncertain' and not advisory['result']['coverage']['complete']})
    cfg=h.manager('GET','/ai-review/config')['config'];cfg.pop('api_key_configured',None);cfg['engine']='new_api_http';h.manager('PUT','/ai-review/config',cfg)
    s.control('natural_only');direct=s.branch('HTTP执行器',{'http.c':code})
    s.check('HTTP引擎离线直接发布',direct,[('http.c','new',2)])
    # 前一次切换已经递增配置修订，恢复时重新读取，不能绕过过期编辑保护。
    cfg=h.manager('GET','/ai-review/config')['config'];cfg.pop('api_key_configured',None)
    cfg['engine']='opencode_cli';h.manager('PUT','/ai-review/config',cfg);s.control()
    print('辅助意见与数据集验收记录：'+str(s.out),flush=True)


if __name__=='__main__':main()

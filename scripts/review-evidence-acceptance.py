#!/usr/bin/env python3
"""独立 Gitea / Manager 的千行嵌入式缺陷基准；答案不进入模型输入。"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]


def fixture(fixed=False):
    header = '''#include <stdint.h>
#include <stddef.h>
#include <string.h>
/* 固件测试使用内存寄存器替身；接口会被网络命令处理器调用。 */
typedef struct {
    uint8_t payload[32];
    uint8_t guard[32];
    uint32_t flash[64];
    uint32_t active_bank;
    uint32_t irq_mask;
    uint32_t status;
    uint32_t control;
    uint32_t audit[32];
    uint32_t watchdog;
    uint32_t dma_owner;
    uint32_t pending;
    uint32_t valid_signature;
    uint32_t voltage_mv;
    uint32_t max_temp;
    uint32_t temperature;
    uint32_t authorized;
} Device;
/* control 非零会启动电机、DMA 或闪存控制器，依接口而定。 */
/* pending 非零表示 DMA 正在读取 payload，完成中断前禁止归还所有权。 */
/* active_bank 只允许 0 或 1；flash 对应单元模拟各银行的启动头。 */
/* irq_mask 非零表示关键故障中断已使能，恢复时必须保留原值。 */
/* watchdog=1 为已使能，运行固件不得关闭它。 */
/* guard 是报文后紧邻的执行许可区，零值表示许可，不得被数据写入。 */
/* flash 擦写最低电压 2700mV，否则可能损坏唯一有效固件。 */
/* 所有外部调用者依赖返回值小于零时硬件状态不改变。 */
'''.splitlines()
    header += [''] * (40 - len(header))
    cases = [
        ('packet_copy', '报文最长 31 字节，末尾追加终止符。',
         'if (length > sizeof(d->payload)) return -2;', 'if (length >= sizeof(d->payload)) return -2;',
         ['memcpy(d->payload, input, length);', 'd->payload[length] = 0;'],
         'd.guard[0]=0xa5; r=packet_copy(&d,b,32); ok=r<0 && d.guard[0]==0xa5;', '边界长度覆盖执行许可区'),
        ('dma_release', '未完成 DMA 继续持有缓冲区，归还即允许接收方覆盖。',
         'd->dma_owner = 0;', 'if (d->pending == 0) d->dma_owner = 0;', [],
         'd.pending=1; d.dma_owner=1; r=dma_release(&d,b,8); ok=d.dma_owner==1;', '提前归还 DMA 缓冲区'),
        ('firmware_install', '网络下载镜像必须通过已有签名验证才能写入闪存。',
         'if (d->valid_signature == 0) d->valid_signature = 1;', 'if (d->valid_signature == 0) return -2;',
         ['d->flash[1-d->active_bank] = 0xfeedu;', 'd->control = 1;'],
         'd.valid_signature=0; r=firmware_install(&d,b,8); ok=r<0 && d.control==0;', '未签名镜像可写入固件'),
        ('bank_erase', '更新必须仅擦除备用银行，活动银行是当前唯一可启动固件。',
         'd->flash[d->active_bank] = 0;', 'd->flash[1-d->active_bank] = 0;', [],
         'd.active_bank=0; d.flash[0]=0xcafeu; r=bank_erase(&d,b,8); ok=d.flash[0]==0xcafeu;', '擦除活动固件银行'),
        ('motor_start', '温度达到上限时必须拒绝启动电机以避免过热损坏。',
         'if (d->temperature < d->max_temp) return -2;', 'if (d->temperature >= d->max_temp) return -2;',
         ['d->control = 1;'],
         'd.temperature=120; d.max_temp=90; r=motor_start(&d,b,8); ok=r<0 && d.control==0;', '过温保护条件反向'),
        ('privileged_command', '外部维护命令只有 authorized=1 才能修改执行许可。',
         'if (d->authorized == 0) d->authorized = 1;', 'if (d->authorized == 0) return -2;',
         ['d->control = input[0] | 1u;'],
         'd.authorized=0; r=privileged_command(&d,b,8); ok=r<0 && d.control==0;', '未授权命令提升权限'),
        ('interrupt_restore', '短临界区结束后必须恢复故障中断，irq_mask 保存原硬件掩码。',
         'd->irq_mask = 0;', 'd->irq_mask = saved_mask;', [],
         'd.irq_mask=0x80; r=interrupt_restore(&d,b,8); ok=d.irq_mask==0x80;', '故障中断永久关闭'),
        ('flash_dma', 'input 前四字节是目标偏移，DMA 区间必须完整位于 64 字节闪存窗口。',
         'if ((uint32_t)(offset + length) > 64u) return -2;', 'if (offset > 64u || length > 64u-offset) return -2;',
         ['d->status = offset;', 'd->control = (uint32_t)length;'],
         'memset(b,255,4); r=flash_dma(&d,b,8); ok=r<0 && d.control==0;', '目标地址加法溢出绕过校验'),
        ('watchdog_restore', '临界区后必须恢复 watchdog，避免异常固件无限挂死。',
         'd->watchdog = 0;', 'd->watchdog = 1;', [],
         'd.watchdog=1; r=watchdog_restore(&d,b,8); ok=d.watchdog==1;', '看门狗被永久禁用'),
        ('ring_consume', 'pending 是有效 DMA 描述符数量，空环必须拒绝消费。',
         'if (d->pending > 64u) return -2;', 'if (d->pending == 0 || d->pending > 64u) return -2;',
         ['d->pending--;', 'd->control = d->pending;'],
         'd.pending=0; r=ring_consume(&d,b,8); ok=r<0 && d.pending==0;', '空描述符环下溢驱动 DMA'),
        ('power_safe_write', '欠压时禁止闪存写入，最低写入电压为 2700mV。',
         'if (d->voltage_mv < 1700u) return -2;', 'if (d->voltage_mv < 2700u) return -2;',
         ['d->flash[d->active_bank] = input[0];', 'd->control = 1;'],
         'd.voltage_mv=2200; d.flash[0]=0xcafeu; r=power_safe_write(&d,b,8); ok=r<0 && d.flash[0]==0xcafeu;', '欠压写入损坏启动固件'),
        ('dma_receive', '接收 DMA 指向 payload，最大长度为 32 字节，不能覆盖 guard 许可区。',
         'if (length > 32u) length = 64u;', 'if (length > 32u) return -2;',
         ['d->status = (uint32_t)length;', 'd->control = 1;'],
         'r=dma_receive(&d,b,48); ok=r<0 && d.control==0;', '错误放大 DMA 长度越界写'),
    ]
    source = list(header); manifest = []; probes = []
    for number, (name, contract, bad, good, body, probe, title) in enumerate(cases, 1):
        block = [f'/* {contract} */', f'int {name}(Device *d, const uint8_t *input, size_t length)', '{',
                 '    if (d == NULL || input == NULL) return -1;',
                 '    if (length > 64u || d->active_bank > 1u) return -1;',
                 '    uint32_t saved_mask = d->irq_mask;',
                 '    uint32_t offset = 0;',
                 '    if (length >= 4u) memcpy(&offset, input, sizeof(offset));',
                 '    uint32_t sample = (uint32_t)length;',
                 '    /* 保存操作开始时的遥测，供离线事故分析使用。 */']
        for i in range(29):
            block.append(f'    d->audit[{i}] = sample ^ {number*256+i}u;')
        assert len(block) == 39
        block.append('    '+(good if fixed else bad))
        block += ['    '+line for line in body]
        block += ['    /* 保存硬件请求后的遥测状态。 */', '    sample = d->control ^ d->status;']
        while len(block) < 77:
            i = len(block) % 32
            block.append(f'    d->audit[{i}] ^= sample + {number*1024+len(block)}u;')
        block += ['    (void)saved_mask;', '    return 0;', '}']
        assert len(block) == 80
        source.extend(block)
        manifest.append({'编号':number, '函数':name, '严重级别':'P1', '行号':number*80,
                         '问题':title, '触发验证':probe})
        probes.append(f'case {number}: {probe} break;')
    assert len(source) == 1000
    test = '#include "firmware.c"\n#include <stdlib.h>\nint main(int argc,char **argv){Device d={0};uint8_t b[64]={0};int r=0,ok=0;d.watchdog=1;d.voltage_mv=3300;d.max_temp=90;switch(atoi(argv[1])){'+''.join(probes)+'}return ok?0:1;}\n'
    return '\n'.join(source)+'\n', manifest, test


def module(name, path):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result


def initialize(local):
    h=local.Harness(); h.wait(h.gitea_url+'/api/v1/version');h.wait(h.manager_url+'/api/session')
    prefix=['exec','-T','--user','git','gitea','gitea','--config','/data/gitea/conf/app.ini','admin','user']
    users=local.compose(*prefix,'list')
    for user,password,key,admin in [('review-admin','GITEA_PASSWORD','GITEA_TOKEN',True),('review-bot','BOT_PASSWORD','BOT_TOKEN',False),('review-developer','DEVELOPER_PASSWORD','DEVELOPER_TOKEN',False)]:
        if user not in users:
            args=['create','--username',user,'--password',h.values[password],'--email',user+'@example.invalid','--must-change-password=false']
            local.compose(*prefix,*args,*(['--admin'] if admin else []))
        if key not in h.values:
            h.values[key]=local.compose(*prefix,'generate-access-token','--username',user,'--token-name','isolated-evidence', '--scopes','all' if admin else 'read:user,write:repository,write:issue','--raw').strip().splitlines()[-1]
    local.save(h.values);h.login();h.manager('POST','/settings',{'host_ip':'127.0.0.1'})
    if not any(s['name']=='本机 AI Review 验收' for s in h.manager('GET','/servers')):
        h.manager('POST','/servers',{'name':'本机 AI Review 验收','role':'primary','host':'127.0.0.1','ssh_user':'git','gitea_container':'隔离审查实例','pg_container':'隔离数据库','pg_dbname':'gitea','pg_user':'gitea','gitea_port':3000,'gitea_url':'http://gitea:3000','api_token':h.values['GITEA_TOKEN']})
    return h


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir',required=True)
    parser.add_argument('--key-file',required=True)
    parser.add_argument('--timeout',type=int,default=10800)
    args=parser.parse_args();state=Path(args.state_dir).resolve();os.environ['REVIEW_STATE_DIR']=str(state)
    local=module('evidence_local',ROOT/'scripts/review-local.py')
    real=module('evidence_real',ROOT/'scripts/review-real-acceptance.py');real.local=local
    source,truth,probe=fixture();folder=state/'fixture';folder.mkdir(exist_ok=True)
    (folder/'ground-truth.json').write_text(json.dumps(truth,ensure_ascii=False,indent=2)+'\n')
    (folder/'probe.c').write_text(probe)
    proof={}
    for corrected in (False,True):
        (folder/'firmware.c').write_text(fixture(corrected)[0])
        subprocess.run(['cc','-std=c11','-O0',str(folder/'probe.c'),'-o',str(folder/'probe')],check=True)
        outcomes=[subprocess.run([str(folder/'probe'),str(i)],capture_output=True).returncode for i in range(1,13)]
        assert outcomes == ([0]*12 if corrected else [1]*12), outcomes
        proof['修正版本' if corrected else '预埋版本']=outcomes
    (folder/'firmware.c').write_text(source)
    (folder/'trigger-proof.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2)+'\n')
    h=initialize(local)
    h.configure_model('https://api.deepseek.com','deepseek-flash','opencode_native',Path(args.key_file).read_text().strip())
    cfg=h.manager('GET','/ai-review/config')['config'];cfg.pop('api_key_configured',None)
    cfg.update(tokenizer_profile='deepseek41',thinking_protocol='deepseek',thinking_enabled=True,
               model_context_tokens=262144,model_max_output_tokens=32768,batch_input_tokens=65536,
               batch_auto_calls=8,batch_execution_seconds=args.timeout,model_request_timeout_seconds=600,review_timeout_seconds=900,task_timeout_seconds=1000)
    h.manager('PUT','/ai-review/config',cfg)
    repo=h.create_repo('embedded-evidence-benchmark');git=real.git
    checkout=real.clone(h,'embedded-evidence-benchmark');branch='embedded-'+str(time.time_ns())
    git(h,checkout,'switch','main');git(h,checkout,'switch','-c',branch)
    (checkout/'firmware.c').write_text(source);git(h,checkout,'add','firmware.c');git(h,checkout,'commit','-m','新增嵌入式控制与固件更新接口');git(h,checkout,'push','origin',branch)
    sha=git(h,checkout,'rev-parse','HEAD').strip()
    pr=h.gitea('POST',f"/repos/{repo['repo_name']}/pulls",{'head':branch,'base':'main','title':'嵌入式控制与固件更新接口','body':'实现报文接收、DMA、银行更新和保护状态管理，请审查变更的安全性与正确性。'})
    record={'说明':'独立实例真实模型验收；答案与修正版未提交仓库或发送给模型。','模型':'deepseek-flash','代码行数':1000,'预埋P1数量':12,'PR编号':pr['number'],'提交':sha,'仓库':repo['repo_name'],'触发验证':proof,'状态':'等待任务'}
    output=state/'acceptance-result.json';started=time.monotonic();last='';approved=set()
    print('已建立真实 PR，1000 行、12 个经触发验证的缺陷。',flush=True)
    while time.monotonic()-started < args.timeout:
        jobs=h.manager('GET',f"/ai-review/tasks?repository={repo['id']}")['items']
        rows=[j for j in jobs if j['number']==pr['number']]
        if rows:
            job=h.manager('GET','/ai-review/tasks/'+rows[0]['id']);record.update(任务=job['id'],状态=job['status'],耗时秒=round(time.monotonic()-started,1))
            if job['status']!=last:
                print('审查状态：'+job['status']+'；'+job.get('stage',''),flush=True);last=job['status']
            if job['status']=='awaiting_cost':
                plan=h.manager('GET',f"/ai-review/tasks/{job['id']}/plan")
                if 'plan' in plan:plan=plan['plan']
                key=plan.get('hash') or plan.get('plan_hash')
                if key not in approved:
                    h.manager('POST',f"/ai-review/tasks/{job['id']}/confirm-plan",{'plan_hash':key});approved.add(key)
                    record['成本计划']={k:plan.get('value',{}).get(k) for k in ('primary','association','max_calls','input_tokens')}
            if job['status'] in ('succeeded','failed','incomplete','publish_unknown','superseded','cancelled','budget_paused'):
                record.update(错误=job.get('error'),诊断=job.get('diagnostic'),结果=job.get('result'))
                break
        output.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n');time.sleep(3)
    if record['状态']=='succeeded':
        prefix=f"/repos/{repo['repo_name']}/pulls/{pr['number']}"
        reviews=h.gitea_pages(prefix+'/reviews',actor='developer')
        comments=[c for review in reviews for c in h.gitea('GET',prefix+f"/reviews/{review['id']}/comments",actor='developer')]
        record['回写验证']={'Review数量':len(reviews),'提交正确':all(r['commit_id']==sha for r in reviews),'行内评论数量':len(comments)}
        record['实际评论']=[{'路径':c['path'],'行号':c.get('position'),'内容':c.get('body')} for c in comments]
        record['待逐项核对']=[{'编号':t['编号'],'预期行号':t['行号'],'问题':t['问题']} for t in truth]
    output.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n');print('证据已保存：'+str(output),flush=True)
    if record['状态']!='succeeded': raise SystemExit(1)


if __name__=='__main__':main()

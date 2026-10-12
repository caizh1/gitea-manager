#!/usr/bin/env python3
"""生成无定位提示的 C/C++ 验收源码；预期和可执行断言保存在模型不可检索的目录外。"""
import argparse
import hashlib
import json
import re
from pathlib import Path
import shutil
import subprocess
import time


def corpus(seed=41):
    """所有样本在运行模型前固定；第二组变换文件、符号、目录、位置及换行。"""
    cases=[]
    newline='\r\n' if seed!=41 else '\n'
    namespace=hashlib.sha256(str(seed).encode()).hexdigest()[:7]
    for defect in (True,False):
        for kind in ('global','declaration','adjacent','insert','delete','cross_file','interrupt'):
            ident=hashlib.sha256(f'{seed}:{kind}:{defect}'.encode()).hexdigest()[:8]
            path=f'platform/{namespace}/{ident}/'+('controller.cpp' if kind=='interrupt' else 'controller.c')
            ext='cpp' if kind=='interrupt' else 'c';folder=str(Path(path).parent)
            fn='dispatch_'+ident; helper='slot_'+ident;write='store_'+ident
            prefix='typedef unsigned int u32;\n#define DRIVER_ATTR(x) __attribute__((unused))\nDRIVER_ATTR(device) static int driver_identity(void) { return 1; }\n'
            old='';extra={};driver='';changed=[]
            if kind=='global':
                old=prefix+f'\nu32 {fn}(u32 index) {{\n#ifdef PIN_MASK\n  return 1u << (index & PIN_MASK);\n#else\n  return 1u << (index & 31u);\n#endif\n}}\n'
                added='#ifndef CONFIG_COMPILE_APP\nstatic u32 sequence = 0;\n#endif\n#ifndef PIN_MASK\n#define PIN_MASK '+('63u' if defect else '31u')+'\n#endif\n'
                new=old.replace(prefix,prefix+added,1)
                driver=f'int main(void) {{ return {fn}(32u) != 1u; }}\n'
                changed=['#define PIN_MASK '+('63u' if defect else '31u')]
            elif kind in ('declaration','adjacent','insert','delete'):
                block=[]
                for i in range(438):
                    block.extend([f'  if ((key & {1 << (i%8)}u) != 0u) {{',f'    u32 local_{i} = key ^ {i+seed}u;',
                                  f'    for (u32 j = 0; j < {i%3+1}u; ++j) {{',f'      switch ((local_{i} + j) & 3u) {{',
                                  '        case 0: total ^= key; break;',f'        case 1: total += {i+1}u; break;',
                                  '        case 2: total = (total >> 1) | (total << 31); break;',
                                  '        default: total ^= j; break;','      }','    }','#if defined(ENABLE_DEVICE)',
                                  f'    total ^= local_{i};','#else',f'    total += local_{i} & 1u;','#endif','  }'])
                middle=3500+(seed%13)
                core=['  u32 limit = 4u;','  for (u32 pin = 0; pin < limit; ++pin) {','    total += values[pin];','  }']
                # 插入点必须在顶层语法块边界。
                middle=(middle//16)*16;block[middle:middle]=core
                old=prefix+f'\nu32 {fn}(u32 key, u32 count, const u32 *values) {{\n  if (values == 0 || count != 4u) return 0u;\n  u32 total = 0;\n'+'\n'.join(block)+'\n  return total;\n}\n'
                if kind in ('declaration','adjacent'):
                    replacement='  u32 limit = '+('5u;' if defect else 'count;')
                    new=old.replace(core[0],replacement);changed=[replacement]
                    if kind=='adjacent':new=new.replace(core[1],'  for (u32 pin = 0; pin != limit; ++pin) {');changed.append('  for (u32 pin = 0; pin != limit; ++pin) {')
                elif kind=='insert':
                    addition='  total += values['+('count' if defect else 'count - 1u')+'];'
                    new=old.replace(core[0],addition+'\n'+core[0]);changed=[addition]
                else:
                    guard='  if (values == 0 || count != 4u) return 0u;'
                    # 正常对照只删除冗余检查，仍保留真实保护。
                    if not defect:old=old.replace(guard,guard+'\n  if (count < 1u) return 0u;')
                    removed=guard if defect else '  if (count < 1u) return 0u;'
                    new=old.replace(removed+'\n','');changed=[removed]
                driver=f'int main(void) {{ u32 values[4] = {{1,2,3,4}}; (void){fn}(0u,4u,values);'
                if kind=='delete':driver+=f' if ({fn}(0u,0u,0) != 0u) return 1;'
                driver+=' return 0; }\n'
            elif kind=='cross_file':
                header=f'u32 {helper}(u32 ticket);\nu32 {write}(u32 slot, u32 value);\n'
                extra[f'{folder}/request_api.h']=header
                extra[f'{folder}/storage.c']='typedef unsigned int u32;\nstatic u32 cells[4];\n'+f'u32 {helper}(u32 ticket) {{ return '+('ticket % 5u' if defect else '(ticket % 4u) + 1u')+'; }\n'+f'u32 {write}(u32 slot, u32 value) {{ cells[slot - 1u] = value; return cells[slot - 1u]; }}\n'
                old=prefix+'#include "request_api.h"\n'+f'u32 {fn}(u32 ticket, u32 value) {{\n  u32 slot = {helper}(ticket);\n  if (slot == 0u) return 0u;\n  return {write}(slot, value);\n}}\n'
                new=old.replace('  if (slot == 0u) return 0u;\n','');changed=['  if (slot == 0u) return 0u;']
                driver=f'int main(void) {{ (void){fn}(0u,9u); return 0; }}\n'
            else:
                extra[f'{folder}/interrupt_api.h']='#ifndef INTERRUPT_API_H\n#define INTERRUPT_API_H\nstruct IrqState { unsigned depth; bool enabled; };\nvoid restore_irq(IrqState *state);\n#endif\n'
                extra[f'{folder}/interrupt.cpp']='#include "interrupt_api.h"\nvoid restore_irq(IrqState *state) { if (state->depth != 0u) --state->depth; state->enabled = (state->depth == 0u); }\n'
                old='#include "interrupt_api.h"\n'+f'bool {fn}(IrqState *state) {{\n  restore_irq(state);\n  return state->enabled;\n}}\n'
                new=old.replace('  restore_irq(state);','  state->enabled = true;' if defect else '  if (state != nullptr) restore_irq(state);').replace('  return state->enabled;','  return state->enabled;' if defect else '  return state != nullptr && state->enabled;')
                changed=['  state->enabled = true;' if defect else '  if (state != nullptr) restore_irq(state);']
                driver=f'int main() {{ IrqState state{{2u, false}}; {fn}(&state); return state.enabled || state.depth != 1u; }}\n'
            cases.append({'id':ident,'name':kind,'defect':defect,'held_out':seed!=41,'path':path,'language':ext,
                          'old':old.replace('\n',newline),'new':new.replace('\n',newline),
                          'files':{k:v.replace('\n',newline) for k,v in extra.items()},'driver':driver,
                          'root_lines':[i for i,line in enumerate((new if kind not in ('delete','cross_file') else old).splitlines(),1) if line in changed],
                          'side':'old' if kind in ('delete','cross_file') else 'new'})
    return cases


def large_pr():
    files={};changes={}
    for file in range(10):
        old='typedef unsigned int u32;\n';new=old
        for function in range(9):
            name=f'dispatch_{file}_{function}'
            body=f'\nu32 {name}(u32 value) {{\n  u32 cached = value;\n  return cached ^ {file*9+function+1}u;\n}}\n'
            old+=body;new+=body.replace('u32 cached = value;','u32 cached = value + 0u;')
        path=f'device/controllers/controller_{file}.c';files[path]=old;changes[path]=new
    return files,changes


def combined_holdout():
    """独立保留组合：宏增量、大头文件和未修改的七千行被调函数共同出现。"""
    baseline=corpus(4301)
    giant=next(c['old'].replace('\r\n','\n') for c in baseline if c['name']=='declaration' and c['defect'])
    body=giant[giant.index('  u32 total = 0;'):giant.rindex('  return total;')]
    body=body.replace('  u32 limit = 4u;\n  for (u32 pin = 0; pin < limit; ++pin) {\n    total += values[pin];\n  }\n','')
    result=[]
    names={'ticket':'request_id','slot':'queue_index','cells':'queue_slots','value':'payload_word',
           'total':'accumulator','local':'temporary','sequence':'request_sequence'}
    def rename(code):
        code=code.replace('\r\n','\n').replace('request_api.h','queue_contract.h')
        for old,new in names.items():code=re.sub(r'\b'+old+r'\b',new,code)
        return code.replace('\n','\r\n')
    for original in (c for c in baseline if c['name']=='cross_file'):
        case=dict(original);ident=case['id'];old_folder=str(Path(case['path']).parent)
        folder='firmware/requests/'+ident;case['path']=folder+'/request_dispatch.c'
        storage=case['files'][old_folder+'/storage.c'].replace('\r\n','\n')
        old_helper=next(line for line in storage.splitlines() if line.startswith('u32 slot_'))
        expression=old_helper.split('return ',1)[1].rsplit(';',1)[0]
        large_helper=f'u32 slot_{ident}(u32 ticket) {{\n'+re.sub(r'\bkey\b','ticket',body)+'  return '+expression+';\n}'
        storage=storage.replace(old_helper,large_helper)
        header=case['files'][old_folder+'/request_api.h'].replace('\r\n','\n')
        header='#ifndef QUEUE_CONTRACT_H\n#define QUEUE_CONTRACT_H\n'+header+''.join(f'#define DEVICE_OPTION_{i} {i}u\n' for i in range(4000))+'#define DECL_ATTR(x) __attribute__((unused))\nDECL_ATTR(queue) static int queue_identity(void) { return 0; }\n#endif\n'
        case['files']={folder+'/queue_contract.h':rename(header),folder+'/request_slots.c':rename(storage)}
        case['new']=case['new'].replace('#include "request_api.h"', '#ifndef REQUEST_NAME\n#define REQUEST_NAME "queue"\n#endif\n#include "request_api.h"')
        case.update(old=rename(case['old']),new=rename(case['new']),driver=rename(case['driver']),
                    id='combined-'+ident,name='combined_context',held_out=True)
        result.append(case)
    return result


def compile_cases(target,cases):
    compiler=shutil.which('clang');cpp=shutil.which('clang++')
    if not compiler or not cpp:raise RuntimeError('需要本机 C/C++ 编译器验证固定预期')
    reports=[];target.mkdir(parents=True,exist_ok=True)
    for case in cases:
        folder=target/case['id'];folder.mkdir(exist_ok=True)
        for path,code in case['files'].items():
            p=folder/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(code.encode())
        source=folder/case['path'];source.parent.mkdir(parents=True,exist_ok=True)
        driver=folder/('外部断言.'+case['language']);driver.write_text('#include "'+case['path']+'"\n'+case['driver'])
        result={'样本':case['id'],'类型':case['name'],'预期存在缺陷':case['defect'],'保留样本':case['held_out'],'版本':[]}
        for version in ('old','new'):
            source.write_bytes(case[version].encode());output=folder/('执行-'+version)
            command=[cpp if case['language']=='cpp' else compiler,'-std='+('c++17' if case['language']=='cpp' else 'c11'),
                     '-O0','-g','-fsanitize=address,undefined','-fno-sanitize-recover=all','-I',str(folder),
                     str(driver),*[str(folder/p) for p in case['files'] if p.endswith(('.c','.cpp'))],'-o',str(output)]
            start=time.monotonic();p=subprocess.run(command,capture_output=True,text=True)
            (folder/(version+'-编译.txt')).write_text(p.stdout+p.stderr)
            if p.returncode:raise RuntimeError('固定样本未通过编译：'+case['id']+' '+version)
            p=subprocess.run([str(output)],capture_output=True,text=True,timeout=20)
            (folder/(version+'-行为.txt')).write_text(p.stdout+p.stderr)
            expected_failure=version=='new' and case['defect']
            passed=(p.returncode!=0)==expected_failure
            result['版本'].append({'版本':version,'行为退出码':p.returncode,'符合预期':passed,'耗时秒':round(time.monotonic()-start,3)})
            if not passed:raise RuntimeError('固定样本行为与预期不符：'+case['id']+' '+version)
        reports.append(result)
    return reports


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--combined',action='store_true');args=p.parse_args()
    cases=combined_holdout() if args.combined else corpus()+corpus(972)
    # 源码、预期哈希在模型运行和样本调试前冻结。
    args.output.mkdir(parents=True,exist_ok=True)
    manifest={'说明':'预期、评分及断言绝不上传至模型可检索的 Gitea 仓库；源码无缺陷提示。','样本':cases}
    path=args.output/'固定样本与外部预期.json'
    if path.exists() and json.loads(path.read_text())!=manifest:raise RuntimeError('已冻结样本发生变化，禁止覆盖首次基准')
    path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    report=compile_cases(args.output/'编译与行为证据',cases)
    (args.output/'编译与行为验收.json').write_text(json.dumps({'结果':report},ensure_ascii=False,indent=2)+'\n')
    print(f'已冻结 {len(cases)} 个样本，{len(cases)*2} 个版本编译与行为验证通过。')


if __name__=='__main__':main()

"""1.1.51 通过受控工具提交结果；禁止从自由文本或其他会话恢复结论。"""
import json
from pathlib import Path
import shutil


def prepare(home, env):
    config=home/'legacy-config'
    config.mkdir(parents=True,exist_ok=True)
    dependencies=Path('/opt/review/legacy-deps')
    if not (config/'node_modules').exists():
        (config/'node_modules').symlink_to(dependencies/'node_modules',target_is_directory=True)
    (config/'package.json').write_text(json.dumps({'dependencies':{'@opencode-ai/plugin':'1.1.51'}}))
    tools=config/'tools';tools.mkdir(exist_ok=True)
    shutil.copyfile('/opt/review/StructuredOutput.js',tools/'StructuredOutput.js')
    shutil.copyfile('/opt/review/review_result_schema.json',config/'schema.json')
    env['OPENCODE_CONFIG_DIR']=str(config)


def result_from_messages(messages, session_id):
    found=[]
    if not isinstance(messages,list):raise ValueError('旧版会话消息结构无效')
    for message in messages:
        info=message.get('info',{})
        if info.get('sessionID')!=session_id or info.get('role')!='assistant':continue
        if info.get('error'):raise ValueError('旧版会话返回错误')
        for part in message.get('parts',[]):
            state=part.get('state',{})
            if part.get('type')=='tool' and part.get('tool')=='StructuredOutput' and state.get('status')=='completed':
                if state.get('output')!='管理器已接收结构化结果。':raise ValueError('结果工具回执无效')
                found.append(state.get('input'))
    if len(found)!=1:raise ValueError('旧版会话必须且只能提交一次结构化结果')
    return found[0]

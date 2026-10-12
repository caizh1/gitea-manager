"""真实固定 OpenCode 二进制＋协议替身，验证自主读取范围与额度收尾；不评估模型质量。"""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'review-runtime'))
from native_session import SessionRun
import legacy_adapter
from native_gateway import NativeGateway
allowed = NativeGateway._allowed_tool
def checked_tool(self, tool):
    result = allowed(self, tool)
    if not result and tool.get('function',{}).get('name') == 'StructuredOutput':
        print(json.dumps({'工具声明':tool},ensure_ascii=False),flush=True)
    return result
NativeGateway._allowed_tool = checked_tool
original_result = legacy_adapter.result_from_messages
def checked_result(messages, session_id):
    try: return original_result(messages, session_id)
    except Exception:
        print(json.dumps({'说明':'隔离固定样本会话失败证据','消息':messages},ensure_ascii=False),flush=True)
        raise
legacy_adapter.result_from_messages = checked_result


def main():
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder); workspace=root/'workspace';workspace.mkdir()
        (workspace/'sample.c').write_text(''.join(f'int value_{i} = {i};\n' for i in range(1,501)))
        calls=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path=='/events':
                    rows=body if isinstance(body,list) else [body]
                    payload=json.dumps({'ok':True,'inserted':len(rows),'session_id':rows[-1]['session_id'],'acked_seq':rows[-1]['seq']}).encode();kind='application/json'
                else:
                    schema=json.loads(Path(legacy_adapter.__file__).with_name('review_result_schema.json').read_text())
                    for tool in body.get('tools',[]):
                        if tool['function']['name']=='StructuredOutput':
                            assert tool['function']['parameters']==schema, '提交到后台的 Schema 必须规范化'
                            assert body.get('tool_choice')=='required', '提交到后台的工具选择必须受控'
                    roles = [message['role'] for message in body['messages']]
                    first_dialogue = next((i for i, role in enumerate(roles) if role != 'system'), len(roles))
                    assert roles.count('system') <= 1 and 'system' not in roles[first_dialogue:], '只允许首条消息为系统消息'
                    calls.append(body)
                    name='read' if len(calls)==1 else 'StructuredOutput'
                    args={'filePath':str(workspace/'sample.c'),'limit':2000} if name=='read' else {'coverage':{'reviewed_paths':['sample.c'],'skipped':[]},'findings':[]}
                    def frame(delta,finish=None):
                        return ('data: '+json.dumps({'id':'chatcmpl-local','object':'chat.completion.chunk','created':int(time.time()),'model':'fixture','choices':[{'index':0,'delta':delta,'finish_reason':finish}]})+'\n\n').encode()
                    payload=frame({'role':'assistant','tool_calls':[{'index':0,'id':'call_'+str(len(calls)),'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]})+frame({},'tool_calls')+b'data: [DONE]\n\n';kind='text/event-stream'
                    if len(calls)>2: payload=frame({'role':'assistant','content':'审查结果已提交。'})+frame({},'stop')+b'data: [DONE]\n\n'
                self.send_response(200);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();url=f'http://127.0.0.1:{server.server_port}'
        try:
            os.environ['AI_REVIEW_EVENT_URL']=url+'/events'
            runtime=root/'runtime';runtime.mkdir()
            data={'job_id':'job_thinking_binary_12345','session_id':'session_thinking_binary_12345','model':'fixture','deadline_at':time.time()+120,'event_url':url+'/events','event_token':'isolated-event','capability':'isolated-capability','prompt':'读取 sample.c，然后提交结构化审查结果。','context_retrieval_budget':1,'thinking_protocol':'deepseek','thinking_enabled':True}
            run=SessionRun(data,workspace,runtime,url+'/v1');run.run()
            assert run.status=='completed',(run.status,run.error_code,run.diagnostic)
            assert len(calls) in (2,3),len(calls)
            assert [x['function']['name'] for x in calls[1]['tools']]==['StructuredOutput']
            tooltext=''.join(str(x.get('content','')) for x in calls[1]['messages'] if x['role']=='tool')
            assert 'value_500' in tooltext, '模型请求 2000 行时应读完 500 行样本，不得被网关缩为 200 行'
            print(json.dumps({'说明':'真实 OpenCode 二进制与协议替身，不代表真实模型质量','结果':'通过','模型调用次数':len(calls),'读取样本行数':500,'额度耗尽仅保留结果工具':True},ensure_ascii=False))
        finally:server.shutdown();server.server_close()

if __name__=='__main__':main()

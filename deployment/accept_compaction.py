"""真实 OpenCode 二进制与受控用量响应：验证 210K 触发压缩，不代表真实模型效果。"""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'review-runtime'))
from native_session import SessionRun, VERSION


def main():
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder);workspace=root/'workspace';workspace.mkdir()
        (workspace/'sample.c').write_text(''.join(f'int value_{i} = {i};\n' for i in range(1,501)))
        calls=[];events=[];errors=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path=='/events':
                    rows=body if isinstance(body,list) else [body];events.extend(rows)
                    payload=json.dumps({'ok':True,'inserted':len(rows),'session_id':rows[-1]['session_id'],'acked_seq':rows[-1]['seq']}).encode();kind='application/json'
                else:
                    tools=[t['function']['name'] for t in body.get('tools',[])]
                    calls.append({'工具':tools,'消息数':len(body['messages'])});n=len(calls)
                    def frame(delta,finish=None,usage=None):
                        value={'id':'chatcmpl-fixture','object':'chat.completion.chunk','created':int(time.time()),'model':'fixture','choices':[{'index':0,'delta':delta,'finish_reason':finish}]}
                        if usage:value['usage']=usage
                        return ('data: '+json.dumps(value)+'\n\n').encode()
                    if n<=2:
                        if 'read' not in tools: errors.append('未到阈值就压缩或失去读取权限')
                        args={'filePath':str(workspace/'sample.c'),'limit':2000}
                        delta={'role':'assistant','tool_calls':[{'index':0,'id':'call_'+str(n),'type':'function','function':{'name':'read','arguments':json.dumps(args)}}]}
                        total=209999 if n==1 else 210000
                        payload=frame(delta)+frame({},'tool_calls',{'prompt_tokens':total-1,'completion_tokens':1,'total_tokens':total})
                        if n==2:
                            text=''.join(str(m.get('content','')) for m in body['messages'] if m['role']=='tool')
                            if 'value_500' not in text:errors.append('网关仍截断了模型请求的读取范围')
                    elif n==3:
                        if tools:errors.append('达到阈值后未进入无工具的压缩请求')
                        payload=frame({'role':'assistant','content':'已读取 sample.c 的全部 500 行，未发现已确认的问题。接下来通过 StructuredOutput 提交覆盖该文件的空意见结果。'})+frame({},'stop',{'prompt_tokens':1000,'completion_tokens':40,'total_tokens':1040})
                    else:
                        if n>5:errors.append('结果提交后仍在重复调用')
                        result={'coverage':{'reviewed_paths':['sample.c'],'skipped':[]},'findings':[]}
                        delta={'role':'assistant','tool_calls':[{'index':0,'id':'result_'+str(n),'type':'function','function':{'name':'StructuredOutput','arguments':json.dumps(result)}}]}
                        payload=frame(delta)+frame({},'tool_calls',{'prompt_tokens':1500,'completion_tokens':50,'total_tokens':1550})
                    payload+=b'data: [DONE]\n\n';kind='text/event-stream'
                self.send_response(200);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start();url=f'http://127.0.0.1:{server.server_port}'
        try:
            os.environ['AI_REVIEW_EVENT_URL']=url+'/events'
            runtime=root/'runtime';runtime.mkdir()
            data={'job_id':'job_compaction_12345678','session_id':'session_compaction_12345678',
                  'model':'fixture','deadline_at':time.time()+120,'event_url':url+'/events',
                  'event_token':'isolated-event','capability':'isolated-capability','prompt':'读取 sample.c，然后提交结构化审核结果。',
                  'context_retrieval_budget':20,'model_context_tokens':262144,'compaction_threshold_tokens':210000}
            run=SessionRun(data,workspace,runtime,url+'/v1');run.run()
            compactions=[e for e in events if e['type']=='session.compaction']
            report={'说明':'真实固定二进制，模型和 Token 用量为协议替身；不代表内网模型验收',
                    '执行器版本':VERSION,'会话状态':run.status,'错误代码':run.error_code,
                    '模型调用':calls,'压缩事件':[{'状态':e['status'],'提示':e['summary']} for e in compactions],
                    '失败':errors}
            print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
            assert run.status=='completed',(run.error_code,run.diagnostic)
            assert not errors,errors
            assert [e['status'] for e in compactions]==['running','completed'],compactions
            assert run.public()['result']['coverage']['reviewed_paths']==['sample.c']
        finally:server.shutdown();server.server_close()

if __name__=='__main__':main()

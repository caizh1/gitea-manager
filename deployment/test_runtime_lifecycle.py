"""真实二进制的取消与超时验证，模型端是隔离协议替身。"""
import json
import os
import sys
from pathlib import Path
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'review-runtime'))
from native_session import SessionRun


def check(kind):
    called=threading.Event();finished=threading.Event();events=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path=='/events':
                rows=body if isinstance(body,list) else [body];events.extend(rows)
                payload=json.dumps({'ok':True,'session_id':rows[-1]['session_id'],'acked_seq':rows[-1]['seq']}).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
            else:
                called.set();finished.wait(25)
                self.send_response(503);self.send_header('Content-Length','0');self.end_headers()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);workspace=root/'workspace';workspace.mkdir();runtime=root/'runtime';runtime.mkdir()
            (workspace/'sample.c').write_text('int main(void) {return 0;}\n')
            url=f'http://127.0.0.1:{server.server_port}'
            os.environ['AI_REVIEW_EVENT_URL']=url+'/events'
            data={'job_id':'job_lifecycle_'+kind,'session_id':'session_lifecycle_'+kind,'model':'fixture','deadline_at':time.time()+18,
                  'event_url':url+'/events','event_token':'isolated-event','capability':'isolated-capability','prompt':'检查 sample.c 并提交结果。'}
            row=SessionRun(data,workspace,runtime,url+'/v1')
            worker=threading.Thread(target=row.run);worker.start()
            if not called.wait(12):raise RuntimeError('样本未进入模型请求阶段')
            if kind=='cancel':row.abort()
            worker.join(24)
            if worker.is_alive():raise RuntimeError('取消或超时未在上限内回收')
            if row.status not in ('aborted','failed'):raise RuntimeError('未关闭失败发布门禁：'+row.status)
            if row.process is not None and row.process.poll() is None:raise RuntimeError('OpenCode 子进程未回收')
            if not events:raise RuntimeError('进度事件未产生')
            return {'场景':kind,'状态':row.status,'错误编号':row.error_code,'事件数':len(events),'进程已回收':True}
    finally:
        finished.set();server.shutdown();server.server_close()

if __name__=='__main__':print(json.dumps([check('cancel'),check('timeout')],ensure_ascii=False))

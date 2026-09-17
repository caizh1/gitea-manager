#!/usr/bin/env python3
"""隔离原生 API 同步点：只记录请求路径与阶段，不记录身份凭据或业务正文。"""
import json
from pathlib import Path
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

ROOT=Path('/tmp/审批读取同步点')
LOCK=threading.Lock()
COUNTS={}


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass

    def do_GET(self):
        config=json.loads((ROOT/'配置.json').read_text())
        path=self.path.split('?')[0]
        key=config.get('标识','')
        pause=False
        if key and path==config.get('路径'):
            with LOCK:
                COUNTS[key]=COUNTS.get(key,0)+1
                pause=COUNTS[key]==config.get('第几次',1)
        req=Request('http://gitea:3000'+self.path,headers={'Authorization':self.headers.get('Authorization','')})
        try:response=build_opener(ProxyHandler({})).open(req,timeout=8)
        except HTTPError as exc:response=exc
        with response:
            body=response.read(4*1024*1024)
            if pause:
                (ROOT/(key+'.到达')).write_text(json.dumps({'路径':path,'阶段':'原生响应已读取，尚未返回 Manager'},ensure_ascii=False))
                deadline=time.monotonic()+6
                while not (ROOT/(key+'.继续')).exists() and time.monotonic()<deadline:time.sleep(.005)
                if not (ROOT/(key+'.继续')).exists():
                    self.send_error(504,'隔离同步点未收到继续信号');return
            self.send_response(response.code)
            for header in ('Content-Type','X-Total-Count'):
                if response.headers.get(header):self.send_header(header,response.headers[header])
            self.send_header('Content-Length',str(len(body)));self.end_headers()
            try:self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):pass


if __name__=='__main__':
    ROOT.mkdir(exist_ok=True)
    ThreadingHTTPServer(('127.0.0.1',18765),Handler).serve_forever()

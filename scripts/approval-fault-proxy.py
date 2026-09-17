#!/usr/bin/env python3
"""只用于独立容器的原生 API 故障代理；不记录请求头、凭据或业务正文。"""
import json
from pathlib import Path
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

CONTROL=Path('/tmp/审批故障代理.json')


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass

    def do_GET(self):
        control=json.loads(CONTROL.read_text())
        mode=control.get('故障','正常')
        affected=self.path.split('?')[0].endswith('/reviews')
        if affected and mode!='正常':
            if mode=='超时':time.sleep(4)
            if mode in ('403','404','429','500'):
                code=int(mode);body=b'{}'
            elif mode=='损坏JSON':code=200;body=b'{'
            elif mode=='重定向':code=302;body=b''
            else:code=200;body=b'[]'
            self.send_response(code)
            if code==302:self.send_header('Location','http://127.0.0.1:1/blocked-target')
            self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            try:self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):pass
            return
        req=Request('http://gitea:3000'+self.path,headers={'Authorization':self.headers.get('Authorization','')})
        try:response=build_opener(ProxyHandler({})).open(req,timeout=10)
        except HTTPError as exc:response=exc
        with response:
            content=response.read(4*1024*1024)
            self.send_response(response.code)
            for key in ('Content-Type','X-Total-Count'):
                if response.headers.get(key):self.send_header(key,response.headers[key])
            self.send_header('Content-Length',str(len(content)));self.end_headers()
            try:self.wfile.write(content)
            except (BrokenPipeError,ConnectionResetError):pass


if __name__=='__main__':
    ThreadingHTTPServer(('127.0.0.1',18765),Handler).serve_forever()

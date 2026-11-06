"""仅供独立验收：在真实封存响应返回应用前终止执行器并记录无凭据请求路径。"""
import os
from pathlib import Path
import json
from urllib.parse import urlsplit
import requests

original=requests.Session.request
log=Path('/app/data/seal-fault-events.jsonl')
marker=Path('/app/data/seal-fault-once')
second=Path('/app/data/seal-fault-recovery')
def request(self, method, url, *args, **kwargs):
    response=original(self,method,url,*args,**kwargs)
    path=urlsplit(url).path
    if '/governance/migration-' in path:
        with log.open('a') as stream:
            stream.write(json.dumps({'方法':method,'路径':path,'状态':response.status_code},ensure_ascii=False)+'\n')
    if method=='POST' and path.endswith('/seal') and response.status_code==200 and not marker.exists():
        marker.write_text(path)
        os._exit(91)
    if method=='GET' and path.endswith('/manifest') and response.status_code==200 and marker.exists() and not second.exists():
        second.write_text('恢复回读已完成，再次中断')
        os._exit(92)
    return response
requests.Session.request=request

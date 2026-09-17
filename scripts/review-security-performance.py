#!/usr/bin/env python3
"""本机执行器真实隔离检查、HMAC/去重及相同负载下的响应时间测量。"""
import concurrent.futures
import hashlib
import hmac
import json
import math
from pathlib import Path
import runpy
import statistics
import time
import threading
import urllib.error
import urllib.request

local=runpy.run_path(str(Path(__file__).with_name('review-local.py')))
h=local['Harness']();h.login();project=h.values.get('REVIEW_PROJECT','gitea-review-local')
out=local['STATE']/('隔离与性能验收-'+str(int(time.time()))+'.json');report={'说明':'真实容器与 HTTP 测量，模型为本机协议替身。'}
if h.manager('GET','/ai-review/config')['config']['model']!='review-fixture':raise RuntimeError('性能测试只能使用独立协议替身')

checks='''import socket,json,os
from pathlib import Path
result={}
for path in ['/app/review/review.db','/app/data/gitea-manager.db','/var/run/docker.sock','/home/appuser/.ssh/id_rsa']:
 result[path]=not Path(path).exists()
try:
 Path('/opt/review/只读验证').write_text('测试写入应被拒绝')
 result['根文件系统只读']=False
except OSError:result['根文件系统只读']=True
for host,label in [('1.1.1.1','公网 TCP 被阻止'),('api.deepseek.com','公网域名连接被阻止')]:
 try:
  conn=socket.create_connection((host,443),timeout=2);conn.close();result[label]=False
 except OSError:result[label]=True
try:socket.getaddrinfo('api.deepseek.com',443);result['外部 DNS 被阻止']=False
except OSError:result['外部 DNS 被阻止']=True
result['内部网关可解析']=bool(socket.getaddrinfo('review-worker',5051))
result['非 root 运行']=os.getuid()!=0
print(json.dumps(result,ensure_ascii=False))
'''
raw=local['run'](['docker','exec','-i',project+'-review-executor-1','python','-'],input=checks)
report['执行器隔离']=json.loads(raw)

# 只读取专用于本机测试仓库的 HMAC 密钥；不写入报告或进程参数。
repo=h.create_repo('performance-fixtures')
secret_script=f"from ai_review.store import Store\ns=Store();print(s.unseal(s.repo({repo['id']})['webhook_secret']))"
secret=local['run'](['docker','exec','-i',project+'-review-worker-1','python','-'],input=secret_script).strip()
payload={'action':'closed','repository':{'id':repo['repo_id'],'full_name':repo['repo_name']},'pull_request':{
    'number':2147483000,'head':{'sha':'a'*40},'base':{'sha':'b'*40,'repo':{'id':repo['repo_id']}}}}
body=json.dumps(payload).encode();signature=hmac.new(secret.encode(),body,hashlib.sha256).hexdigest();del secret
url=h.manager_url+f'/api/ai-review/webhooks/{repo["id"]}'
def event(i, bad=False):
    req=urllib.request.Request(url,data=body,headers={'Content-Type':'application/json','X-Gitea-Signature':'0'*64 if bad else signature,
        'X-Gitea-Event':'pull_request','X-Gitea-Delivery':'performance-same-event'},method='POST')
    start=time.perf_counter()
    try:
        with urllib.request.urlopen(req,timeout=5) as response:return response.status,time.perf_counter()-start,json.load(response)
    except urllib.error.HTTPError as exc:return exc.code,time.perf_counter()-start,{}
values=[event(i) for i in range(100)]
def p95(rows):return sorted(rows)[math.ceil(len(rows)*.95)-1]
report['Webhook']={'次数':len(values),'P95毫秒':round(p95([r[1] for r in values])*1000,2),
                    '均为202':all(r[0]==202 for r in values),'错误签名被拒绝':event(0,True)[0]==403,
                    '重复关闭事件没有创建任务':all(not r[2].get('job') for r in values)}

def measure():
    values=[]
    for _ in range(100):
        start=time.perf_counter();h.manager('GET','/ai-review/tasks');values.append(time.perf_counter()-start)
    return p95(values)*1000
stop=threading.Event()
def traffic():
    while not stop.wait(.1):event(0)
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    future=pool.submit(traffic)
    try:
        for _ in range(20):h.manager('GET','/ai-review/tasks')
        baseline=[measure() for _ in range(3)]
        boundaries=runpy.run_path(str(Path(__file__).with_name('review-boundary-acceptance.py')))
        suite=boundaries['Suite']('performance-code-fixtures');suite.prepare();suite.control(delay=20)
        pr=suite.branch('性能测量',{'performance.c':b'int performance(void) {\n    return 1; /* REVIEW_TEST_TARGET */\n}\n'})
        for _ in range(100):
            rows=h.manager('GET',f'/ai-review/tasks?repository={suite.repo["id"]}')['items']
            if any(r['status']=='running' and r['stage'].startswith('分析') for r in rows):break
            time.sleep(.1)
        else:raise RuntimeError('没有实际运行的 AI 任务，不能测量模型执行期间响应退化')
        loaded=[measure() for _ in range(3)]
        suite.control()
    finally:
        stop.set();future.result()
idle=statistics.median(baseline);load=statistics.median(loaded)
report['Manager响应']={'端点':'/api/ai-review/tasks','每组请求数':100,'各组基线P95毫秒':baseline,'各组事件负载P95毫秒':loaded,
                      '中位P95退化百分比':round((load/idle-1)*100,2),'说明':'两组均保持每秒约 10 次相同事件请求；第二组同时运行真实 Worker 和 OpenCode。不代表生产容量。'}
out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(report,ensure_ascii=False,indent=2));print('验收记录：'+str(out))

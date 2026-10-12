"""安装选择与执行器启动缓存共同决定门禁；健康读取不会启动 OpenCode。"""
import json
import hashlib
import os
from pathlib import Path
import time


def diagnostic_root():
    return Path(os.environ.get('MANAGEMENT_DIAGNOSTICS','/app/data/diagnostics'))


def live_path():
    return Path(os.environ.get('AI_REVIEW_DB', '/app/review/review.db')).parent/'executor-live-status.json'


def status():
    value=os.environ.get('AI_REVIEW_RUNTIME_STATUS')
    if not value:return {'ready':True,'reason':'未启用安装器运行时门禁'}
    try: installed=json.loads(Path(value).read_text())
    except (OSError,ValueError):return {'ready':False,'reason':'安装执行器状态不可读取'}
    if not installed.get('ready'):return installed
    # 旧安装证据不能冒充本次无响应或尚未完成的启动检查。
    metadata={k:v for k,v in installed.items() if k not in ('checks','process','cpu','binary_sha256')}
    try:live=json.loads(live_path().read_text())
    except (OSError,ValueError):live={}
    if time.time()-live.get('checked_at',0)>30:
        return {**metadata,'ready':False,'reason':'等待后台确认执行器启动自检状态'}
    return {**metadata,**live}


def available():
    return status().get('ready') is True


def refresh():
    if not os.environ.get('AI_REVIEW_RUNTIME_STATUS'):return
    path=live_path()
    try:
        if time.time()-path.stat().st_mtime<10:return
    except OSError:pass
    import requests
    from services.diagnostics import private_write, Report
    try:
        with requests.Session() as session:
            session.trust_env=False
            response=session.get(os.environ.get('AI_REVIEW_EXECUTOR_URL','http://review-executor:5052')+'/health',timeout=(2,3),allow_redirects=False)
            body=response.json()
            value={key:body[key] for key in ('version','mode','checks','binary_sha256','cpu','process') if key in body}
            value.update(ready=response.status_code==200 and body.get('ready') is True, reason=body.get('reason') or body.get('error') or ('执行器启动自检通过' if response.status_code==200 else '执行器未就绪，HTTP '+str(response.status_code)), http_status=response.status_code)
    except (requests.RequestException,ValueError) as exc:
        value={'ready':False,'reason':'执行器健康读取失败：'+type(exc).__name__,'error':str(exc)}
    signature=hashlib.sha256(json.dumps({k:v for k,v in value.items() if k!='error'},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    try: previous=json.loads(path.read_text())
    except (OSError,ValueError): previous={}
    diagnostic_id=previous.get('diagnostic_id') if previous.get('signature')==signature else None
    if not diagnostic_id and value.get('reason') != '执行器启动自检中':
        report=Report(diagnostic_root(),'执行器启动与运行诊断')
        report.step='执行器健康与启动自检'
        report.write('runtime-probes.json',value)
        for check in value.get('checks',[]):
            report.event('自检详情',check.get('步骤','启动自检'),详情=check)
        if not value['ready']: report.fail(RuntimeError(value['reason']))
        report.finish('执行器就绪' if value['ready'] else 'AI 不可用，核心功能保留')
        diagnostic_id=report.id
    private_write(path,{**value,'signature':signature,'diagnostic_id':diagnostic_id,'checked_at':time.time()})

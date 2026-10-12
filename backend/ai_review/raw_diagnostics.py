"""管理员原始诊断状态与两跳受限下载；不接受客户端文件路径。"""
import hashlib
import hmac
import json
import re
import time

import requests
from flask import Response, jsonify, stream_with_context

from .common import ReviewError, secret_file

IDENT = re.compile(r'^[A-Za-z0-9_-]{8,160}$')
MAX_ARCHIVE_BYTES = 260 * 1024 * 1024  # 256 MiB 原始证据加 ZIP 固定开销
WORKER_URL = 'http://review-worker:5051'


def metadata(value, job_id, session_id):
    """只保存 runtime 产生的有限元数据，不保存或改写原始证据内容。"""
    if not isinstance(value, dict):
        return None
    if value.get('job_id') not in (None, job_id) or value.get('session_id') not in (None, session_id):
        return None
    result = {'available': value.get('available') is True, 'truncated': value.get('truncated') is True,
              'job_id': job_id, 'session_id': session_id}
    status = value.get('status')
    if isinstance(status,str) and status in {'capturing','ready','archiving','available','partial','unavailable','expired','error'}:
        result['status'] = status
    for key in ('bytes', 'original_bytes', 'collected_files', 'omitted_files'):
        item = value.get(key)
        if type(item) is int and 0 <= item <= 2**63-1:
            result[key] = item
    expires = value.get('retention_expires_at')
    if isinstance(expires, (int, float)) and not isinstance(expires, bool) and 0 <= expires <= 10**11:
        result['retention_expires_at'] = expires
    for key in ('error', 'partial_reason'):
        item = value.get(key)
        if isinstance(item, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}', item):
            result[key] = item
    sha256 = value.get('sha256')
    if isinstance(sha256, str) and re.fullmatch(r'[0-9a-f]{64}', sha256):
        result['sha256'] = sha256
    return result


def _proxy_key():
    return hmac.new(secret_file('AI_REVIEW_MASTER_KEY').encode(), b'raw-diagnostics-proxy-v1',
                    hashlib.sha256).digest()


def signature(action, job_id, session_id, timestamp):
    canonical = '\n'.join((action, job_id, session_id, str(timestamp))).encode()
    return hmac.new(_proxy_key(), canonical, hashlib.sha256).hexdigest()


def verify_proxy(action, job_id, session_id, timestamp, supplied):
    if (not IDENT.fullmatch(job_id) or not IDENT.fullmatch(session_id)
            or not isinstance(timestamp,str) or not re.fullmatch(r'[0-9]{1,12}',timestamp)
            or not isinstance(supplied,str) or not re.fullmatch(r'[0-9a-f]{64}',supplied)
            or abs(time.time() - int(timestamp)) > 30):
        return False
    return hmac.compare_digest(signature(action, job_id, session_id, int(timestamp)), supplied)


def _worker_request(job, head=False):
    session_id = job.get('session_id') or ''
    if not IDENT.fullmatch(job['id']) or not IDENT.fullmatch(session_id):
        raise ReviewError('该任务没有可关联的原始诊断会话')
    timestamp = int(time.time())
    action = 'head' if head else 'archive'
    url = f"{WORKER_URL}/internal/raw-diagnostics/{job['id']}/{session_id}/archive"
    client = requests.Session(); client.trust_env = False
    try:
        response = client.request('HEAD' if head else 'GET',url,
            headers={'X-Raw-Diagnostics-Time': str(timestamp),
            'X-Raw-Diagnostics-Signature': signature(action, job['id'], session_id, timestamp)},
            stream=True, timeout=(5, 30), allow_redirects=False)
    except requests.RequestException:
        client.close()
        return jsonify(error='原始诊断下载代理不可用'), 503
    if response.status_code != 200:
        message=None;status=response.status_code
        try:
            body=response.raw.read(65537,decode_content=True)
            value=json.loads(body) if len(body)<=65536 else None
            message=value.get('error') if isinstance(value,dict) else None
        except (ValueError,TypeError,AttributeError,UnicodeError,requests.RequestException,OSError):
            pass
        finally:
            response.close();client.close()
        return jsonify(error=message or '原始诊断暂不可下载'), status
    if (response.headers.get('X-Diagnostics-Job-ID') != job['id']
            or response.headers.get('X-Diagnostics-Session-ID') != session_id):
        response.close();client.close()
        return jsonify(error='原始诊断代理身份不匹配'), 502
    try: length = int(response.headers.get('Content-Length', ''))
    except ValueError: length = -1
    if not 0 <= length <= MAX_ARCHIVE_BYTES:
        response.close()
        client.close()
        return jsonify(error='原始诊断包大小无效或超过上限'), 502
    name = f"ai-review-raw-diagnostics-{job['id']}.zip"
    if head:
        diagnostic_headers={key:value for key,value in response.headers.items()
                            if key.lower().startswith('x-diagnostics-')}
        response.close();client.close()
        diagnostic_headers.update({
            'Content-Disposition':f'attachment; filename="{name}"','Content-Length':str(length),
            'Content-Type':'application/zip','Cache-Control':'no-store'})
        result=Response('',headers=diagnostic_headers)
        result.headers['Content-Length']=str(length)
        return result

    def chunks():
        sent = 0
        try:
            for chunk in response.iter_content(64 * 1024):
                sent += len(chunk)
                if sent > MAX_ARCHIVE_BYTES:
                    raise RuntimeError('原始诊断包超过代理上限')
                yield chunk
        finally:
            response.close()
            client.close()
    headers={key:value for key,value in response.headers.items()
             if key.lower().startswith('x-diagnostics-')}
    headers.update({'Content-Disposition':f'attachment; filename="{name}"',
        'Content-Length':str(length),'Content-Type':'application/zip',
        'X-Content-Type-Options':'nosniff','Cache-Control':'no-store'})
    result=Response(chunks(),headers=headers)
    result.call_on_close(lambda:(response.close(),client.close()))
    return result


def worker_download(job):
    return _worker_request(job)


def worker_head(job):
    return _worker_request(job, head=True)

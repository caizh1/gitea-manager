"""只记录数值耗时；阶段与阶段内明细分层展示，不改变任务预算或审查结果。"""
import contextlib
import functools
import logging
import math
import time

TARGET_SECONDS = 600


def migrate(c):
    c.execute('''CREATE TABLE IF NOT EXISTS review_performance (
        job TEXT NOT NULL, category TEXT NOT NULL, key TEXT NOT NULL,
        elapsed REAL NOT NULL, samples INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(job,category,key))''')


def record(store, job, category, key, elapsed):
    if not isinstance(elapsed,(int,float)) or not math.isfinite(elapsed) or elapsed<0:return
    try:
        with store.tx() as c:
            c.execute('''INSERT INTO review_performance VALUES (?,?,?,?,1)
                ON CONFLICT(job,category,key) DO UPDATE SET
                elapsed=elapsed+excluded.elapsed,samples=samples+1''',
                (job,category,key,elapsed))
    except Exception as exc:
        # 诊断写入失败不能覆盖原异常或把已经发布的任务标成失败；后续业务操作仍检查数据库。
        logging.warning('审查耗时记录未保存：任务=%s 类别=%s 异常=%s',job,category,type(exc).__name__)


@contextlib.contextmanager
def span(store, job, category, key=''):
    if category in ('preparation','analysis','publication'):record(store,job,'started:'+category,key,0)
    started=time.monotonic()
    try:yield
    finally:record(store,job,category,key,time.monotonic()-started)


def measured(category):
    def decorate(method):
        @functools.wraps(method)
        def wrapped(self,*args,**kwargs):
            with span(self.store,self.job['id'],category):return method(self,*args,**kwargs)
        return wrapped
    return decorate


def summary(store, job):
    with contextlib.closing(store.connect()) as c:
        rows=c.execute('SELECT category,key,elapsed,samples FROM review_performance WHERE job=? ORDER BY rowid',(job,)).fetchall()
    if not rows:return None
    totals={};calls={};samples={}
    for row in rows:
        category=row['category'];seconds=row['elapsed']
        totals[category]=totals.get(category,0)+seconds
        samples[category]=samples.get(category,0)+row['samples']
        if category in ('call','model','executor'):
            calls.setdefault(row['key'],{'call_id':row['key']})[category+'_seconds']=round(seconds,3)
    # 模型耗时嵌套在执行器等待内；不能将两者相加当作任务总时间。
    for call in calls.values():
        if 'call_seconds' in call and 'model_seconds' in call:
            call['overhead_seconds']=round(max(0,call['call_seconds']-call['model_seconds']),3)
    incomplete=[k for k in ('preparation','analysis','publication') if samples.get('started:'+k,0)>samples.get(k,0)]
    stages={k:round(totals[k],3) if k in totals and k not in incomplete else None for k in ('preparation','analysis','publication')}
    detail={k:round(totals[k],3) if k in totals else None for k in ('retrieval','call','model','executor')}
    complete=all('call_seconds' in c and 'model_seconds' in c for c in calls.values())
    detail['overhead']=round(sum(c['overhead_seconds'] for c in calls.values()),3) if calls and complete else None
    return {'stages_seconds':stages,'analysis_detail_seconds':detail,'calls':list(calls.values()),
            'recorded_calls':len(calls),'call_breakdown_complete':complete,'incomplete_stages':incomplete}

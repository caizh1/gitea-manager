"""仅由隔离容量执行器加载的计时探针；不随生产入口加载，不记录凭据或业务正文。"""
import json
import time
from urllib.parse import urlsplit

from flask import g,has_request_context
import requests
from sqlalchemy import event

from approval.view_app import create_app as application
from models import db
import routes.approval_routes as routes


def create_app():
    app=application()
    original_send=requests.Session.send
    original_status=routes.status
    original_display=routes.display_status

    def send(self,request,**kwargs):
        started=time.monotonic()
        try:return original_send(self,request,**kwargs)
        finally:
            if has_request_context() and hasattr(g,'probe'):
                # 路径只作接口分类，不记录查询字符串、Cookie、Token、用户名或原始 URL。
                path=urlsplit(request.url).path
                category='原生API' if '/api/v1/' in path else ('会话身份' if path.endswith('/user/settings') else '原生PR权限')
                item=g.probe['下游'].setdefault(category,{'次数':0,'秒':0})
                item['次数']+=1;item['秒']+=time.monotonic()-started
    requests.Session.send=send

    def timed(target,label):
        def invoke(*args,**kwargs):
            started=time.monotonic()
            try:return target(*args,**kwargs)
            finally:
                if has_request_context() and hasattr(g,'probe'):
                    g.probe[label]=round(time.monotonic()-started,4)
        return invoke
    routes.status=timed(original_status,'完整计算秒')
    routes.display_status=timed(original_display,'展示等待及计算秒')

    with app.app_context():
        @event.listens_for(db.engine,'before_cursor_execute')
        def before_query(conn,cursor,statement,parameters,context,executemany):
            context.probe_start=time.monotonic()

        @event.listens_for(db.engine,'after_cursor_execute')
        def after_query(conn,cursor,statement,parameters,context,executemany):
            if has_request_context() and hasattr(g,'probe'):
                g.probe['数据库次数']+=1
                g.probe['数据库秒']+=time.monotonic()-context.probe_start

    @app.before_request
    def begin():
        g.probe={'类型':'隔离展示计时','进入时间':time.time(),'下游':{},'数据库次数':0,'数据库秒':0}
        g.probe_start=time.monotonic()

    @app.after_request
    def finish(response):
        if hasattr(g,'probe'):
            g.probe.update(HTTP=response.status_code,进程内秒=round(time.monotonic()-g.probe_start,4))
            print(json.dumps(g.probe,ensure_ascii=False),flush=True)
        return response
    return app

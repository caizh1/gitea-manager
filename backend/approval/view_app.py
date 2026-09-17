"""独立展示服务入口，只注册原生会话核验后的只读展示路由；不启动 Manager 后台任务。"""
from flask import Flask,jsonify,request
from sqlalchemy import text

from config import DATABASE_URL
from models import db


def create_app():
    app=Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI=DATABASE_URL,SQLALCHEMY_TRACK_MODIFICATIONS=False,
                      SQLALCHEMY_ENGINE_OPTIONS={'connect_args':{'timeout':1}})
    db.init_app(app)
    # 表结构仅由主 Manager 初始化；本进程不创建默认管理员或运行迁移/后台任务。
    from routes.approval_routes import approval_view_bp
    app.register_blueprint(approval_view_bp,url_prefix='/api/approval-view')

    @app.before_request
    def maintenance():
        from services.upgrade_maintenance import active
        if active() and request.path!='/health':
            return jsonify(error='正在升级并核对数据，请稍后访问'),503

    @app.get('/health')
    def health():
        db.session.execute(text('SELECT policy_id FROM approval_view_cache LIMIT 1'))
        return jsonify(ok=True,role='approval-view')

    return app

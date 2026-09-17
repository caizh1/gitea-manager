"""用进程互斥保护 SQLite 初始化，避免多个 Gunicorn worker 同时建表。"""
import fcntl
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from sqlalchemy.engine import make_url


@contextmanager
def database_startup_lock(app):
    url = make_url(app.config['SQLALCHEMY_DATABASE_URI'])
    if url.get_backend_name() != 'sqlite' or not url.database or url.database == ':memory:':
        yield
        return
    folder = Path(url.database).resolve().parent
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / '.migration.lock', 'a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            # 不支持可靠 flock 的共享目录不能提供双 worker 执行保障；启动时明确阻断。
            with tempfile.NamedTemporaryFile(dir=folder, prefix='.lock-check-') as probe:
                fcntl.flock(probe, fcntl.LOCK_EX)
                check = 'import fcntl,sys\nf=open(sys.argv[1],"a")\ntry:\n fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)\nexcept BlockingIOError:\n sys.exit(0)\nsys.exit(1)'
                result = subprocess.run([sys.executable, '-c', check, probe.name], timeout=10, capture_output=True)
                if result.returncode != 0:
                    raise RuntimeError('数据库目录不支持可靠的进程文件锁。Docker Desktop 请使用 compose.local.yml 的原生持久卷。')
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)

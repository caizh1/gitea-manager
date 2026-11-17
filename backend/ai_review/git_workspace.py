"""复用原生 Git 工作目录，固定版本证据按需读取。"""

import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
from urllib.parse import quote

from .common import ReviewError, safe_path, valid_sha
from .git_cache import cache_key, cache_root, run_git, write_json, _size, git_credentials


def repository_root(repo):
    from .native_workspace import WORKSPACE_ROOT

    return WORKSPACE_ROOT / ".repositories" / cache_key(repo)


def object_signature(folder):
    """检查对象文件的元数据变化，不在热路径重复读取全部对象内容。"""
    rows = []
    for path in (folder / "objects").rglob("*"):
        if path.is_file():
            stat = path.stat()
            rows.append((str(path.relative_to(folder)), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    return hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest()


def read_blob(workspace, sha, path):
    safe_path(path)
    return run_git(["show", valid_sha(sha) + ":" + path], cwd=workspace)


def release_previous(store, root):
    marker = root / "active.json"
    if not marker.exists():
        return
    previous = json.loads(marker.read_text())
    old = store.job(previous["job"], evidence=False)
    if old is None:
        raise ReviewError("缺少上次 Git 工作目录任务记录，无法核对执行器状态")
    if not old.get("session_id"):
        return
    import contextlib
    import requests
    from .native_review import cancel_session, _request, _response

    with contextlib.closing(requests.Session()) as session:
        session.trust_env = False
        try:
            response = _request(session, "GET", "/sessions/" + old["session_id"])
            if response.status_code == 200 and _response(response, 1024 * 1024).get("execution_stopped") is True:
                return
        except requests.RequestException:
            pass
    # 领取超时不能证明旧执行器已停止，切换共享目录前必须取得退出回执。
    for _ in range(20):
        status = cancel_session(old["id"], old["session_id"])
        if status.get("execution_stopped") is True:
            return
        time.sleep(0.1)
    raise ReviewError("上次审查执行器尚未确认停止，保留原 Git 工作目录")


def prepare(store, client, repo, job, snapshot):
    if snapshot.get('workspace_layout') != 'git-worktree-v2':
        return _prepare(store, client, repo, job, snapshot)
    deadline = time.monotonic() + store.job_config(job['id']).get('batch_preprocess_seconds', 120)
    while True:
        store.active(job['id'], job['owner'])
        try:
            with store.lock('git-workspace-' + cache_key(repo)):
                return _prepare(store, client, repo, job, snapshot)
        except WorkspaceBusy:
            raise
        except ReviewError as exc:
            if str(exc) != '该 PR 正在由其他执行者发布':
                raise
            if time.monotonic() >= deadline:
                raise ReviewError('等待 Git 缓存锁超过预处理时限') from None
            time.sleep(0.1)


class WorkspaceBusy(ReviewError):
    pass


def _prepare(store, client, repo, job, snapshot):
    from .authorization import authorize
    from .gitea import assert_version

    started = time.monotonic()
    deadline = started + store.job_config(job["id"]).get("batch_preprocess_seconds", 120)
    last_check = 0

    def check():
        nonlocal last_check
        now = time.monotonic()
        if now >= deadline:
            raise ReviewError("源码准备超过预处理时限")
        if now - last_check >= 0.5:
            store.active(job["id"], job["owner"])
            client.check_active()
            last_check = now

    authorize(repo, job)
    client.repository()
    root = repository_root(repo)
    root.mkdir(parents=True, exist_ok=True, mode=0o755)
    concurrent = snapshot.get('workspace_layout') == 'git-worktree-v2'
    if not concurrent or (root / 'active.json').exists():
        release_previous(store, root)
        if concurrent:
            (root / 'active.json').unlink(missing_ok=True)
    folder = root / 'objects.git'
    workspace = root / 'worktrees' / job['id'] if concurrent else root / 'worktree'
    workspace.parent.mkdir(exist_ok=True)
    if concurrent:
        store.update(job['id'], job['owner'], workspace_path=str(workspace), snapshot=snapshot)

    state_path = root / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    mode = "缓存复用"
    if not (folder / "HEAD").exists():
        legacy = cache_root(store) / cache_key(repo)
        if (legacy / "HEAD").exists():
            shutil.copytree(legacy, folder, dirs_exist_ok=True)
            mode = "缓存迁移"
        else:
            folder.mkdir(exist_ok=True, mode=0o755)
            run_git(["init", "--bare", "."], cwd=folder, check=check)
            mode = "首次获取"
    folder.chmod(0o755)
    signature = object_signature(folder)
    if state.get("objects") != signature:
        from .git_cache import GitFailure

        try:
            run_git(["fsck", "--full", "--no-dangling"], cwd=folder, check=check, timeout=max(1, deadline-time.monotonic()))
        except GitFailure:
            check()
            with contextlib.closing(store.connect()) as connection:
                other = connection.execute('SELECT 1 FROM review_execution_slots WHERE repository=? AND job!=? LIMIT 1', (repo['id'], job['id'])).fetchone()
            if other:
                raise WorkspaceBusy('Git 缓存损坏且其他任务仍占用，已保留现场，待其他任务结束后重试')
            shutil.rmtree(folder)
            shutil.rmtree(workspace, ignore_errors=True)
            folder.mkdir(mode=0o755)
            run_git(["init", "--bare", "."], cwd=folder, check=check)
            mode = "缓存重建"
    required = [valid_sha(snapshot[k]) for k in ("base", "head", "merge_base")]

    def exists(sha):
        from .git_cache import GitFailure

        try:
            run_git(["cat-file", "-e", sha + "^{commit}"], cwd=folder, check=check)
            return True
        except GitFailure:
            check()
            return False

    url = client.url + "/" + quote(repo["repo_name"], safe="/") + ".git"
    run_git(["config", "remote.origin.url", url], cwd=folder, check=check)
    fetch_seconds = 0
    if not all(exists(sha) for sha in required):
        fetch_started = time.monotonic()
        with git_credentials(store.unseal(repo["bot_token"])) as env:
            refs = ["+refs/heads/*:refs/remotes/origin/*", f'+refs/pull/{job["number"]}/head:refs/review/{job["number"]}']
            run_git(["-c", "fetch.fsckObjects=true", "fetch", "--no-tags", "origin", *refs], cwd=folder, env=env, check=check, timeout=max(1, deadline-time.monotonic()))
            for sha in required:
                if not exists(sha):
                    run_git(["-c", "fetch.fsckObjects=true", "fetch", "origin", sha], cwd=folder, env=env, check=check, timeout=max(1, deadline-time.monotonic()))
        fetch_seconds = time.monotonic() - fetch_started
        if mode == "缓存复用":
            mode = "增量更新"
    bases = run_git(["merge-base", "--all", required[0], required[1]], cwd=folder, check=check).decode().split()
    if required[2] not in bases:
        raise ReviewError("Gitea 比较基点与 Git 对象不一致")
    for index, sha in enumerate(required):
        run_git(["update-ref", f"refs/review-active/{job['id']}/{index}" if concurrent else f"refs/review-active/{index}", sha], cwd=folder, check=check)
    checkout_started = time.monotonic()
    # info/attributes 优先于仓库规则，保持工作目录文本与 Git blob 一致。
    attributes = folder / "info" / "attributes"
    attributes.parent.mkdir(exist_ok=True)
    attributes.write_text("* -text -filter -ident -working-tree-encoding -eol\n")
    run_git(["config", "core.symlinks", "false"], cwd=folder, check=check)
    run_git(["config", "core.autocrlf", "false"], cwd=folder, check=check)
    if not (workspace / ".git").is_file():
        shutil.rmtree(workspace, ignore_errors=True)
        run_git(["worktree", "prune"], cwd=folder, check=check)
        run_git(["worktree", "add", "--detach", str(workspace), required[1]], cwd=folder, check=check, timeout=max(1, deadline-time.monotonic()))
    else:
        dirty = run_git(["status", "--porcelain", "--untracked-files=normal"], cwd=workspace, check=check)
        if dirty:
            run_git(["reset", "--hard", "HEAD"], cwd=workspace, check=check)
            run_git(["clean", "-fd"], cwd=workspace, check=check)
        run_git(["checkout", "--detach", required[1]], cwd=workspace, check=check, timeout=max(1, deadline-time.monotonic()))
    # 运行时 uid 与 Worker 不同，仅记录本仓库的 safe.directory。
    run_git(["config", "safe.directory", str(workspace)], cwd=folder, check=check)
    entries = run_git(["ls-tree", "-rz", required[1]], cwd=folder, check=check).split(b"\0")
    unresolved = []
    for entry in entries:
        if not entry:
            continue
        metadata, path = entry.split(b"\t", 1)
        mode_bits = metadata.split()[0]
        if mode_bits in (b"160000", b"120000"):
            unresolved.append({"路径": path.decode("utf-8", "replace"), "类型": "子模块" if mode_bits == b"160000" else "链接", "原因": "保留 Git 记录，未获取外部内容"})
    assert_version(client, job["number"], snapshot)
    check()
    write_json(state_path, {"objects": object_signature(folder), "head": required[1]})
    marker = root / 'active' / (job['id'] + '.json') if concurrent else root / 'active.json'
    marker.parent.mkdir(exist_ok=True)
    write_json(marker, {'job': job['id'], 'snapshot_id': snapshot['id']})
    size = _size(root)
    if size > int(os.environ.get("AI_REVIEW_GIT_CACHE_BYTES", 8 * 1024**3)):
        raise ReviewError("Git 仓库与工作目录超过缓存容量")
    os.utime(root, None)
    prune(store, root)
    info = {"bytes": size, "snapshot_id": snapshot["id"], "mode": mode, "network_seconds": round(fetch_seconds, 3),
            "prepare_seconds": round(time.monotonic()-started-fetch_seconds, 3),
            "checkout_seconds": round(time.monotonic()-checkout_started, 3), "unresolved": unresolved}
    return str(workspace), info


def prune(store, protected):
    """按最近使用清理空闲仓库，持有审查锁的目录不可删除。"""
    limit = int(os.environ.get("AI_REVIEW_GIT_CACHE_BYTES", 8 * 1024**3))
    with store.lock("git-workspace-inventory", blocking=True):
        folders = sorted(protected.parent.iterdir(), key=lambda p: p.stat().st_mtime)
        sizes = {folder: _size(folder) for folder in folders if folder.is_dir()}
        total = sum(sizes.values())
        for folder, size in sizes.items():
            if total <= limit:
                break
            if folder == protected:
                continue
            try:
                with store.lock("git-workspace-" + folder.name):
                    if in_use(store, folder):
                        continue
                    release_previous(store, folder)
                    shutil.rmtree(folder)
                    total -= size
            except ReviewError:
                continue
        if total > limit:
            raise ReviewError("Git 仓库缓存达到总容量限制")


def in_use(store, folder):
    with contextlib.closing(store.connect()) as connection:
        rows = connection.execute('SELECT j.workspace_path FROM review_execution_slots s JOIN jobs j ON j.id=s.job').fetchall()
    return any(row[0] and Path(row[0]).is_relative_to(folder) for row in rows) or any((folder / 'active').glob('*.json'))


def release_task(store, job):
    """调用者已经核对执行停止并持有任务执行锁。"""
    snapshot = job.get('snapshot') or {}
    if snapshot.get('workspace_layout') != 'git-worktree-v2' or not job.get('workspace_path'):
        return
    repo = store.repo(job['repository'])
    root = repository_root(repo)
    workspace = root / 'worktrees' / job['id']
    if str(workspace) != job['workspace_path']:
        raise ReviewError('任务工作目录与冻结记录不一致')
    with store.lock('git-workspace-' + cache_key(repo)):
        folder = root / 'objects.git'
        if workspace.exists():
            run_git(['worktree', 'remove', '--force', str(workspace)], cwd=folder)
        if folder.exists():
            for index in range(3):
                run_git(['update-ref', '-d', f"refs/review-active/{job['id']}/{index}"], cwd=folder)
        (root / 'active' / (job['id'] + '.json')).unlink(missing_ok=True)

"""按仓库权限隔离的增量 Git 缓存，只从固定对象生成任务快照。"""

import hashlib
import contextlib
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import selectors
import subprocess
import tempfile
import time
from urllib.parse import quote

from .common import ReviewError, encode, valid_sha
from .native_workspace import MAX_FILES, MAX_FILE_BYTES, MAX_EXTRACTED_BYTES, RESERVE_BYTES


def cache_root(store):
    return store.path.parent / "git-cache"


def cache_key(repo):
    identity = {key: repo.get(key) for key in ("gitea_url", "repo_id", "access_scope")}
    return hashlib.sha256(encode(identity).encode()).hexdigest()


class GitFailure(ReviewError):
    pass


@contextlib.contextmanager
def cache_lock(store, key, check):
    with (store.path.parent / ("git-cache-" + key + ".lock")).open("a") as lock:
        while True:
            check()
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def run_git(args, *, cwd=None, env=None, check=None, timeout=120, output_limit=32 * 1024 * 1024):
    clean = {key: value for key, value in os.environ.items() if not key.startswith(("GIT_", "SSH_"))}
    clean = {
        key: value
        for key, value in clean.items()
        if key.lower() not in ("http_proxy", "https_proxy", "all_proxy")
    }
    clean.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_TERMINAL_PROMPT="0",
        GIT_NO_REPLACE_OBJECTS="1",
        LC_ALL="C.UTF-8",
    )
    clean.update(env or {})
    command = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "protocol.file.allow=never",
        "-c",
        "protocol.ext.allow=never",
        "-c",
        "http.followRedirects=false",
        "-c",
        "credential.helper=",
        "-c",
        "gc.auto=0",
        *args,
    ]
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=clean,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if check:
                    check()
                if time.monotonic() >= deadline:
                    raise ReviewError("Git 源码获取超过时限")
                if out.tell() > output_limit or err.tell() > output_limit:
                    raise ReviewError("Git 输出超过容量限制")
                if cwd and shutil.disk_usage(cwd).free < RESERVE_BYTES:
                    raise ReviewError("Git 缓存磁盘空间不足")
                time.sleep(0.05)
            if check:
                check()
            if process.returncode:
                raise GitFailure("Git 源码操作失败，退出码 " + str(process.returncode))
            out.seek(0)
            value = out.read(output_limit + 1)
            if len(value) > output_limit:
                raise ReviewError("Git 输出超过容量限制")
            return value
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def _size(folder):
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file() and not p.is_symlink())


def inventory(store, repos):
    root = cache_root(store)
    rows = []
    for repo in repos:
        folder = root / cache_key(repo)
        if not folder.is_dir():
            continue
        rows.append({"repository": repo["id"], "bytes": _size(folder)})
    return {
        "limit_bytes": int(os.environ.get("AI_REVIEW_GIT_CACHE_BYTES", 8 * 1024**3)),
        "bytes": sum(row["bytes"] for row in rows),
        "repositories": rows,
    }


def clear(store, repo):
    with store.lock("git-cache-" + cache_key(repo)):
        shutil.rmtree(cache_root(store) / cache_key(repo), ignore_errors=True)


def prune(store, protected=None):
    root = cache_root(store)
    limit = int(os.environ.get("AI_REVIEW_GIT_CACHE_BYTES", 8 * 1024**3))
    with store.lock("git-cache-inventory", blocking=True):
        folders = sorted(
            (p for p in root.iterdir() if p.is_dir() and not p.is_symlink()), key=lambda p: p.stat().st_mtime
        )
        total = sum(_size(p) for p in folders)
        for folder in folders:
            if total <= limit:
                break
            if folder.name == protected:
                continue
            try:
                with store.lock("git-cache-" + folder.name):
                    size = _size(folder)
                    shutil.rmtree(folder)
                    total -= size
            except ReviewError:
                continue
        if total > limit:
            raise ReviewError("Git 缓存达到总容量限制，请清理缓存或调整容量")


class ObjectReader:
    def __init__(self, folder, check):
        self.check = check
        self.buffer = bytearray()
        self.process = subprocess.Popen(
            ["git", "-c", "core.hooksPath=/dev/null", "cat-file", "--batch"],
            cwd=folder,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_NO_REPLACE_OBJECTS": "1",
            },
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def fill(self, size=None):
        deadline = time.monotonic() + 30
        while len(self.buffer) < size if size is not None else b"\n" not in self.buffer:
            self.check()
            if time.monotonic() > deadline:
                raise ReviewError("读取固定 Git 对象超时")
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdout, selectors.EVENT_READ)
                if not selector.select(0.2):
                    continue
                chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise ReviewError("Git 对象读取中断")
            self.buffer.extend(chunk)

    def read(self, oid):
        self.process.stdin.write(oid.encode() + b"\n")
        self.process.stdin.flush()
        self.fill()
        header, _, remaining = self.buffer.partition(b"\n")
        self.buffer = bytearray(remaining)
        fields = header.split()
        if len(fields) != 3 or fields[0].decode() != oid or fields[1] != b"blob":
            raise ReviewError("Git 对象类型或身份不一致")
        size = int(fields[2])
        if size > MAX_FILE_BYTES:
            raise ReviewError("源码单文件超过限制")
        self.fill(size + 1)
        body = bytes(self.buffer[:size])
        if self.buffer[size] != 10:
            raise ReviewError("Git 对象分隔符无效")
        del self.buffer[: size + 1]
        return body

    def close(self):
        self.process.kill()
        self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()


def _extract(folder, sha, destination, check):
    tree = run_git(["ls-tree", "-rz", "--full-tree", sha], cwd=folder, check=check)
    records = tree.split(b"\0")
    if len(records) - 1 > MAX_FILES:
        raise ReviewError("固定源码文件数超过限制")
    destination.mkdir(mode=0o755)
    entries, unresolved, total = [], [], 0
    reader = ObjectReader(folder, check)
    try:
        for record in records:
            if not record:
                continue
            check()
            info, raw_name = record.split(b"\t", 1)
            mode, kind, oid = info.decode("ascii").split()
            try:
                name = raw_name.decode("utf-8")
            except UnicodeError:
                raise ReviewError("源码路径不是 UTF-8") from None
            from .common import safe_path

            safe_path(name)
            if mode not in ("100644", "100755"):
                unresolved.append(
                    {
                        "路径": name,
                        "类型": "子模块" if kind == "commit" else "链接",
                        "原因": "需要外部目标，未跟随",
                    }
                )
                continue
            body = reader.read(oid)
            total += len(body)
            if total > MAX_EXTRACTED_BYTES:
                raise ReviewError("固定源码超过提取容量限制")
            if shutil.disk_usage(destination).free < len(body) + RESERVE_BYTES:
                raise ReviewError("固定源码提取磁盘空间不足")
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            target.chmod(0o444)
            if body.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
                unresolved.append({"路径": name, "类型": "LFS 指针", "原因": "未获取外部 LFS 对象"})
            entries.append(
                {
                    "路径": name,
                    "字节数": len(body),
                    "SHA256": hashlib.sha256(body).hexdigest(),
                    "Git对象": oid,
                }
            )
        for parent, _, _ in os.walk(destination, topdown=False):
            Path(parent).chmod(0o555)
        return {"文件": entries, "未决内容": unresolved}
    finally:
        reader.close()


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.chmod(0o444)
    os.replace(temporary, path)


def prepare(store, client, repo, job, snapshot):
    from .authorization import authorize
    from .gitea import assert_version
    from .native_workspace import WORKSPACE_ROOT

    key = cache_key(repo)
    folder = cache_root(store) / key
    root = WORKSPACE_ROOT / job["id"]
    started = time.monotonic()
    deadline = time.monotonic() + store.job_config(job["id"]).get("batch_preprocess_seconds", 120)
    last_authorized = 0

    def check():
        nonlocal last_authorized
        if time.monotonic() - last_authorized >= 2:
            authorize(repo, job)
            last_authorized = time.monotonic()
        store.active(job["id"], job["owner"])
        client.check_active()
        if time.monotonic() >= deadline:
            raise ReviewError("源码准备超过预处理时限")

    authorize(repo, job)
    client.repository()
    cache_root(store).mkdir(mode=0o700, parents=True, exist_ok=True)
    WORKSPACE_ROOT.mkdir(mode=0o755, parents=True, exist_ok=True)
    with cache_lock(store, key, check):
        check()
        marker = root / "ready.json"
        if marker.exists():
            info = json.loads(marker.read_text())
            if info["snapshot_id"] != snapshot["id"]:
                raise ReviewError("工作区与任务固定版本不匹配")
            assert_version(client, job["number"], snapshot)
            return str(root), info
        mode = "首次获取" if not folder.exists() else "增量更新"
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not (folder / "HEAD").exists():
            run_git(["init", "--bare", "."], cwd=folder, check=check)
        try:
            run_git(["fsck", "--full", "--no-dangling"], cwd=folder, check=check)
        except GitFailure:
            # 超时、撤权不允许被当成损坏重建。
            check()
            shutil.rmtree(folder)
            folder.mkdir(mode=0o700)
            run_git(["init", "--bare", "."], cwd=folder, check=check)
            mode = "缓存重建"
        required = [valid_sha(snapshot[k]) for k in ("base", "head", "merge_base")]

        def has_object(sha):
            try:
                run_git(["cat-file", "-e", sha + "^{commit}"], cwd=folder, check=check)
                return True
            except GitFailure:
                check()
                return False

        network_started = time.monotonic()
        if all(has_object(sha) for sha in required):
            mode = "缓存复用"
        else:
            url = client.url + "/" + quote(repo["repo_name"], safe="/") + ".git"
            with tempfile.TemporaryDirectory(prefix="git-auth-") as temp:
                askpass = Path(temp) / "askpass.py"
                askpass.write_text(
                    '#!/usr/bin/env python3\nimport os,sys\nprint("oauth2" if "username" in sys.argv[1].lower() else os.environ["REVIEW_GIT_SECRET"])\n'
                )
                askpass.chmod(0o700)
                env = {"GIT_ASKPASS": str(askpass), "REVIEW_GIT_SECRET": store.unseal(repo["bot_token"])}
                refs = [
                    "+refs/heads/*:refs/heads/*",
                    f'+refs/pull/{job["number"]}/head:refs/review/{job["number"]}',
                ]
                run_git(
                    ["fetch", "--no-tags", url, *refs],
                    cwd=folder,
                    env=env,
                    check=check,
                    timeout=max(1, deadline - time.monotonic()),
                )
                for sha in required:
                    if not has_object(sha):
                        run_git(["fetch", "--no-tags", url, sha], cwd=folder, env=env, check=check)
        network_seconds = time.monotonic() - network_started
        run_git(["fsck", "--full", "--no-dangling"], cwd=folder, check=check)
        bases = (
            run_git(["merge-base", "--all", required[0], required[1]], cwd=folder, check=check)
            .decode()
            .split()
        )
        if required[2] not in bases:
            raise ReviewError("Gitea 比较基点与 Git 对象不一致")
        limit = int(os.environ.get("AI_REVIEW_GIT_CACHE_BYTES", 8 * 1024**3))
        if _size(folder) > limit:
            raise ReviewError("单仓库缓存超过配置容量，请调整容量后重试")
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(mode=0o755)
        try:
            before = _extract(folder, required[2], root / "before", check)
            after = _extract(folder, required[1], root / "after", check)
            # Gitea 的固定 diff 用于评论定位，内容另与两侧 Git blob 严格核对。
            (root / "change.diff").write_text(snapshot["diff"], encoding="utf-8")
            (root / "change.diff").chmod(0o444)
            from .diff import parse_diff

            paths = sorted({p.new_path or p.old_path for p in parse_diff(snapshot["diff"].encode())})
            write_json(
                root / "source-manifest.json", {"变更文件": paths, "修改前源码": before, "修改后源码": after}
            )
            info = {
                "snapshot_id": snapshot["id"],
                "mode": mode,
                "network_seconds": round(network_seconds, 3),
                "prepare_seconds": round(time.monotonic() - started - network_seconds, 3),
                "bytes": _size(folder),
                "unresolved": before["未决内容"] + after["未决内容"],
            }
            write_json(marker, info)
            assert_version(client, job["number"], snapshot)
            os.utime(folder, None)
            prune(store, protected=key)
            return str(root), info
        except BaseException:
            shutil.rmtree(root, ignore_errors=True)
            raise

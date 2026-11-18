"""Codex 接入的关键边界和增量对象提取回归。"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_review.common import ReviewError
from ai_review.responses import ResponseStream
from ai_review.git_cache import _extract, run_git, cache_key


def test_coverage_counts_actual_reads_across_phases():
    from ai_review.codex_review import read_coverage

    snapshot = {
        "diff": "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a=1\n+a=2\n"
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-b=1\n+b=2\n",
        "coverage": {"files_total": 2, "files_reviewed": 2, "complete": True},
    }
    assert read_coverage(snapshot, [])["files_reviewed"] == 0
    reads = [
        {"path": "after/a.py"},
        {"path": "after/a.py"},
        {"path": "after/helper.py"},
    ]
    result = read_coverage(snapshot, reads)
    assert result["files_reviewed"] == 1
    assert result["source_files_read"] == 2
    assert read_coverage(snapshot, reads + [{"path": "before/b.py"}])["files_reviewed"] == 2


def test_responses_requires_complete_terminal():
    parser = ResponseStream(4096)
    parser.feed(b'data: {"type":"response.completed","response":{"status":"completed","output":[]}}\r\n\r\n')
    assert parser.finish()["status"] == "completed"
    parser = ResponseStream(4096)
    parser.feed(b'data: {"type":"response.incomplete"}\n\n')
    with pytest.raises(ReviewError):
        parser.finish()


@pytest.mark.parametrize("configured_output_limit", [None, 16384])
@pytest.mark.parametrize("native_tools", [False, True])
@pytest.mark.parametrize("effort", ["low", "high", "max"])
def test_codex_requests_keep_authentication_and_usage_beyond_legacy_limits(tmp_path, monkeypatch, configured_output_limit, native_tools, effort):
    import hashlib
    import time

    from flask import Flask
    from ai_review.budgets import CODEX_AND_LEGACY_BUDGETS
    from ai_review.responses import register
    from ai_review.store import Store

    store = Store(tmp_path / "review.db")
    config = dict(store.config(), **CODEX_AND_LEGACY_BUDGETS, engine='codex_cli')
    assert config["model_max_output_tokens"] == 0
    config.update(enabled=True, debounce_seconds=0, batch_input_tokens=4096,
                  thinking_protocol="deepseek", thinking_enabled=True, reasoning_effort=effort)
    if configured_output_limit is not None:
        config["model_max_output_tokens"] = configured_output_limit
    store.save_config(config)
    repo = store.save_repo(1, 1, {"enabled": True, "gitea_url": "http://test", "repo_name": "owner/repo"})
    job_id = store.enqueue(repo["id"], 1, {"head": "b" * 40, "base": "a" * 40})
    job = store.claim()
    store.update(job_id, job["owner"], session_id="codex-session")
    if native_tools:
        store.update(job_id, job["owner"], snapshot={"codex_tools": "native"})
    with store.tx() as conn:
        conn.execute("INSERT INTO capabilities VALUES (?,?,?,?,?,?)", (
            hashlib.sha256(b"test-capability").hexdigest(), job_id, job["owner"], time.time() + 60, -1,
            store.config()["model"],
        ))

    assert store.job_config(job_id)["model_context_tokens"] == 1_000_000

    class ResponsesStream:
        status = 200

        def chunks(self, guard):
            guard()
            yield (b'data: {"type":"response.completed","response":{"status":"completed","output":[],'
                   b'"usage":{"input_tokens":43000,"output_tokens":300}}}\n\n')

        def close(self):
            pass

    sent = []

    def open_stream(url, body, *_args, **_kwargs):
        sent.append(body)
        return ResponsesStream()

    monkeypatch.setattr("ai_review.responses.open_model_stream", open_stream)
    monkeypatch.setattr("ai_review.responses.endpoint", lambda *_: "https://example.invalid/v1")
    monkeypatch.setattr(store, "unseal", lambda _: "test-key")
    app = Flask(__name__)
    register(app, store)
    client = app.test_client()
    headers = {"Authorization": "Bearer test-capability", "X-Review-Session": "codex-session"}
    body = {"model": store.config()["model"], "stream": True, "max_output_tokens": 8192,
            "input": [{"role": "user", "content": "测试" * 25000}]}
    body["tools"] = [{"type": "function", "name": "exec_command", "parameters": {"type": "object"}}]
    assert client.post("/v1/responses", json=body).status_code == 400
    assert client.post("/v1/responses", json=body, headers={**headers, "X-Review-Session": "other"}).status_code == 400
    assert client.post("/v1/responses", json=body, headers=headers, buffered=True).status_code == 200
    body["max_output_tokens"] = 1024
    assert client.post("/v1/responses", json=body, headers=headers).status_code == 400
    for call in range(2, 66):
        body["input"] = [{"role": "user", "content": f"第 {call} 次请求"}]
        assert client.post("/v1/responses", json=body, headers=headers, buffered=True).status_code == 200
    store.finish(job_id, job["owner"], "cancelled")
    body["input"] = [{"role": "user", "content": "取消后的新请求"}]
    assert client.post("/v1/responses", json=body, headers=headers).status_code == 400
    assert len(sent) == 65
    assert all(bool(outbound["tools"]) == native_tools for outbound in sent)
    assert all(outbound["reasoning"] == {"effort": effort} for outbound in sent)
    if configured_output_limit is None:
        assert all("max_output_tokens" not in outbound for outbound in sent)
    else:
        assert all(outbound["max_output_tokens"] == configured_output_limit for outbound in sent)
    assert store.job(job_id)["model_calls"] == 65
    assert store.job(job_id)["model_usage"] == {"input_tokens": 43000 * 65, "output_tokens": 300 * 65}


def test_native_tools_response_and_fixed_source_evidence(tmp_path):
    from ai_review.codex_review import normalize, read_coverage

    item = {"type": "function_call", "name": "exec_command", "arguments": '{"cmd":"rg bug after"}'}
    ResponseStream(4096, native_tools=True).validate_item(item)
    with pytest.raises(ReviewError):
        ResponseStream(4096).validate_item(item)
    (tmp_path / "after").mkdir()
    (tmp_path / "after/a.py").write_text("a=2\n")
    snapshot = {
        "id": "fixed-snapshot",
        "codex_tools": "native",
        "diff": "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a=1\n+a=2\n",
        "coverage": {"files_total": 1, "files_reviewed": 1, "complete": True},
    }
    raw = {"findings": [{"priority": 1, "title": "测试问题", "body": "固定版本中的具体问题",
                          "code_location": {"absolute_file_path": "after/a.py",
                                            "line_range": {"start": 1, "end": 1}}}]}
    assert normalize(raw, snapshot, tmp_path)[0]["support"][0]["quote"] == "a=2"
    assert read_coverage(snapshot, [])["source_files_read"] is None
    assert read_coverage(snapshot, [])["files_reviewed"] == 0
    raw["findings"][0]["code_location"]["absolute_file_path"] = "../secret"
    with pytest.raises(ReviewError):
        normalize(raw, snapshot, tmp_path)


@pytest.mark.parametrize("effort", ["low", "high", "max"])
def test_codex_native_config_and_command_events(tmp_path, effort):
    from types import SimpleNamespace

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "review-runtime"))
    from codex_session import CodexRun

    run = object.__new__(CodexRun)
    run.root = tmp_path
    run.upstream_output_tokens = 0
    run.compaction_threshold_tokens = 800000
    run.model_context_tokens = 1000000
    run.model = "测试模型"
    run.gateway = SimpleNamespace(url="http://127.0.0.1:1/v1")
    run.thinking_enabled = True
    run.reasoning_effort = effort
    run.project = tmp_path
    run.native_tools = True
    run.native_commands = []
    run.command_started = {}
    args = run._config()
    settings = [args[i + 1] for i in range(0, len(args), 2)]
    assert f'model_reasoning_effort="{effort}"' in settings
    assert "features.shell_tool=true" in settings
    assert not any(value.startswith("mcp_servers.") for value in settings)
    events = []
    run.emit = lambda kind, **kwargs: events.append((kind, kwargs))
    command = {"type": "command_execution", "id": "执行一", "command": "rg needle after",
               "aggregated_output": "after/a.py:1:needle", "exit_code": 0, "status": "completed"}
    run._consume({"type": "item.completed", "item": command})
    assert run.native_commands[0]["command"] == "rg needle after"
    assert events[0][1]["payload"]["call_id"] == "执行一"
    run.native_tools = False
    settings = run._config()
    assert any(value.startswith("mcp_servers.review.command=") for value in settings)


def test_native_complete_review_accepts_intro_and_rejects_truncated_results():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "review-runtime"))
    from codex_session import decode_review
    from native_session import NativeError

    value = {"findings": [], "overall_explanation": "核对完成"}
    encoded = json.dumps(value, ensure_ascii=False)
    assert decode_review("审查已完成。\n" + encoded) == value
    assert decode_review("```json\n" + encoded + "\n```") == value
    with pytest.raises(NativeError):
        decode_review(encoded[:-1])
    with pytest.raises(NativeError):
        decode_review(encoded + "\n" + encoded)


def test_incomplete_marker_requires_explicit_result_prefix():
    from ai_review.codex_review import incomplete_result

    assert not incomplete_result({"overall_explanation": "证据已经核对，无需标记 INCOMPLETE。"})
    assert incomplete_result({"overall_explanation": " INCOMPLETE 缺少必要源码"})
    assert incomplete_result({"_budget_exhausted": True, "overall_explanation": ""})


def test_extract_ignores_export_ignore_and_never_follows_symlinks(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    (repo / "a.py").write_text("print(1)\n")
    (repo / ".gitattributes").write_text("a.py export-ignore\n")
    (repo / "escape").symlink_to("/etc/passwd")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=测试",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "初始版本",
        ],
        check=True,
        capture_output=True,
    )
    sha = run_git(["rev-parse", "HEAD"], cwd=repo).decode().strip()
    result = _extract(repo, sha, tmp_path / "snapshot", lambda: None)
    assert (tmp_path / "snapshot/a.py").read_text() == "print(1)\n"
    assert not (tmp_path / "snapshot/escape").exists()
    assert result["未决内容"][0]["类型"] == "链接"


def test_cache_separates_authorization():
    repo = {"gitea_url": "http://gitea", "repo_id": 1, "access_scope": {"binding_id": 1}}
    assert cache_key(repo) != cache_key(dict(repo, access_scope={"binding_id": 2}))


def test_corrupt_blob_rebuilds_cache_before_snapshot(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    from ai_review import git_cache, native_workspace
    from ai_review.store import Store

    key = tmp_path / "master.key"
    key.write_bytes(Fernet.generate_key())
    monkeypatch.setenv("AI_REVIEW_MASTER_KEY_FILE", str(key))
    store = Store(tmp_path / "review.db")
    config = store.config()
    config.update(enabled=True, debounce_seconds=0)
    store.save_config(config)
    repo = store.save_repo(1, 1, {"enabled": True, "gitea_url": "http://test",
                                "repo_name": "owner/repo", "bot_token": store.seal("test")})
    repo = store.repo(repo["id"])
    source = tmp_path / "source"
    source.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(source), *args], stderr=subprocess.DEVNULL).decode().strip()

    git("init", "-b", "main")
    git("config", "user.name", "测试")
    git("config", "user.email", "test@example.invalid")
    (source / "a.py").write_text("a=1\n")
    git("add", ".")
    git("commit", "-m", "基线")
    base = git("rev-parse", "HEAD")
    (source / "a.py").write_text("a=2\n")
    git("commit", "-am", "变更")
    head = git("rev-parse", "HEAD")
    original = git_cache.run_git

    def fetch_local(args, **kwargs):
        if args[0] == "fetch":
            return subprocess.check_output(
                ["git", "-c", "protocol.file.allow=always", "-C", str(kwargs["cwd"]),
                 "fetch", str(source), "+refs/heads/*:refs/heads/*"], stderr=subprocess.DEVNULL)
        return original(args, **kwargs)

    from types import SimpleNamespace
    client = SimpleNamespace(url="http://test", repository=lambda: None, check_active=lambda: None)
    monkeypatch.setattr(git_cache, "run_git", fetch_local)
    monkeypatch.setattr(native_workspace, "WORKSPACE_ROOT", tmp_path / "workspaces")
    monkeypatch.setattr("ai_review.authorization.authorize", lambda *args: None)
    monkeypatch.setattr("ai_review.gitea.assert_version", lambda *args: None)
    for number in (1, 2):
        job_id = store.enqueue(repo["id"], number, {"head": head, "base": base})
        job = store.claim()
        workspace, info = git_cache.prepare(store, client, repo, job,
            {"id": head, "base": base, "head": head, "merge_base": base, "diff": git("diff", base, head)})
        assert (Path(workspace) / "after/a.py").read_text() == "a=2\n"
        assert info["mode"] == ("首次获取" if number == 1 else "缓存重建")
        store.finish(job_id, job["owner"], "succeeded")
        if number == 1:
            oid = git("rev-parse", "HEAD:a.py")
            blob = git_cache.cache_root(store) / cache_key(repo) / "objects" / oid[:2] / oid[2:]
            blob.chmod(0o600)
            blob.write_bytes(b"broken")


def test_source_tools_reject_escape_and_record_unlimited_reads(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "review-runtime"))
    from review_tools import SourceTools

    (tmp_path / "after").mkdir()
    (tmp_path / "after/a.py").write_text("print(1)\n")
    (tmp_path / "after/link").symlink_to("/etc/passwd")
    tools = SourceTools(tmp_path, tmp_path / "reads.jsonl")
    for path in ("../secret", "/etc/passwd", "after/link"):
        with pytest.raises(ValueError):
            tools.call("read", {"path": path})
    for _ in range(201):
        assert tools.call("read", {"path": "after/a.py"})["line_end"] == 1
    assert len((tmp_path / "reads.jsonl").read_text().splitlines()) == 201


def test_defaults_preserve_explicit_model(tmp_path):
    from ai_review.store import Store

    store = Store(tmp_path / "review.db")
    assert store.config()["model"] == "deepseek-flash"
    assert store.config()["compaction_threshold_tokens"] == 800_000
    with store.tx() as c:
        current = store.config(connection=c)
        current["model"] = "existing-model"
        c.execute("UPDATE settings SET value=?", (json.dumps(current),))
    assert store.config()["model"] == "existing-model"


def test_responses_rejects_hidden_native_tool_at_terminal():
    parser = ResponseStream(4096)
    terminal = {
        "type": "response.completed",
        "response": {
            "status": "completed",
            "output": [{"type": "function_call", "name": "shell", "arguments": "{}"}],
        },
    }
    with pytest.raises(ReviewError):
        parser.feed(("data: " + json.dumps(terminal) + "\n\n").encode())


def test_current_source_can_prove_repair_with_empty_diff():
    from ai_review.issue_lifecycle import validate_check

    payload = {
        "historical_diff": {"H1": "return a / b"},
        "current_diff": {},
        "current_source": {"S1": "return a / b if b else 0"},
        "candidates": [],
        "issue_id": "test",
    }
    raw = {
        "issue_id": "test",
        "snapshot_id": "fixed",
        "status": "fixed",
        "reason": "新增保护覆盖了原触发条件",
        "evidence": [
            {"id": "H1", "quote": "return a / b"},
            {"id": "S1", "quote": "return a / b if b else 0"},
        ],
        "relations": {},
        "anchor_id": None,
    }
    assert validate_check(raw, payload, {"id": "fixed"})["status"] == "fixed"


def test_native_git_workspace_reuses_repository_and_fixed_evidence(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from cryptography.fernet import Fernet
    from ai_review import git_workspace, native_workspace
    from ai_review.store import Store
    from ai_review.codex_review import normalize, prompt

    key = tmp_path / 'master.key'
    key.write_bytes(Fernet.generate_key())
    monkeypatch.setenv('AI_REVIEW_MASTER_KEY_FILE', str(key))
    monkeypatch.setattr(native_workspace, 'WORKSPACE_ROOT', tmp_path / 'workspaces')
    monkeypatch.setattr('ai_review.authorization.authorize', lambda *args: None)
    monkeypatch.setattr('ai_review.gitea.assert_version', lambda *args: None)
    source = tmp_path / 'source'
    source.mkdir()

    def git(*args):
        return subprocess.check_output(['git', '-C', str(source), *args], stderr=subprocess.DEVNULL).decode().strip()

    git('init', '-b', 'custom-main')
    git('config', 'user.name', '测试')
    git('config', 'user.email', 'test@example.invalid')
    (source / 'a.py').write_text('a=1\n')
    (source / '.gitattributes').write_text('a.py export-ignore text eol=crlf\n')
    (source / 'link').symlink_to('/etc/passwd')
    git('add', '.')
    git('commit', '-m', '基线')
    base = git('rev-parse', 'HEAD')
    (source / 'a.py').write_text('a=2\n')
    git('commit', '-am', '修改')
    head = git('rev-parse', 'HEAD')
    original = git_workspace.run_git
    fetches = []

    def local_git(args, **kwargs):
        if 'fetch' in args:
            fetches.append(1)
            return subprocess.check_output(['git', '-c', 'protocol.file.allow=always', '-C', str(kwargs['cwd']),
                                            'fetch', str(source), '+refs/heads/*:refs/remotes/origin/*'], stderr=subprocess.DEVNULL)
        return original(args, **kwargs)

    monkeypatch.setattr(git_workspace, 'run_git', local_git)
    client = SimpleNamespace(url='http://test', repository=lambda: None, check_active=lambda: None)
    store = Store(tmp_path / 'review.db')
    config = store.config()
    config.update(enabled=True, debounce_seconds=0)
    store.save_config(config)
    saved = store.save_repo(1, 1, {'enabled': True, 'gitea_url': 'http://test', 'repo_name': 'owner/repo',
                                  'bot_token': store.seal('test')})
    repo = store.repo(saved['id'])
    paths = []
    for number in (1, 2, 3):
        if number == 3:
            (source / 'a.py').write_text('a=3\n')
            git('commit', '-am', '后续修改')
            head = git('rev-parse', 'HEAD')
        job_id = store.enqueue(repo['id'], number, {'base': base, 'head': head})
        job = store.claim()
        snapshot = {'id': head, 'base': base, 'head': head, 'merge_base': base,
                    'workspace_layout': 'git-worktree-v1', 'codex_tools': 'native', 'diff': git('diff', base, head) + '\n'}
        workspace, info = git_workspace.prepare(store, client, repo, job, snapshot)
        paths.append(workspace)
        assert info['mode'] == {1: '首次获取', 2: '缓存复用', 3: '增量更新'}[number]
        assert len(fetches) == (1 if number < 3 else 2)
        assert not (Path(workspace) / 'before').exists()
        assert (Path(workspace) / 'link').read_text() == '/etc/passwd'
        assert not (Path(workspace) / 'link').is_symlink()
        assert git_workspace.read_blob(workspace, base, 'a.py') == b'a=1\n'
        bound = native_workspace.bind_snapshot_evidence(store, job, snapshot, workspace)
        raw = {'findings': [{'priority': 1, 'title': '测试问题', 'body': '固定版本定位',
                            'code_location': {'absolute_file_path': str(Path(workspace) / 'a.py'),
                                              'line_range': {'start': 1, 'end': 1}}}]}
        result = normalize(raw, bound, workspace)
        assert result[0]['support'][0]['quote'] == ('a=3' if number == 3 else 'a=2')
        assert 'before/' not in prompt(snapshot)
        store.finish(job_id, job['owner'], 'succeeded')
        from ai_review.concurrency import reap
        reap(store)
        if number == 2:
            (Path(workspace) / 'a.py').write_text('被中断任务改动\n')
    assert len(set(paths)) == 1
    assert (Path(workspace) / 'a.py').read_bytes() == b'a=3\n'
    oid = git('rev-parse', 'HEAD:a.py')
    blob = git_workspace.repository_root(repo) / 'objects.git' / 'objects' / oid[:2] / oid[2:]
    blob.chmod(0o600)
    blob.write_bytes('损坏对象'.encode())
    job_id = store.enqueue(repo['id'], 4, {'base': base, 'head': head})
    job = store.claim()
    workspace, info = git_workspace.prepare(store, client, repo, job, snapshot)
    assert info['mode'] == '缓存重建'
    assert (Path(workspace) / 'a.py').read_bytes() == b'a=3\n'

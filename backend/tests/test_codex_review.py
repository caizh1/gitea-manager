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


def test_source_tools_reject_escape(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "review-runtime"))
    from review_tools import SourceTools

    (tmp_path / "after").mkdir()
    (tmp_path / "after/a.py").write_text("print(1)\n")
    (tmp_path / "after/link").symlink_to("/etc/passwd")
    tools = SourceTools(tmp_path, tmp_path / "reads.jsonl")
    for path in ("../secret", "/etc/passwd", "after/link"):
        with pytest.raises(ValueError):
            tools.call("read", {"path": path})
    assert tools.call("read", {"path": "after/a.py"})["line_end"] == 1


def test_defaults_preserve_explicit_model(tmp_path):
    from ai_review.store import Store

    store = Store(tmp_path / "review.db")
    assert store.config()["model"] == "DeepSeek- V4.1-Flash"
    with store.tx() as c:
        current = store.config(connection=c)
        current["model"] = "existing-model"
        c.execute("UPDATE settings SET value=?", (json.dumps(current),))
    assert store.config()["model"] == "existing-model"


def test_publication_requires_frozen_confirmation():
    from test_ai_review_issue_lifecycle import LifecycleTests
    from ai_review.issue_lifecycle import reconcile
    from ai_review.publication import await_publication, preview, approve, publish_approved

    case = LifecycleTests()
    case.setUp()
    try:
        store, job = case.store, case.job
        with store.tx() as c:
            config = json.loads(
                c.execute(
                    "SELECT value FROM configuration_history WHERE revision=?", (job["config_revision"],)
                ).fetchone()[0]
            )
            config["engine"] = "codex_cli"
            c.execute(
                "UPDATE configuration_history SET value=? WHERE revision=?",
                (json.dumps(config), job["config_revision"]),
            )
        result = reconcile(store, case.client, job, case.repo_value, case.snapshot, case.report())
        await_publication(store, job, result)
        view = preview(store, store.job(job["id"]))
        assert not case.client.calls
        approve(
            store,
            case.client,
            job,
            {"preview_hash": view["preview_hash"], "selected": [view["findings"][0]["issue_id"]]},
            "测试管理员",
        )
        claimed = store.claim()
        with patch("ai_review.publication.publish_overview"):
            publish_approved(store, case.client, claimed, case.repo_value)
        assert store.job(job["id"])["status"] == "succeeded"
        assert sum(method == "POST" for method, _, _ in case.client.calls) == 1
        with pytest.raises(ReviewError):
            approve(
                store, case.client, job, {"preview_hash": view["preview_hash"], "selected": []}, "测试管理员"
            )
    finally:
        case.tearDown()


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


def test_unselected_findings_stay_in_manager():
    from test_ai_review_issue_lifecycle import LifecycleTests
    from ai_review.issue_lifecycle import reconcile
    from ai_review.publication import (
        await_publication,
        preview,
        approve,
        publish_approved,
        confirmed_overview,
    )

    case = LifecycleTests()
    case.setUp()
    try:
        with case.store.tx() as c:
            config = json.loads(
                c.execute(
                    "SELECT value FROM configuration_history WHERE revision=?", (case.job["config_revision"],)
                ).fetchone()[0]
            )
            config["engine"] = "codex_cli"
            c.execute(
                "UPDATE configuration_history SET value=? WHERE revision=?",
                (json.dumps(config), case.job["config_revision"]),
            )
        result = reconcile(case.store, case.client, case.job, case.repo_value, case.snapshot, case.report())
        await_publication(case.store, case.job, result)
        view = preview(case.store, case.store.job(case.job["id"]))
        approve(
            case.store,
            case.client,
            case.job,
            {"preview_hash": view["preview_hash"], "selected": []},
            "测试管理员",
        )
        job = case.store.claim()
        with patch("ai_review.publication.publish_overview"):
            publish_approved(case.store, case.client, job, case.repo_value)
        assert not case.client.calls
        saved = case.store.job(job["id"])
        assert len(saved["result"]["findings"]) == 1
        assert saved["result"]["publication"]["findings"] == []
        assert result["findings"][0]["issue_id"][:12] not in confirmed_overview(case.store, saved)
    finally:
        case.tearDown()


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

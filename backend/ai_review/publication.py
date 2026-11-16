"""发布预览与一次性确认，绑定结果内容和 PR 固定版本。"""

import json
import contextlib
from copy import deepcopy
import time

from .common import ReviewError, PublishUnknown, digest, encode


def migrate(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS review_codex_usage (
      call_id TEXT PRIMARY KEY, job TEXT NOT NULL, input_tokens INTEGER NOT NULL,
      output_tokens INTEGER NOT NULL, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS codex_review_phases (
      job TEXT NOT NULL, phase TEXT NOT NULL, session_id TEXT NOT NULL, result TEXT NOT NULL,
      created REAL NOT NULL, PRIMARY KEY(job,phase));
    CREATE TABLE IF NOT EXISTS review_publication_approvals (
      job TEXT PRIMARY KEY, preview_hash TEXT NOT NULL, selected TEXT NOT NULL,
      approved_by TEXT NOT NULL, approved_at REAL NOT NULL);
    """)


def preview(store, job):
    if store.job_config(job["id"]).get("engine") != "codex_cli" or not (job.get("result") or {}).get(
        "lifecycle"
    ):
        raise ReviewError("该任务尚未生成发布预览")
    result = job["result"]
    with contextlib.closing(store.connect()) as c:
        rows = c.execute(
            "SELECT o.issue,o.value,i.finding FROM review_issue_observations o JOIN review_issues i ON i.id=o.issue WHERE o.job=? ORDER BY o.issue",
            (job["id"],),
        ).fetchall()
    changes = [
        {
            "issue": row["issue"],
            "title": json.loads(row["finding"]).get("title", ""),
            **json.loads(row["value"]),
        }
        for row in rows
    ]
    value = {
        "job": job["id"],
        "snapshot_id": job["snapshot"]["id"],
        "head": job["snapshot"]["head"],
        "findings": result["findings"],
        "status_changes": changes,
        "report": result.get("report_markdown", ""),
    }
    value["preview_hash"] = digest(value)
    return value


def await_publication(store, job, result):
    store.update(job["id"], job["owner"], result=result, stage="等待确认发布")
    with store.tx() as c:
        store._active(c, job["id"], job["owner"])
        c.execute(
            "UPDATE jobs SET status='awaiting_publication',owner='',lease=0,updated=? WHERE id=?",
            (time.time(), job["id"]),
        )
        c.execute("DELETE FROM capabilities WHERE job=?", (job["id"],))


def approve(store, client, job, data, actor):
    from .gitea import assert_version
    from .authorization import authorize

    with store.lock(f"发布-{job['repository']}-{job['number']}"):
        current = store.job(job["id"])
        view = preview(store, current)
        if current["status"] != "awaiting_publication" or data.get("preview_hash") != view["preview_hash"]:
            raise ReviewError("预览已变化或已经确认，请刷新")
        selected = data.get("selected")
        allowed = {f["issue_id"] for f in view["findings"] if f.get("issue_id")} | {
            r["issue"] for r in view["status_changes"]
        }
        if (
            not isinstance(selected, list)
            or any(not isinstance(i, str) for i in selected)
            or not set(selected) <= allowed
        ):
            raise ReviewError("选择的问题不属于当前预览")
        authorize(store.repo(job["repository"]), job, "write")
        assert_version(client, job["number"], current["snapshot"])
        with store.tx() as c:
            configured = store.repo(job["repository"], c)
            current_config = store.config(connection=c)
            if (
                not configured["enabled"]
                or not current_config["enabled"]
                or configured["revision"] != job["repo_revision"]
                or store.execution_revision_for(job["config_revision"], c)
                != current_config["execution_revision"]
            ):
                raise ReviewError("仓库权限或配置已变化，请重新审查")
            row = c.execute("SELECT status FROM jobs WHERE id=?", (job["id"],)).fetchone()
            if row["status"] != "awaiting_publication":
                raise ReviewError("任务状态已变化")
            c.execute(
                "INSERT INTO review_publication_approvals VALUES (?,?,?,?,?)",
                (job["id"], view["preview_hash"], encode(sorted(set(selected))), actor, time.time()),
            )
            c.execute(
                "UPDATE jobs SET status='queued',available=?,stage='发布已确认，等待执行',updated=? WHERE id=?",
                (time.time(), time.time(), job["id"]),
            )


def publish_approved(store, client, job, repo):
    from .publish import Publisher
    from .gitea import assert_version

    with contextlib.closing(store.connect()) as c:
        approved = c.execute(
            "SELECT * FROM review_publication_approvals WHERE job=?", (job["id"],)
        ).fetchone()
    if not approved or approved["preview_hash"] != preview(store, job)["preview_hash"]:
        raise ReviewError("发布确认缺失或内容已变化")
    if not job["result"].get("analysis_complete") and job["snapshot"].get("engine") == "codex_cli":
        raise ReviewError("审查尚未完整结束，禁止确认发布")
    assert_version(client, job["number"], job["snapshot"])
    selected = set(json.loads(approved["selected"]))
    result = deepcopy(job["result"])
    result.pop("publication", None)
    result["findings"] = [f for f in result["findings"] if f.get("issue_id") in selected]
    result["lifecycle"] = dict(
        result["lifecycle"], publish_ids=[i for i in result["lifecycle"]["publish_ids"] if i in selected]
    )
    # 发布器只同步已确认的问题状态。
    result["_approved_issue_ids"] = sorted(selected)
    full_result = deepcopy(job["result"])
    full_result["publication"] = result
    store.update(job["id"], job["owner"], result=full_result)
    publishing_job = dict(job)
    Publisher(store, client, publishing_job, repo, job["snapshot"], result).publish()
    # 固定总览在本次明确确认后同步。
    store.update(job["id"], job["owner"], status="publishing")
    publish_overview(store, client, job, repo, result)
    store.finish(job["id"], job["owner"], "succeeded")


def publish_overview(store, client, job, repo, result):
    """确认发布时更新固定总览，沿用已有同步器的未知回执保护。"""
    from .progress import ProgressSync

    # 同步器按确认记录授权写入。
    with store.tx() as c:
        c.execute(
            "UPDATE pr_state SET progress_next=0 WHERE repository=? AND number=?",
            (job["repository"], job["number"]),
        )
    sync = ProgressSync(store, factory=lambda _: client)
    if not sync.sync(job["repository"], job["number"]):
        raise PublishUnknown("确认后的总览同步尚未完成，请核对任务状态")


def confirmed_overview(store, job):
    """总览内容只来自本次确认的固定版本和所选问题。"""
    from .progress import SUMMARY_MARK
    from .issue_lifecycle import LABELS
    from .publish import prose

    with contextlib.closing(store.connect()) as c:
        approval = c.execute(
            "SELECT selected FROM review_publication_approvals WHERE job=?", (job["id"],)
        ).fetchone()
    if not approval:
        raise ReviewError("缺少总览发布确认")
    selected = set(json.loads(approval["selected"]))
    view = preview(store, job)
    body = SUMMARY_MARK + "\n### AI Code Review\n\n审查版本 `" + view["head"] + "`。"
    body += "\n\n" + (job["result"].get("report_markdown", "").splitlines() or [""])[0]
    for row in view["status_changes"]:
        if row["issue"] in selected:
            body += (
                "\n\n- `" + row["issue"][:12] + "` **" + LABELS[row["status"]] + "** " + prose(row["title"])
            )
            body += "\n  " + prose(row["reason"])
    body += "\n\n完整分析与未发布意见保存在 Manager 任务 `" + job["id"] + "`。"
    return body


def recover_confirmed(store, client, job, repo):
    """仅回读已确认的写入，所有回执齐全才算发布完成。"""
    from .publish import Publisher
    from .issue_publication import recover, change_operation
    from .progress import ProgressSync

    result = job["result"].get("publication")
    if not result or "_approved_issue_ids" not in result:
        raise PublishUnknown("缺少已确认的冻结发布内容")
    publisher = Publisher(store, client, job, repo, job["snapshot"], result)
    with contextlib.closing(store.connect()) as c:
        creates = c.execute(
            "SELECT 1 FROM review_issue_operations WHERE job=? AND kind='create'", (job["id"],)
        ).fetchone()
        edits = [
            dict(row)
            for row in c.execute(
                "SELECT * FROM review_issue_operations WHERE job=? AND kind='edit'", (job["id"],)
            )
        ]
    reference = {"review_id": job.get("review_id"), "review_fragment": job.get("review_fragment", "")}
    if creates:
        reference, incomplete = recover(publisher)
        if incomplete:
            raise PublishUnknown("部分确认意见尚未发送，保留未完成发布状态")
    for op in edits:
        payload = json.loads(op["payload"])
        rows = client.pages(publisher.path + "/" + str(payload["review_id"]) + "/comments")
        actual = next((row for row in rows if row["id"] == payload["comment_id"]), None)
        if (
            not actual
            or actual.get("user", {}).get("id") != repo["bot_id"]
            or actual.get("body") != payload["body"]
        ):
            raise PublishUnknown("历史问题状态写入尚未取得确定回执")
        change_operation(store, job, op["key"], "done", {"comment_id": payload["comment_id"]})
        with store.tx() as c:
            c.execute(
                "UPDATE review_issue_comments SET last_body=?,error=? WHERE repository=? AND comment_id=?",
                (payload["body"], "", repo["id"], payload["comment_id"]),
            )
    expected = confirmed_overview(store, job)
    rows = client.pages(client.prefix + f"/issues/{job['number']}/comments")
    matches = [
        row for row in rows if row.get("body") == expected and row.get("user", {}).get("id") == repo["bot_id"]
    ]
    if len(matches) != 1 or not ProgressSync(store).belongs(matches[0], repo, job["number"]):
        raise PublishUnknown("版本总览尚未取得唯一确定回执")
    with store.tx() as c:
        c.execute(
            "UPDATE review_issue_summaries SET body=?,pending=NULL WHERE repository=? AND number=?",
            (expected, repo["id"], job["number"]),
        )
    return reference

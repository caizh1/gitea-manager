"""PR-Agent选择、冻结配置、结果门禁与自动发布回归。"""
import importlib.util
import json
import os
import sqlite3
from pathlib import Path

import pytest

from ai_review.common import ReviewError, Stale
from ai_review.store import Store
from ai_review.thinking import parameters
from ai_review.budgets import validate_context
from test_auto_publication import case, result, send, retry, next_version


def test_new_review_store_uses_verified_pr_agent_budget(tmp_path):
    config = Store(tmp_path / 'review.db').config()
    assert config['engine'] == 'pr_agent'
    assert config['model'] == 'deepseek-flash' and config['api_base_url'] == '' and not config['enabled']
    assert config['model_context_tokens'] == 1_000_000
    assert config['model_max_output_tokens'] == 393216
    assert config['model_response_max_mib'] == 64
    assert config['max_findings'] == 30
    assert config['task_timeout_seconds'] == config['review_timeout_seconds'] == config['model_request_timeout_seconds'] == 3600
    assert parameters(config) == {'thinking': {'type': 'enabled'}, 'reasoning_effort': 'high'}
    validate_context(config)


def test_pr_agent_budget_preset_preserves_existing_task_configuration(tmp_path):
    from ai_review.budgets import PR_AGENT_BUDGETS

    store = Store(tmp_path / 'review.db')
    store.save_config({'revision': 1, 'enabled': True, 'model_max_output_tokens': 0,
                       'max_calls': 32, 'max_files': 40, 'max_findings': 20})
    repo = store.save_repo(1, 1, {'enabled': True, 'gitea_url': 'http://test', 'repo_name': 'owner/repo'})
    first = store.enqueue(repo['id'], 1, {'head': 'b' * 40, 'base': 'a' * 40})
    store.save_config({'revision': store.config()['revision'], **PR_AGENT_BUDGETS})
    second = store.enqueue(repo['id'], 2, {'head': 'c' * 40, 'base': 'a' * 40})
    assert store.job(first)['status'] == 'queued'
    assert store.job_config(first)['model_max_output_tokens'] == 0
    assert store.job_config(first)['max_findings'] == 20
    assert store.job_config(second)['model_max_output_tokens'] == 393216
    assert store.job_config(second)['max_findings'] == 30


def test_review_store_upgrades_version_22_without_changing_old_jobs(tmp_path):
    store = Store(tmp_path / 'review.db')
    store.save_config({'revision': 1, 'enabled': True})
    repo = store.save_repo(1, 1, {'enabled': True, 'gitea_url': 'http://test', 'repo_name': 'owner/repo'})
    old_job = store.enqueue(repo['id'], 1, {'head': 'b' * 40, 'base': 'a' * 40})
    with sqlite3.connect(store.path) as connection:
        connection.execute('ALTER TABLE jobs DROP COLUMN pr_agent_protocol')
        connection.execute('PRAGMA user_version=22')

    upgraded = Store(store.path)
    assert upgraded.job(old_job)['pr_agent_protocol'] == 0
    new_job = upgraded.enqueue(repo['id'], 2, {'head': 'c' * 40, 'base': 'a' * 40})
    assert upgraded.job(new_job)['pr_agent_protocol'] == 1
    assert Store(upgraded.path).job(old_job)['status'] == 'queued'


@pytest.mark.parametrize(('added', 'deleted'), [
    (10000, 0), (0, 10001), (29999, 0), (30000, 0), (0, 30000),
    (15000, 15000), (30001, 0), (0, 30001), (15001, 15000),
])
def test_pr_agent_limit_counts_both_sides(added, deleted):
    from ai_review.batch_flow import validate_single_size

    snapshot = {'engine': 'pr_agent'}
    raw = ('diff --git a/replace.c b/replace.c\n--- a/replace.c\n+++ b/replace.c\n'
           f'@@ -{int(bool(deleted))},{deleted} +{int(bool(added))},{added} @@\n').encode()
    raw += '-旧注释\n'.encode('gbk') * deleted + b'+int new;\n' * added
    if added + deleted > 30000:
        with pytest.raises(ReviewError, match='30001 行.*30000 行') as error:
            validate_single_size(snapshot, raw=raw)
        assert error.value.error_code == 'PR_TOTAL_CHANGE_LIMIT'
    else:
        counts = validate_single_size(snapshot, raw=raw)
        assert (counts['added_lines'], counts['deleted_lines']) == (added, deleted)


def test_pr_agent_context_does_not_depend_on_hidden_codex_compaction():
    config = {'engine': 'pr_agent', 'model_context_tokens': 32768,
              'compaction_threshold_tokens': 800000, 'model_max_output_tokens': 8192}
    validate_context(config)
    with pytest.raises(ReviewError, match='压缩阈值'):
        validate_context(dict(config, engine='codex_cli'))
    with pytest.raises(ReviewError, match='输出预留'):
        validate_context(dict(config, model_max_output_tokens=32768))


@pytest.mark.parametrize('foreign_input', [False, True])
def test_pr_agent_orphan_cleanup_matches_frozen_session(tmp_path, monkeypatch, foreign_input):
    runtime_source = Path(__file__).resolve().parents[2] / 'review-runtime'
    monkeypatch.syspath_prepend(str(runtime_source))
    import native_session

    runtime = tmp_path / 'runtime'
    bindings = runtime / 'bindings'
    bindings.mkdir(parents=True)
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    job, session = 'a' * 32, 'b' * 32
    (bindings / (job + '.' + session + '.json')).write_text(json.dumps({
        'session_id': session, 'workspace': str(workspace)}))
    process = tmp_path / 'proc' / '123'
    process.mkdir(parents=True)
    interpreter = '/opt/pr-agent-runtime/bin/python3.12'
    input_path = runtime / ('other' if foreign_input else session) / 'input.json'
    (process / 'cmdline').write_bytes(b'\0'.join(os.fsencode(value) for value in (
        interpreter, runtime_source / 'pr_agent_runner.py', input_path)) + b'\0')
    (process / 'cwd').symlink_to(workspace, target_is_directory=True)
    (process / 'environ').write_bytes(os.fsencode('HOME=' + str(runtime / session)) + b'\0')
    monkeypatch.setenv('AI_REVIEW_NATIVE_ROOT', str(runtime))
    monkeypatch.setenv('AI_REVIEW_PR_AGENT_PYTHON', interpreter)
    real_path = Path
    monkeypatch.setattr(native_session, 'Path', lambda value: tmp_path / 'proc' if value == '/proc' else real_path(value))
    monkeypatch.setattr(native_session.os, 'getpgid', lambda pid: pid)
    killed = []
    monkeypatch.setattr(native_session.os, 'killpg', lambda pid, signal: killed.append(pid))
    assert native_session.NativeSessions()._stop_orphans(job) is foreign_input
    assert killed == ([] if foreign_input else [123])


def runner_module():
    path = Path(__file__).resolve().parents[2] / 'review-runtime' / 'pr_agent_runner.py'
    spec = importlib.util.spec_from_file_location('pr_agent_runner_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mixed_encoding_round_trip_and_final_change_digest():
    import hashlib

    from ai_review.common import digest
    from ai_review.diff import parse_diff, validate_patch
    from ai_review.diff_inline import index, verify_comment
    from ai_review.pr_agent_encoding import decode_blob, pack, render_block, validate_config

    path = 'src/legacy/queue.c'
    old = '// 旧版注释\r\n'.encode('gbk')
    new = '// 新版注释\r\n'.encode()
    raw = (f'diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n'.encode()
           + b'-' + old + b'+' + new)
    rules = validate_config({'.': 'gb18030', 'src/legacy': 'gbk'})
    sides = {'old': decode_blob(old, path, '旧侧', rules)[1],
             'new': decode_blob(new, path, '新侧', rules)[1]}
    assert sides == {'old': 'gbk', 'new': 'utf-8'}
    patch, = parse_diff(raw)
    anchors = validate_patch(patch, old, new, 'snapshot', sides)
    assert [a['quote'] for a in anchors] == ['// 旧版注释', '// 新版注释']
    diff = render_block(raw, patch, sides)
    assert '-// 旧版注释' in diff
    snapshot = {'id': 'snapshot', 'pr_agent_protocol': 1, 'diff': diff,
                'raw_diff': pack(raw), 'diff_sha256': hashlib.sha256(raw).hexdigest()}
    report_anchors, _ = index(snapshot)
    gitea_projection = ''.join('\ufffd' if 0xdc80 <= ord(c) <= 0xdcff else c
                               for c in old.decode('utf-8', 'surrogateescape').rstrip('\r\n'))
    snippet = '@@ -1 +1 @@\n-' + gitea_projection + '\n+// 新版注释'
    verify_comment(snippet, report_anchors['R2'], snapshot)
    with pytest.raises(ReviewError, match='评论片段与固定 PR diff 不一致'):
        verify_comment(snippet.replace('新版注释', '错误注释'), report_anchors['R2'], snapshot)
    runner = runner_module()
    rendered = f"## File: '{path}'\n@@ -1 +1 @@\n__new hunk__\n1 +// 新版注释\r\n__old hunk__\n-// 旧版注释"
    expected = {path: digest([('+', 1, '// 新版注释'), ('-', 1, '// 旧版注释')])}
    assert runner.rendered_changes(rendered) == expected
    assert runner.rendered_changes(rendered.rsplit('\n', 1)[0]) != expected
    with pytest.raises(ReviewError, match='旧侧.*第 1 行.*第 4 字节'):
        decode_blob(old, path, '旧侧', {})
    with pytest.raises(ReviewError, match='固定版本旧文件不一致'):
        validate_patch(patch, b'changed\n', new, 'snapshot', sides)


def test_pr_agent_rejects_gitea_diff_omitting_a_whole_file(tmp_path):
    import hashlib
    import subprocess

    from ai_review.native_workspace import bind_snapshot_evidence
    from ai_review.pr_agent_encoding import pack

    repo = tmp_path / 'repo'
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args])

    git('init', '-q')
    git('config', 'user.name', 'Review')
    git('config', 'user.email', 'review@example.invalid')
    for path in ('first.c', 'second.c'):
        (repo / path).write_text('int old;\n')
    git('add', '.')
    git('commit', '-qm', '基线')
    base = git('rev-parse', 'HEAD').decode().strip()
    for path in ('first.c', 'second.c'):
        (repo / path).write_text('int new;\n')
    git('commit', '-qam', '固定变更')
    head = git('rev-parse', 'HEAD').decode().strip()
    raw = git('diff', base, head, '--', 'first.c')

    store = Store(tmp_path / 'review.db')
    store.save_config(dict(store.config(), enabled=True, debounce_seconds=0, engine='pr_agent'))
    repository = store.save_repo(1, 1, {'enabled': True, 'repo_name': 'owner/repo'})
    store.enqueue(repository['id'], 1, {'head': head, 'base': base})
    job = store.claim()
    snapshot = {'head': head, 'merge_base': base, 'id': '固定快照', 'source_encodings': {},
                'workspace_layout': 'git-worktree-v2', 'pr_agent_protocol': 1,
                'raw_diff': pack(raw), 'diff_sha256': hashlib.sha256(raw).hexdigest(),
                'coverage': {'files_total': 1}}
    result = bind_snapshot_evidence(store, job, snapshot, repo)
    assert not result['coverage']['complete']
    assert result['coverage']['skipped'][0]['path'] == 'second.c'


def test_pr_agent_history_checks_code_without_structural_diff_marker():
    from ai_review.issue_lifecycle import validate_check

    payload = {'issue_id': '问题编号', 'candidates': [],
               'historical_diff': {'H1': '+++ b/writer.c', 'H2': '@@ -1 +1 @@', 'H3': '+    acknowledge();'},
               'current_diff': {'C1': '@@ -1 +1 @@', 'C2': '+    if (failed) return;'}}
    snapshot = {'id': '固定版本', 'pr_agent_protocol': 1}
    raw = {'issue_id': '问题编号', 'snapshot_id': '固定版本', 'status': 'fixed', 'reason': '失败时在确认前退出',
           'relations': {}, 'evidence': [{'id': 'H3', 'quote': '    acknowledge();'},
                                        {'id': 'C2', 'quote': '    if (failed) return;'}]}
    assert validate_check(raw, payload, snapshot)['evidence'][0]['quote'] == '+    acknowledge();'
    for entry in ({'id': 'H1', 'quote': '++ b/writer.c'}, {'id': 'H3', 'quote': '    acknowledge(other);'}):
        raw['evidence'][0] = entry
        with pytest.raises(ReviewError, match='不属于固定 diff 原文'):
            validate_check(raw, payload, snapshot)


def configure_case(case):
    case.snapshot['engine'] = 'pr_agent'
    case.store.update(case.job['id'], case.job['owner'], snapshot=case.snapshot)
    with case.store.tx() as connection:
        for table in ('settings', 'configuration_history'):
            key = 'id' if table == 'settings' else 'revision'
            number = 1 if table == 'settings' else case.job['config_revision']
            row = connection.execute(f'SELECT value FROM {table} WHERE {key}=?', (number,)).fetchone()
            value = dict(json.loads(row[0]), engine='pr_agent')
            connection.execute(f'UPDATE {table} SET value=? WHERE {key}=?', (json.dumps(value), number))


@pytest.mark.parametrize('effort', ['low', 'high', 'max'])
def test_pr_agent_thinking_uses_selected_effort(effort):
    assert parameters({'engine': 'pr_agent', 'thinking_protocol': 'deepseek',
                       'thinking_enabled': True, 'reasoning_effort': effort}) == {
        'thinking': {'type': 'enabled'}, 'reasoning_effort': effort}


def test_pr_agent_disabled_thinking():
    assert parameters({'engine': 'pr_agent', 'thinking_protocol': 'deepseek',
                       'thinking_enabled': False}) == {'thinking': {'type': 'disabled'}}


def test_pr_agent_jobs_keep_engine_and_use_shared_concurrency(tmp_path):
    store = Store(tmp_path / 'review.db')
    config = dict(store.config(), enabled=True, debounce_seconds=0, engine='pr_agent', max_concurrent_reviews=2)
    store.save_config(config)
    repo = store.save_repo(1, 1, {'enabled': True, 'gitea_url': 'http://test', 'repo_name': 'owner/repo'})
    first = store.enqueue(repo['id'], 1, {'head': 'b' * 40, 'base': 'a' * 40})
    second = store.enqueue(repo['id'], 2, {'head': 'c' * 40, 'base': 'a' * 40})
    for _ in range(2):
        job = store.claim()
        assert job['id'] in (first, second)
        assert job['batch_protocol'] == 0
        assert job['rules_protocol'] == 1
        assert job['pr_agent_protocol'] == 1
        assert store.job_config(job['id'])['engine'] == 'pr_agent'
    assert store.claim() is None
    store.save_config(dict(store.config(), engine='codex_cli'))
    assert store.job(first)['configuration']['engine'] == 'pr_agent'
    with pytest.raises(Stale):
        store.job_config(first)


def test_pr_agent_result_requires_severity_and_location():
    normalize = runner_module().normalize
    data = {'review': {'key_issues_to_review': [{'issue_header': '[P1] 越界访问',
        'issue_content': '边界条件使循环访问缓冲区末尾之外。', 'relevant_file': 'src/copy.c',
        'start_line': 10, 'end_line': 11}]}}
    output = normalize(data)
    assert output['findings'][0]['priority'] == 1
    assert output['findings'][0]['code_location']['line_range'] == {'start': 10, 'end': 11}
    data['review']['key_issues_to_review'][0]['issue_header'] = '越界访问'
    with pytest.raises(ValueError, match='priority_missing'):
        normalize(data)
    assert normalize({'review': {'key_issues_to_review': []}})['findings'] == []
    with pytest.raises(ValueError, match='shape_invalid'):
        normalize({'review': {'security_concerns': '无'}})


def test_pr_agent_native_title_uses_independent_severity():
    module = runner_module()
    data = {'review': {'key_issues_to_review': [{'severity': 'P2',
        'issue_header': '重叠缓冲区', 'issue_content': '调用约束未说明是否支持重叠。',
        'relevant_file': 'copy.c', 'start_line': 3, 'end_line': 3}]}}
    finding = module.normalize(data)['findings'][0]
    assert finding['priority'] == 2
    assert finding['title'] == '[P2] 重叠缓冲区'
    data['review']['key_issues_to_review'][0]['issue_header'] = '[P1] 重叠缓冲区'
    with pytest.raises(ValueError, match='priority_conflict'):
        module.normalize(data)
    template = 'class KeyIssuesComponentLink(BaseModel):\n    issue_header: str\n      issue_header: |'
    adapted = module.priority_prompt(template, schema=True)
    assert 'severity: Literal["P0", "P1", "P2", "P3"]' in adapted
    assert '      severity: P2\n      issue_header: |' in adapted


@pytest.mark.parametrize('findings', [False, True])
def test_pr_agent_automatic_publication(case, findings):
    configure_case(case)
    result(case, findings)
    send(case)
    assert case.store.job(case.job['id'])['status'] == 'succeeded'
    assert bool(case.client.code.get(1)) == findings
    assert len(case.client.summaries) == 1
    with case.store.connect() as connection:
        assert connection.execute('SELECT count(*) FROM review_publication_approvals').fetchone()[0] == 0


def test_pr_agent_incomplete_result_blocks_publication(case):
    configure_case(case)
    result(case)
    case.store.update(case.job['id'], case.job['owner'], result=dict(case.job['result'], analysis_complete=False))
    case.job = case.store.job(case.job['id'])
    with pytest.raises(ReviewError, match='尚未完整结束'):
        send(case)
    assert not case.client.summaries


def test_pr_agent_lost_receipt_does_not_duplicate(case):
    configure_case(case)
    result(case)
    case.client.summary_failure = True
    from ai_review.common import PublishUnknown
    with pytest.raises(PublishUnknown):
        send(case)
    retry(case)
    assert case.store.job(case.job['id'])['status'] == 'succeeded'
    assert len(case.client.summaries) == 1

"""独立进程调用固定版本 PR-Agent，仅输出 Manager 会话事件。"""
import asyncio
import contextlib
from copy import copy
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sys

VERSION = '0.49.0'
FILE_HEADER = re.compile(r"## File: '(.*)'")
HUNK_HEADER = re.compile(r'@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@')
NEW_CHANGE = re.compile(r'(\d+) \+(.*)')


def rendered_changes(text):
    """核对原生编号补丁的路径、侧、行号和文本，保留重复行的计数。"""
    files = {}
    path = side = old = None
    for raw in text.split('\n'):
        line = raw.removesuffix('\r')
        if match := FILE_HEADER.fullmatch(line):
            path = match[1]
            files.setdefault(path, [])
            side = old = None
        elif match := HUNK_HEADER.match(line):
            old = int(match[1])
            side = None
        elif line == '__new hunk__':
            side = 'new'
        elif line == '__old hunk__':
            side = 'old'
        elif path and side == 'new' and (match := NEW_CHANGE.fullmatch(line)):
            files[path].append(('+', int(match[1]), match[2]))
        elif path and side == 'old' and old is not None and line.startswith(('-', ' ')):
            if line.startswith('-'):
                files[path].append(('-', old, line[1:]))
            old += 1
    return {path: hashlib.sha256(json.dumps(sorted(rows), ensure_ascii=False, separators=(',', ':'))
                                 .encode()).hexdigest() for path, rows in files.items()}


def emit(kind, **value):
    sys.__stdout__.write(json.dumps({'type': kind, **value}, ensure_ascii=False) + '\n')
    sys.__stdout__.flush()


def priority_prompt(template, schema=False):
    if schema:
        marker = 'class KeyIssuesComponentLink(BaseModel):\n'
        if template.count(marker) != 1:
            raise ValueError('pr_agent_version_mismatch')
        template = template.replace(marker, marker +
            '    severity: Literal["P0", "P1", "P2", "P3"] = Field(description="P0仅限无需特定输入或环境条件、普遍造成灾难性后果的问题。'
            'P1用于真实可达且严重到阻止合并的缺陷。P2/P3用于其他非阻断意见。")\n')
    return template.replace('      issue_header: |', '      severity: P2\n      issue_header: |')


def normalize(data):
    review = data.get('review')
    if not isinstance(review, dict) or not isinstance(review.get('key_issues_to_review'), list):
        raise ValueError('pr_agent_review_shape_invalid')
    findings = []
    for issue in review['key_issues_to_review']:
        title = str(issue.get('issue_header', '')).strip()
        match = re.match(r'^\[P([0-3])\]\s*(.+)', title, re.S)
        severity = issue.get('severity') or ('P' + match[1] if match else None)
        if severity not in ('P0', 'P1', 'P2', 'P3'):
            raise ValueError('pr_agent_priority_missing')
        if match and severity != 'P' + match[1]:
            raise ValueError('pr_agent_priority_conflict')
        title = match[2] if match else title
        if not title:
            raise ValueError('pr_agent_finding_invalid')
        path = issue.get('relevant_file')
        body = issue.get('issue_content')
        start, end = issue.get('start_line'), issue.get('end_line')
        if not isinstance(path, str) or not isinstance(body, str) or not body.strip():
            raise ValueError('pr_agent_finding_invalid')
        if type(start) is not int or type(end) is not int or not 1 <= start <= end:
            raise ValueError('pr_agent_location_invalid')
        findings.append({'title': '[' + severity + '] ' + title, 'body': body.strip(), 'priority': int(severity[1]),
                         'code_location': {'absolute_file_path': path.strip(),
                                           'line_range': {'start': start, 'end': end}}})
    return {'findings': findings, 'overall_explanation': json.dumps(data, ensure_ascii=False),
            'overall_correctness': 'patch is incorrect' if any(x['priority'] < 2 for x in findings) else 'patch is correct',
            '_pr_agent_version': VERSION, '_pr_agent_review': data,
            '_review_completion': {'complete': True, 'finish_reason': 'stop'}}


async def review(payload):
    if importlib.metadata.version('pr-agent') != VERSION:
        raise ValueError('pr_agent_version_mismatch')
    from pr_agent.config_loader import get_settings
    from pr_agent.log import get_logger
    from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
    from pr_agent.commands.pr_reviewer import PRReviewer
    from pr_agent.commands import pr_reviewer
    from pr_agent.algo import pr_processing
    from pr_agent.algo.types import EDIT_TYPE
    from pr_agent.git_providers.diff_parsing import parse_unified_diff
    from tenacity import stop_after_attempt
    task = payload['input']
    from ai_review.pr_agent_encoding import AgentCoverageError, PROTOCOL, uses_local_git
    local_git = uses_local_git(task.get('pr_agent_protocol'))
    provider = 'plain-diff'
    expected = task.get('expected_changes')
    checked_diff = None
    if expected is not None or local_git:
        from pr_agent.algo import language_handler
        from pr_agent.git_providers import plain_diff_provider

        # Manager 已在 Git 层区分普通文本与不支持内容。撤掉这次调用的扩展名过滤，
        # 仍用原生语言排序、补丁渲染与 Token 预算。
        language_handler.filter_bad_extensions = list
        plain_diff_provider.partition_files_by_language_filter = lambda files: (files, [])
    if local_git:
        from pr_agent.git_providers import local_git_provider, register_git_provider
        from pr_agent_local import local_provider

        provider = 'manager-local'
        register_git_provider(provider, local_provider(task))
        local_git_provider.partition_files_by_language_filter = lambda files: (files, [])

    native_render = pr_processing.decouple_and_convert_to_hunks_with_lines_numbers

    def render_patch(patch, file):
        # 上游原生渲染会把整文件删除压成一行文件名。保留整个旧侧补丁并参与同一次 Token 预算计算。
        if file and file.edit_type == EDIT_TYPE.DELETED:
            file = copy(file)
            file.edit_type = EDIT_TYPE.MODIFIED
        return native_render(patch, file)

    pr_processing.decouple_and_convert_to_hunks_with_lines_numbers = render_patch
    native_get_diff = pr_reviewer.get_pr_diff

    def complete_diff(*args, **kwargs):
        nonlocal checked_diff
        # 原生压缩会省略部分删除内容，压缩后的输入不能作为完整审查。
        kwargs['return_prepared'] = True
        prepared = native_get_diff(*args, **kwargs)
        if prepared.file_dict is not None:
            raise ValueError('pr_agent_context_incomplete')
        if expected is not None:
            if rendered_changes(prepared.diff) != expected:
                raise ValueError('pr_agent_changes_missing')
            checked_diff = prepared.diff.strip('\n')
        return prepared

    pr_reviewer.get_pr_diff = complete_diff

    get_logger().remove()
    settings = get_settings()
    model = 'openai/' + payload['model']
    values = {
        'config.git_provider': provider, 'config.model': model,
        'config.weak_model': model, 'config.fallback_models': [], 'config.num_retries': 0,
        'config.ai_timeout': payload['timeout'], 'config.propagate_command_errors': True,
        'config.response_language': 'zh-CN', 'config.publish_output': False,
        'config.publish_output_progress': False, 'config.verbosity_level': 0,
        'config.custom_model_max_tokens': payload['context_tokens'],
        'config.max_model_tokens': payload['context_tokens'],
        'config.reasoning_effort': payload['reasoning_effort'],
        'config.additional_reasoning_effort_models': [model],
        'config.repo_context_files': [], 'config.ignore_language_framework': [],
        'config.ignore_pr_language': [], 'ignore.glob': [], 'ignore.regex': [],
        'skills.enabled': False, 'artifacts.enable': False,
        'pr_reviewer.enable_large_pr_chunking': False,
        'pr_reviewer.num_max_findings': payload['max_findings'],
        'pr_reviewer.persistent_finding_state': False,
        'pr_reviewer.inline_key_issues': False,
        'openai.api_base': payload['gateway'], 'openai.key': os.environ['PR_AGENT_API_KEY'],
        'litellm.timeout': payload['timeout'],
        'plain_diff.content': task.get('diff'), 'plain_diff.title': task['title'],
        'plain_diff.description': task['description'],
    }
    if local_git:
        values.update({'config.allow_dynamic_context': True, 'config.patch_extra_lines_before': 5,
                       'config.patch_extra_lines_after': 1, 'config.max_extra_lines_before_dynamic_context': 10})
    elif expected is not None:
        values.update({'config.patch_extra_lines_before': 0, 'config.patch_extra_lines_after': 0})
    for key, value in values.items():
        settings.set(key, value)
    import litellm
    litellm.telemetry = False

    class MeasuredHandler(LiteLLMAIHandler):
        async def chat_completion(self, model, system, user, temperature=0.2, img_path=None):
            call = self._chat_completion_with_retry.retry_with(stop=stop_after_attempt(1))
            return await call(self, model, system, user, temperature, img_path,
                              configured_deployment_id=self.deployment_id)

        async def _acompletion(self, *args, **kwargs):
            kwargs['num_retries'] = 0
            kwargs['stream'] = False
            if payload['output_tokens']:
                kwargs['max_tokens'] = payload['output_tokens']
            if expected is not None and (not checked_diff or not any(
                message.get('role') == 'user' and checked_diff in message.get('content', '')
                for message in kwargs.get('messages', ())
            )):
                raise ValueError('pr_agent_changes_missing')
            response = await super()._acompletion(*args, **kwargs)
            usage = response.usage.model_dump() if response.usage else {}
            emit('usage', usage={'input_tokens': usage.get('prompt_tokens', 0),
                                 'output_tokens': usage.get('completion_tokens', 0)})
            finish = response.choices[0].finish_reason if response.choices else None
            if finish == 'length' and task.get('pr_agent_protocol'):
                raise ValueError('pr_agent_response_length')
            if finish != 'stop':
                raise ValueError('pr_agent_response_incomplete')
            return response

    if task['phase'].startswith('history-'):
        answer, finish = await MeasuredHandler().chat_completion(
            model, '只按给定的历史问题复核约定输出完整JSON对象。', task['instructions'])
        if finish != 'stop':
            raise ValueError('pr_agent_response_incomplete')
        text = answer.strip()
        if text.startswith('```') and text.endswith('```'):
            text = '\n'.join(text.splitlines()[1:-1])
        json.loads(text)
        return {'findings': [], 'overall_explanation': text,
                '_pr_agent_version': VERSION, '_review_completion': {'complete': True, 'finish_reason': 'stop'}}

    settings.set('pr_review_prompt.system', priority_prompt(settings.pr_review_prompt.system, schema=True))
    settings.set('pr_review_prompt.user', priority_prompt(settings.pr_review_prompt.user))
    if local_git:
        reviewer = PRReviewer('gm-review-base-' + payload['job_id'], ai_handler=MeasuredHandler)
        reviewer.vars.update(title=task['title'], description=task['description'])
        files = reviewer.git_provider.get_diff_files()
        expected = reviewer.git_provider.expected_changes
    else:
        files = parse_unified_diff(task['diff'])
    deletion_instructions = ''
    if any(file.edit_type == EDIT_TYPE.DELETED for file in files):
        deletion_instructions = (
            '同时检查整文件删除引入的回归。对于已删除文件，使用旧侧 hunk 的行号报告位置。\n')
    extra = (
        '使用中文。每项severity字段填写P0、P1、P2或P3，issue_header保持简短标题。'
        'P0/P1仅用于当前改动造成、真实可达且严重到阻止合并的缺陷。'
        '核对调用约束和已有保护，正文给出触发条件、因果链、影响和证据。'
        '普通优化和轻微问题使用P2/P3。没有成立问题时key_issues_to_review返回空列表。\n'
        + deletion_instructions + task['instructions'])
    settings.set('pr_reviewer.extra_instructions', extra)
    if local_git:
        reviewer.vars['extra_instructions'] = extra
    else:
        reviewer = PRReviewer('manager', ai_handler=MeasuredHandler)
    expected_paths = {item.filename for item in files}
    actual_paths = {item.filename for item in reviewer.git_provider.get_diff_files()}
    if actual_paths != expected_paths or reviewer.git_provider.get_filtered_diff_file_names():
        if task.get('pr_agent_protocol') == PROTOCOL:
            filtered = set(reviewer.git_provider.get_filtered_diff_file_names())
            raise AgentCoverageError('pr_agent_files_filtered', expected_files=len(expected_paths),
                                     actual_files=len(actual_paths),
                                     filtered_paths=sorted(filtered | (expected_paths - actual_paths))[:50])
        raise ValueError('pr_agent_review_incomplete')
    # plain-diff构造器为命令行输出启用发布，这里由Manager统一发布。
    settings.set('config.publish_output', False)
    await reviewer.run()
    if task.get('pr_agent_protocol') == PROTOCOL:
        if not reviewer.prediction:
            raise ValueError('pr_agent_prediction_missing')
        if (reviewer.remaining_files_list or reviewer.partial_files_list
                or reviewer.review_failed_chunk_count or reviewer.review_chunk_count != 1):
            raise AgentCoverageError('pr_agent_chunks_incomplete', chunks=reviewer.review_chunk_count,
                                     failed_chunks=reviewer.review_failed_chunk_count,
                                     remaining_files=len(reviewer.remaining_files_list or []),
                                     partial_files=len(reviewer.partial_files_list or []))
    if (not reviewer.prediction or reviewer.remaining_files_list or reviewer.partial_files_list
            or reviewer.review_failed_chunk_count or reviewer.review_chunk_count != 1):
        raise ValueError('pr_agent_review_incomplete')
    if reviewer.review_chunk_hit_limit:
        raise ValueError('pr_agent_findings_limit')
    data = reviewer.prediction_data or reviewer._load_valid_review_yaml(reviewer.prediction)
    result = normalize(data)
    if local_git:
        result['_pr_agent_local_input'] = {'changes': reviewer.git_provider.change_counts,
                                           'files': len(expected), 'dynamic_context': True}
    if len(result['findings']) >= payload['max_findings']:
        raise ValueError('pr_agent_findings_limit')
    return result


def main():
    os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
    payload = json.loads(Path(sys.argv[1]).read_text())
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = asyncio.run(review(payload))
        emit('result', result=result)
    except Exception as error:
        cause = error
        code = 'pr_agent_execution_failed'
        for _ in range(8):
            if re.fullmatch(r'pr_agent_[a-z_]+', str(cause)):
                code = str(cause)
                break
            cause = cause.__cause__ or cause.__context__
            if cause is None:
                break
        emit('error', code=code, exception_type=type(error).__name__,
             **({'details': cause.details} if hasattr(cause, 'details') else {}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""将固定 PR 和仓库规范交给 PR-Agent 原生审查，复用现有阶段与发布编排。"""
import json

from .common import digest, encode
from .diff import parse_diff, quote_text
from .pr_agent_encoding import PROTOCOL, uses_local_git
from .repository_rules import instructions


def phase_input(snapshot, phase, candidates=None):
    extra = instructions(snapshot)
    if phase.startswith('refutation-'):
        extra += (
            '\n只独立核对以下单个候选，检查触发路径、已有保护和严重级别。'
            '仅返回仍成立的该问题并沿用其代码区间，排除后返回空问题列表。'
            '在 security_concerns 中解释核对依据。\n' + encode(candidates)
        )
    elif phase.startswith('history-'):
        payload = candidates['payload']
        if snapshot.get('pr_agent_protocol') == PROTOCOL:
            payload = dict(sorted(payload.items()))
            for key in ('historical_diff', 'current_diff', 'current_source'):
                if key in payload:
                    payload[key] = dict(sorted(payload[key].items(), key=lambda item: int(item[0][1:])))
            evidence = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        else:
            evidence = encode(payload)
        extra += '\n' + candidates['instructions'] + '\n' + evidence
    result = {key: snapshot[key] for key in ('head', 'merge_base', 'title', 'description')} | {
        'phase': phase, 'instructions': extra,
    }
    if snapshot.get('pr_agent_protocol'):
        result['pr_agent_protocol'] = snapshot['pr_agent_protocol']
    if uses_local_git(result.get('pr_agent_protocol')):
        if result['pr_agent_protocol'] == PROTOCOL:
            result['review_paths'] = sorted(file['path'] for file in snapshot['files'])
        return dict(result, source_encodings=snapshot['source_encodings'],
                    max_changed_lines=snapshot['max_changed_lines'])
    result['diff'] = snapshot['diff']
    if result.get('pr_agent_protocol') and not phase.startswith('history-'):
        coverage = {}
        for patch in parse_diff(snapshot['diff'].encode()):
            rows = []
            for hunk in patch.hunks:
                old = hunk.old_start - bool(hunk.old_count)
                new = hunk.new_start - bool(hunk.new_count)
                for kind, raw in hunk.rows:
                    old += kind != '+'
                    new += kind != '-'
                    if kind in ('+', '-'):
                        rows.append((kind, new if kind == '+' else old, quote_text(raw)))
            coverage[patch.new_path or patch.old_path] = digest(sorted(rows))
        result['expected_changes'] = coverage
    return result

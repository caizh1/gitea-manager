"""将固定 PR 和仓库规范交给 PR-Agent 原生审查，复用现有阶段与发布编排。"""
from .common import encode
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
        extra += '\n' + candidates['instructions'] + '\n' + encode(candidates['payload'])
    return {key: snapshot[key] for key in ('head', 'merge_base', 'title', 'description', 'diff')} | {
        'phase': phase, 'instructions': extra,
    }

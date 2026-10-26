"""把来源项目合并条件转为接收端的最终合并授权门禁。"""
from svn_migration.common import require

CI_ISSUER = 'gitlab:ci:issuer'
CI_CONTEXT = 'migration/gitlab-pipeline'


def needs_pipeline(setting):
    return any(setting.get(key) is True for key in ('only_allow_merge_if_pipeline_succeeds', 'only_allow_merge_if_build_succeeds'))


def convert(inventory, kind, mapping, normalized):
    for dataset in inventory['datasets']:
        if dataset['category'] != '项目合并前置条件':
            continue
        setting = dataset['rows'][0]
        if kind == 'gitlab':
            if setting.get('only_allow_merge_if_all_status_checks_passed'):
                continue  # 企业外部检查继续明确保留为未验证。
            method, squash = setting.get('merge_method') or 'merge', setting.get('squash_option') or 'default_off'
            require(method in ('merge', 'rebase_merge', 'ff') and squash in ('never', 'always', 'default_on', 'default_off'),
                    '来源合并方式不在经过定义的转换范围')
            ci = needs_pipeline(setting)
            require(not ci or len(mapping.get(CI_ISSUER, [])) == 1, '强制 CI 需要明确一个可信目标 CI 回执账号')
            styles = (['fast-forward-only'] if method == 'ff' else ['merge']) if squash != 'always' else []
            if squash != 'never':
                styles.append('squash')
            policy = {'source_kind': kind, 'method': method, 'squash': squash, 'allowed_styles': styles,
                      'require_pipeline': ci, 'allow_skipped_pipeline': bool(setting.get('allow_merge_on_skipped_pipeline')),
                      'require_discussions': bool(setting.get('only_allow_merge_if_all_discussions_are_resolved')),
                      'ci_context': CI_CONTEXT if ci else '', 'ci_user_id': mapping[CI_ISSUER][0]['id'] if ci else 0,
                      'default_style': 'squash' if squash in ('always', 'default_on') else styles[0]}
        else:
            fields = {'allow_merge_commits': 'merge', 'allow_rebase': 'rebase', 'allow_rebase_explicit': 'rebase-merge',
                      'allow_squash_merge': 'squash', 'allow_fast_forward_only_merge': 'fast-forward-only', 'allow_manual_merge': 'manually-merged'}
            if not any(key in setting for key in fields):
                continue
            styles = [style for key, style in fields.items() if setting.get(key)]
            require(styles, '来源禁用了全部合并方式，需要明确恢复策略')
            policy = {'source_kind': kind, 'method': '', 'squash': '', 'allowed_styles': styles,
                      'require_pipeline': False, 'allow_skipped_pipeline': False, 'require_discussions': False,
                      'ci_context': '', 'ci_user_id': 0, 'default_style': setting.get('default_merge_style') or styles[0]}
            policy['autodetect_manual_merge'] = bool(setting.get('autodetect_manual_merge'))
            require(policy['default_style'] in styles, '来源默认合并方式与允许列表不一致')
        normalized['objects'].append({'kind': 'merge_policy', 'source_id': 'project', 'title': '迁入合并条件',
                                      'created': 0, 'history': policy})
        normalized['pending'] = [p for p in normalized['pending'] if p.get('path') != dataset['path']]

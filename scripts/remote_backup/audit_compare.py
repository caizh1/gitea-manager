"""仅允许原生启动核对追加的已知审计；原有审计及关联范围必须逐行保留。"""
import json


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def recovery_append_only(expected, actual):
    event_table, scope_table = 'governance_audit_event', 'governance_audit_scope'
    if not all(table in expected and table in actual for table in (event_table, scope_table)):
        return False
    try:
        old_events = {row['event_id']: row for row in expected[event_table]}
        new_events = {row['event_id']: row for row in actual[event_table]}
        if len(new_events) != len(actual[event_table]) or any(new_events.get(key) != row for key, row in old_events.items()):
            return False
        operations = {row['id']: row for row in expected.get('governance_repository_creation', [])}
        extra = {}
        for key in new_events.keys() - old_events.keys():
            row = new_events[key]
            actor, details = decoded(row['actor']), decoded(row['details'])
            operation = operations.get(details.get('operation_id'))
            if (not operation or operation['state'] != 'unknown'
                    or row['type'] != 'repository.creation_recovery_required'
                    or row['result'] != 'unknown' or row['actor_id'] != 0
                    or actor.get('kind') != 'system' or actor.get('transport') != 'background'
                    or actor.get('name') != '项目创建恢复核对任务'
                    or details != {'operation_id': operation['id'], 'origin': operation['origin'],
                                   'reason_code': operation['last_reason_code']}
                    or (row['scope_type'], row['scope_id']) != ('repository', operation['repo_id'])
                    or (row['object_type'], row['object_id']) != ('repository', operation['repo_id'])
                    or row['object_path'] != operation['object_path']
                    or row['id'] <= max((event['id'] for event in old_events.values()), default=0)):
                return False
            ancestors = decoded(row['ancestor_i_ds']) or []
            if ancestors != (decoded(operation['ancestor_i_ds']) or []):
                return False
            scopes = {('instance', 0), ('repository', operation['repo_id'])}
            scopes.update(('group', identifier) for identifier in ancestors)
            extra[row['id']] = (row, scopes)
        old_scopes = {row['id']: row for row in expected[scope_table]}
        new_scopes = {row['id']: row for row in actual[scope_table]}
        if len(new_scopes) != len(actual[scope_table]) or any(new_scopes.get(key) != row for key, row in old_scopes.items()):
            return False
        seen = {key: set() for key in extra}
        for key in new_scopes.keys() - old_scopes.keys():
            row = new_scopes[key]
            if row['event_sequence'] not in extra:
                return False
            event, allowed = extra[row['event_sequence']]
            scope = (row['scope_type'], row['scope_id'])
            if scope not in allowed or scope in seen[event['id']] or row['occurred_at'] != event['occurred_at']:
                return False
            seen[event['id']].add(scope)
        return all(seen[key] == allowed for key, (_, allowed) in extra.items())
    except (KeyError, TypeError, ValueError):
        return False

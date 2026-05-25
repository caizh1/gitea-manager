from datetime import datetime

from flask import Blueprint, request, jsonify
from flask_login import login_required

from models import db, GiteaServer, CommitMessageRule, CommitGateAssignment
from services.commit_gate_service import (
    DEFAULT_PATTERN,
    DEFAULT_REJECT_MESSAGE,
    apply_rule,
    list_repos_with_assignments,
    remove_gate,
    rule_to_dict,
    test_message,
)

commit_gate_bp = Blueprint('commit_gate', __name__)


@commit_gate_bp.route('/commit-rules', methods=['GET'])
@login_required
def list_rules():
    server_id = request.args.get('server_id', type=int)
    query = CommitMessageRule.query
    if server_id:
        query = query.filter_by(server_id=server_id)
    rules = query.order_by(CommitMessageRule.created_at.desc()).all()
    return jsonify({
        'rules': [rule_to_dict(r) for r in rules],
        'default_rule': {
            'name': 'Conventional Commits',
            'pattern': DEFAULT_PATTERN,
            'reject_message': DEFAULT_REJECT_MESSAGE,
        },
    })


@commit_gate_bp.route('/commit-rules', methods=['POST'])
@login_required
def create_rule():
    data = request.get_json() or {}
    server_id = data.get('server_id')
    server = GiteaServer.query.get(server_id)
    if not server:
        return jsonify({'error': 'Server not found'}), 404

    name = (data.get('name') or '').strip()
    pattern = (data.get('pattern') or '').strip()
    if not name or not pattern:
        return jsonify({'error': 'Name and pattern are required'}), 400

    result = test_message(pattern, '[ID-1] fix: test message')
    if not result.get('ok'):
        return jsonify({'error': 'Invalid pattern', 'detail': result.get('error')}), 400

    rule = CommitMessageRule(
        server_id=server.id,
        name=name,
        pattern=pattern,
        reject_message=(data.get('reject_message') or DEFAULT_REJECT_MESSAGE).strip(),
        enabled=bool(data.get('enabled', True)),
        created_at=datetime.utcnow(),
    )
    db.session.add(rule)
    db.session.commit()
    return jsonify(rule_to_dict(rule)), 201


@commit_gate_bp.route('/commit-rules/<int:rule_id>', methods=['PUT'])
@login_required
def update_rule(rule_id):
    rule = CommitMessageRule.query.get_or_404(rule_id)
    data = request.get_json() or {}

    if 'name' in data:
        name = (data.get('name') or '').strip()
        if not name:
            return jsonify({'error': 'Name is required'}), 400
        rule.name = name
    if 'pattern' in data:
        pattern = (data.get('pattern') or '').strip()
        if not pattern:
            return jsonify({'error': 'Pattern is required'}), 400
        result = test_message(pattern, '[ID-1] fix: test message')
        if not result.get('ok'):
            return jsonify({'error': 'Invalid pattern', 'detail': result.get('error')}), 400
        rule.pattern = pattern
    if 'reject_message' in data:
        rule.reject_message = (data.get('reject_message') or DEFAULT_REJECT_MESSAGE).strip()
    if 'enabled' in data:
        rule.enabled = bool(data.get('enabled'))

    rule.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify(rule_to_dict(rule))


@commit_gate_bp.route('/commit-rules/<int:rule_id>', methods=['DELETE'])
@login_required
def delete_rule(rule_id):
    rule = CommitMessageRule.query.get_or_404(rule_id)
    assigned = CommitGateAssignment.query.filter_by(rule_id=rule.id).count()
    if assigned:
        return jsonify({'error': 'Rule is still applied to repositories'}), 400
    db.session.delete(rule)
    db.session.commit()
    return jsonify({'ok': True})


@commit_gate_bp.route('/commit-gates/repos', methods=['GET'])
@login_required
def list_repos():
    server_id = request.args.get('server_id', type=int)
    if not server_id:
        return jsonify({'error': 'server_id is required'}), 400
    try:
        return jsonify(list_repos_with_assignments(server_id))
    except ValueError as e:
        return jsonify({'error': str(e)}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@commit_gate_bp.route('/commit-gates/apply', methods=['POST'])
@login_required
def apply_gate():
    data = request.get_json() or {}
    try:
        results = apply_rule(
            server_id=data.get('server_id'),
            rule_id=data.get('rule_id'),
            repo_names=data.get('repo_names') or [],
            apply_all=bool(data.get('apply_all')),
        )
        return jsonify({'results': results})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@commit_gate_bp.route('/commit-gates/remove', methods=['POST'])
@login_required
def remove_gate_route():
    data = request.get_json() or {}
    try:
        results = remove_gate(
            server_id=data.get('server_id'),
            repo_names=data.get('repo_names') or [],
            remove_all=bool(data.get('remove_all')),
        )
        return jsonify({'results': results})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@commit_gate_bp.route('/commit-gates/test', methods=['POST'])
@login_required
def test_rule():
    data = request.get_json() or {}
    return jsonify(test_message(data.get('pattern') or '', data.get('message') or ''))

import logging
from datetime import datetime

from models import db, MirrorConfig


_UNSET = object()


def _clamp_percent(percent):
    return max(0, min(int(percent or 0), 100))


def update_mirror_progress(
    config_or_id,
    stage,
    label,
    percent,
    detail='',
    current_repo_name=_UNSET,
    current_repo_index=_UNSET,
    current_repo_total=_UNSET,
    reset_current=False,
):
    config = config_or_id
    if isinstance(config_or_id, int):
        config = MirrorConfig.query.get(config_or_id)
    if not config:
        return

    if reset_current:
        config.current_repo_name = ''
        config.current_repo_index = 0
        config.current_repo_total = 0

    config.progress_stage = stage
    config.progress_label = label
    config.progress_percent = _clamp_percent(percent)
    config.progress_detail = detail or ''
    config.progress_updated_at = datetime.utcnow()

    if current_repo_name is not _UNSET:
        config.current_repo_name = current_repo_name or ''
    if current_repo_index is not _UNSET:
        config.current_repo_index = int(current_repo_index or 0)
    if current_repo_total is not _UNSET:
        config.current_repo_total = int(current_repo_total or 0)

    from services.integration_outbox_service import emit_event
    emit_event(
        f'mirror.{stage}', 'mirror', config.id, stage,
        {'label': label, 'percent': config.progress_percent, 'detail': detail,
         'source_server_id': config.source_server_id, 'target_server_id': config.target_server_id,
         'current_repo_name': config.current_repo_name},
    )

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logging.warning('[mirror-progress] update failed: %s', e)


def mirror_progress_for_response(config):
    return {
        'progress_stage': config.progress_stage,
        'progress_label': config.progress_label,
        'progress_percent': config.progress_percent or 0,
        'progress_detail': config.progress_detail,
        'progress_updated_at': config.progress_updated_at.isoformat() if config.progress_updated_at else None,
        'current_repo_name': config.current_repo_name or '',
        'current_repo_index': config.current_repo_index or 0,
        'current_repo_total': config.current_repo_total or 0,
    }

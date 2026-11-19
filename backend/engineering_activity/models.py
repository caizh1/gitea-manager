"""研发上库的采集配置、仓库检查点和可核对的事件。"""
from models import db


class Collection(db.Model):
    __tablename__ = 'engineering_activity_collections'

    server_id = db.Column(db.Integer, primary_key=True)
    epoch = db.Column(db.String(64), primary_key=True)
    organization_id = db.Column(db.Integer, primary_key=True)
    settings = db.Column(db.JSON, nullable=False, default=dict)
    due = db.Column(db.DateTime, nullable=False, index=True)
    requested = db.Column(db.Integer, nullable=False, default=0)
    started = db.Column(db.DateTime)
    finished = db.Column(db.DateTime)
    status = db.Column(db.String(24), nullable=False, default='queued')
    progress = db.Column(db.JSON, nullable=False, default=dict)


class Repository(db.Model):
    __tablename__ = 'engineering_activity_repositories'

    id = db.Column(db.Integer, primary_key=True)
    server_id = db.Column(db.Integer, nullable=False)
    epoch = db.Column(db.String(64), nullable=False)
    organization_id = db.Column(db.Integer, nullable=False)
    scope_id = db.Column(db.Integer, nullable=False)
    repo_id = db.Column(db.Integer, nullable=False)
    birth = db.Column(db.String(80), nullable=False)
    name = db.Column(db.String(512), nullable=False)
    synced = db.Column(db.DateTime)
    checkpoint = db.Column(db.JSON, nullable=False, default=dict)
    health = db.Column(db.JSON, nullable=False, default=dict)
    __table_args__ = (
        db.UniqueConstraint('server_id', 'epoch', 'organization_id', 'scope_id', 'repo_id', 'birth'),
    )


class Activity(db.Model):
    __tablename__ = 'engineering_activity_events'

    repository = db.Column(db.Integer, db.ForeignKey(Repository.id, ondelete='CASCADE'), primary_key=True)
    kind = db.Column(db.String(24), primary_key=True)
    key = db.Column(db.String(128), primary_key=True)
    occurred = db.Column(db.DateTime, nullable=False)
    day = db.Column(db.String(10), nullable=False)
    person = db.Column(db.String(72), nullable=False)
    name = db.Column(db.String(256), nullable=False)
    values = db.Column(db.JSON, nullable=False)
    __table_args__ = (db.Index('ix_engineering_activity_events_day', 'repository', 'day', 'kind'),)


class Roster(db.Model):
    __tablename__ = 'engineering_activity_rosters'

    server_id = db.Column(db.Integer, primary_key=True)
    epoch = db.Column(db.String(64), primary_key=True)
    team_id = db.Column(db.Integer, primary_key=True)
    day = db.Column(db.String(10), primary_key=True)
    organization_id = db.Column(db.Integer, nullable=False)
    members = db.Column(db.JSON, nullable=False)


class Daily(db.Model):
    __tablename__ = 'engineering_activity_daily'

    repository = db.Column(db.Integer, db.ForeignKey(Repository.id, ondelete='CASCADE'), primary_key=True)
    day = db.Column(db.String(10), primary_key=True)
    kind = db.Column(db.String(24), primary_key=True)
    person = db.Column(db.String(72), primary_key=True)
    branch = db.Column(db.String(512), primary_key=True)
    name = db.Column(db.String(256), nullable=False)
    values = db.Column(db.JSON, nullable=False)


class ClosingEvidence(db.Model):
    __tablename__ = 'engineering_activity_closings'

    server_id = db.Column(db.Integer, primary_key=True)
    epoch = db.Column(db.String(64), primary_key=True)
    organization_id = db.Column(db.Integer, primary_key=True)
    repo_id = db.Column(db.Integer, primary_key=True)
    delivery = db.Column(db.String(64), primary_key=True)
    birth = db.Column(db.DateTime, nullable=False)
    issue = db.Column(db.Integer, nullable=False)
    actor = db.Column(db.Integer, nullable=False)
    closed = db.Column(db.DateTime, nullable=False)
    received = db.Column(db.DateTime, nullable=False)
    sha = db.Column(db.String(64), nullable=False)
    __table_args__ = (db.Index('ix_engineering_activity_closing_issue', 'server_id', 'epoch',
                               'organization_id', 'repo_id', 'issue'),)

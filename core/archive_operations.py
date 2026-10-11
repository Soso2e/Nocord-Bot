"""Small local maintenance entry points; no content or secrets in diagnostics."""
import logging
from pathlib import Path
import sqlite3
from core.discord_archive_sync import get_ingestor
from core.archive_search import create_search

log = logging.getLogger(__name__)


def index_once():
    try:
        ingestor = get_ingestor()
        if ingestor:
            return create_search(ingestor.archive, ingestor.policy).index_pending()
    except Exception as exc:
        log.warning('Archive index retry pending (%s)', type(exc).__name__)
    return 0


def archive_status(archive, policy):
    search = create_search(archive, policy)
    scope, params = search.scope({})
    with archive.connect() as db:
        total = db.execute(f'SELECT COUNT(*) FROM discord_messages m WHERE {scope}', params).fetchone()[0]
        indexable = db.execute(f'SELECT COUNT(*) FROM discord_messages m WHERE {scope} '
                               'AND length(trim(m.content))>0', params).fetchone()[0]
        indexed = db.execute('SELECT COUNT(*) FROM archive_vectors v JOIN discord_messages m '
            'ON v.message_id=m.message_id AND v.revision=m.revision '
            f'WHERE v.model=? AND {scope}', [search.model] + params).fetchone()[0]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        state = dict(db.execute('SELECT key,value FROM archive_state')) if 'archive_state' in tables else {}
        healthy = db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    return dict(healthy=healthy, trust_zone=policy.zone, message_count=total, indexed_count=indexed,
        pending_index_count=indexable-indexed, vector_enabled=search.embed is not None,
        last_event_at=state.get('last_event_at'), last_reconcile_at=state.get('last_reconcile_at'),
        last_reconcile_error=state.get('last_reconcile_error'),
        current_allowlist_count=sum(len(v.get('channels', [])) for v in policy.allow_guilds.values()))


def backup_archive(archive, policy, destination):
    archive.bind_zone(policy.zone)
    destination = Path(destination).resolve()
    if destination == archive.path.resolve() or destination.exists():
        raise ValueError('Backup destination must be a new file')
    destination.parent.mkdir(parents=True, exist_ok=True)
    # SQLite backup API includes committed WAL pages and produces a consistent snapshot.
    with archive.connect() as source:
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()


def restore_archive(source_path, policy, destination):
    source_path, destination = Path(source_path).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ValueError('Stop Nocord and restore into a new path')
    if not source_path.is_file():
        raise ValueError('Backup missing')
    source = sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)
    try:
        zone = source.execute("SELECT value FROM archive_meta WHERE key='trust_zone'").fetchone()
        if not zone or zone[0] != policy.zone:
            raise ValueError('Backup belongs to another trust zone')
        if source.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Backup failed integrity check')
        destination.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()

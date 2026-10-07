"""Message-level source of truth. No Discord, LLM or vector dependency."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


def utc(value=None):
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


FIELDS = ('message_id guild_id guild_name channel_id channel_name thread_id thread_name '
          'author_id author_name content created_at edited_at reply_to_message_id discord_url').split()


class DiscordArchive:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version > 1:
                raise ValueError('Archive schema is newer than this application')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS discord_messages (
                    message_id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, guild_name TEXT,
                    channel_id TEXT NOT NULL, channel_name TEXT, thread_id TEXT, thread_name TEXT,
                    author_id TEXT, author_name TEXT, content TEXT, created_at TEXT NOT NULL,
                    edited_at TEXT, reply_to_message_id TEXT, discord_url TEXT,
                    is_deleted INTEGER NOT NULL DEFAULT 0, archived_at TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS discord_attachments (
                    attachment_id TEXT PRIMARY KEY, message_id TEXT NOT NULL
                    REFERENCES discord_messages(message_id) ON DELETE CASCADE,
                    filename TEXT, content_type TEXT, size INTEGER, url TEXT);
                CREATE INDEX IF NOT EXISTS archive_channel_time
                    ON discord_messages(channel_id, thread_id, created_at, message_id);
                PRAGMA user_version=1;
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def upsert_message(self, message):
        data = {key: message.get(key) for key in FIELDS}
        for key in ('message_id', 'guild_id', 'channel_id'):
            if data[key] is None:
                raise ValueError(f'Missing {key}')
            data[key] = str(data[key])
        data['created_at'] = utc(data['created_at'])
        if data['edited_at']:
            data['edited_at'] = utc(data['edited_at'])
        data['content'] = data['content'] or ''
        location = data['thread_id'] or data['channel_id']
        data['discord_url'] = f"https://discord.com/channels/{data['guild_id']}/{location}/{data['message_id']}"
        with self.connect() as db:
            old = db.execute('SELECT * FROM discord_messages WHERE message_id=?',
                             (data['message_id'],)).fetchone()
            # A stale history scan must never resurrect a tombstone or overwrite a newer edit.
            if old and (old['is_deleted'] or (old['edited_at'] or old['created_at']) >
                        (data['edited_at'] or data['created_at'])):
                return False
            attachments = message.get('attachments', [])
            old_attachments = [dict(r) for r in db.execute(
                'SELECT attachment_id,filename,content_type,size,url FROM discord_attachments '
                'WHERE message_id=? ORDER BY attachment_id', (data['message_id'],))]
            normalized = [{k: a.get(k) for k in ('attachment_id', 'filename', 'content_type', 'size', 'url')}
                          for a in attachments]
            for a in normalized:
                a['attachment_id'] = str(a['attachment_id'])
            normalized.sort(key=lambda a: a['attachment_id'])
            if old and all(old[k] == data[k] for k in FIELDS) and old_attachments == normalized:
                return False
            columns = ','.join(FIELDS)
            updates = ','.join(f'{k}=excluded.{k}' for k in FIELDS if k != 'message_id')
            db.execute(f'INSERT INTO discord_messages ({columns},archived_at) '
                       f'VALUES ({",".join("?" for _ in FIELDS)},?) '
                       f'ON CONFLICT(message_id) DO UPDATE SET {updates}, '
                       'archived_at=excluded.archived_at, revision=discord_messages.revision+1',
                       [data[k] for k in FIELDS] + [utc()])
            db.execute('DELETE FROM discord_attachments WHERE message_id=?', (data['message_id'],))
            for a in normalized:
                db.execute('INSERT INTO discord_attachments VALUES (?,?,?,?,?,?,?)',
                           (a['attachment_id'], data['message_id'], a['filename'],
                            a['content_type'], a['size'], a['url']))
        return True

    def mark_deleted(self, message_id):
        with self.connect() as db:
            db.execute("UPDATE discord_messages SET content='', is_deleted=1, "
                       'revision=revision+1, archived_at=? WHERE message_id=? AND is_deleted=0',
                       (utc(), str(message_id)))
            db.execute('DELETE FROM discord_attachments WHERE message_id=?', (str(message_id),))

    def get_message(self, message_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM discord_messages WHERE message_id=?',
                             (str(message_id),)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result['attachments'] = [dict(r) for r in db.execute(
                'SELECT attachment_id,filename,content_type,size,url FROM discord_attachments '
                'WHERE message_id=?', (str(message_id),))]
            return result

    def get_context(self, message_id, before=10, after=10):
        hit = self.get_message(message_id)
        if not hit or hit['is_deleted']:
            return []
        with self.connect() as db:
            scope = 'channel_id=? AND thread_id IS ? AND is_deleted=0'
            args = (hit['channel_id'], hit['thread_id'])
            left = list(db.execute(f'SELECT * FROM discord_messages WHERE {scope} '
                'AND (created_at,message_id)<(?,?) ORDER BY created_at DESC,message_id DESC LIMIT ?',
                args + (hit['created_at'], hit['message_id'], max(0, min(100, before)))))
            right = list(db.execute(f'SELECT * FROM discord_messages WHERE {scope} '
                'AND (created_at,message_id)>(?,?) ORDER BY created_at,message_id LIMIT ?',
                args + (hit['created_at'], hit['message_id'], max(0, min(100, after)))))
        return [dict(r) for r in reversed(left)] + [hit] + [dict(r) for r in right]

    def list_channels(self, guild_id=None):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                'SELECT DISTINCT guild_id,guild_name,channel_id,channel_name,thread_id,thread_name '
                'FROM discord_messages WHERE is_deleted=0' +
                (' AND guild_id=?' if guild_id else '') + ' ORDER BY channel_id,thread_id',
                (str(guild_id),) if guild_id else ())]

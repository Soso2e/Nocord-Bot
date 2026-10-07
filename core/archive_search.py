"""Read boundary and rebuildable keyword/semantic projections of the archive."""
import json
import math
import re
from core.discord_archive import FIELDS, utc

MAX_RESULTS = 100


def bounded(value, default=20):
    return max(1, min(MAX_RESULTS, int(value if value is not None else default)))


def evidence(row):
    return {key: row.get(key) for key in FIELDS} | {'attachments': row.get('attachments', [])}


class ArchiveSearch:
    def __init__(self, archive, policy, embed=None, model='bge-m3'):
        self.archive, self.policy, self.embed, self.model = archive, policy, embed, model
        with archive.connect() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='archive_fts'").fetchone()
            db.executescript('''
                CREATE VIRTUAL TABLE IF NOT EXISTS archive_fts USING fts5(content);
                CREATE VIRTUAL TABLE IF NOT EXISTS archive_trigrams USING fts5(content,tokenize='trigram');
                CREATE TABLE IF NOT EXISTS archive_vectors (
                    message_id TEXT PRIMARY KEY REFERENCES discord_messages(message_id) ON DELETE CASCADE,
                    revision INTEGER NOT NULL, model TEXT NOT NULL, embedding TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS archive_insert AFTER INSERT ON discord_messages
                WHEN NEW.is_deleted=0 BEGIN
                    INSERT INTO archive_fts(rowid,content) VALUES (NEW.rowid,NEW.content);
                    INSERT INTO archive_trigrams(rowid,content) VALUES (NEW.rowid,NEW.content);
                END;
                CREATE TRIGGER IF NOT EXISTS archive_update AFTER UPDATE ON discord_messages BEGIN
                    DELETE FROM archive_fts WHERE rowid=OLD.rowid;
                    DELETE FROM archive_trigrams WHERE rowid=OLD.rowid;
                    INSERT INTO archive_fts(rowid,content) SELECT NEW.rowid,NEW.content WHERE NEW.is_deleted=0;
                    INSERT INTO archive_trigrams(rowid,content) SELECT NEW.rowid,NEW.content WHERE NEW.is_deleted=0;
                    DELETE FROM archive_vectors WHERE message_id=OLD.message_id;
                END;
                CREATE TRIGGER IF NOT EXISTS archive_delete AFTER DELETE ON discord_messages BEGIN
                    DELETE FROM archive_fts WHERE rowid=OLD.rowid;
                    DELETE FROM archive_trigrams WHERE rowid=OLD.rowid;
                END;
            ''')
            if not exists:
                self._rebuild_fts(db)

    def _rebuild_fts(self, db):
        for table in ('archive_fts', 'archive_trigrams'):
            db.execute(f'DELETE FROM {table}')
            db.execute(f'INSERT INTO {table}(rowid,content) SELECT rowid,content '
                       'FROM discord_messages WHERE is_deleted=0')

    def rebuild_fts(self):
        with self.archive.connect() as db:
            self._rebuild_fts(db)

    def scope(self, filters):
        clause, args = self.policy.sql_scope()
        parts = [clause, 'm.is_deleted=0']
        for name in ('guild_id', 'channel_id', 'thread_id', 'author_id'):
            if filters.get(name) is not None:
                parts.append(f'm.{name}=?')
                args.append(str(filters[name]))
        for name, op in (('after', '>'), ('before', '<')):
            if filters.get(name):
                parts.append(f'm.created_at{op}?')
                args.append(utc(filters[name]))
        return ' AND '.join(parts), args

    def search(self, query, limit=20, **filters):
        if not query.strip():
            return []
        if len(query) > 2000:
            raise ValueError('Query exceeds 2000 characters')
        limit = bounded(limit)
        scope, args = self.scope(filters)
        tokens = re.findall(r'\w+', query, re.UNICODE)[:32]
        if not tokens:
            return []
        fts = ' OR '.join('"' + token.replace('"', '""') + '"' for token in tokens)
        ranks = {}
        rows = {}
        with self.archive.connect() as db:
            hits = list(db.execute(
                'SELECT m.*,bm25(archive_fts) AS rank FROM archive_fts '
                'JOIN discord_messages m ON m.rowid=archive_fts.rowid '
                f'WHERE archive_fts MATCH ? AND {scope} ORDER BY rank,m.message_id LIMIT ?',
                [fts] + args + [limit * 4]))
            if len(query.strip()) >= 3:
                hits += list(db.execute(
                    'SELECT m.*,bm25(archive_trigrams) AS rank FROM archive_trigrams '
                    'JOIN discord_messages m ON m.rowid=archive_trigrams.rowid '
                    f'WHERE archive_trigrams MATCH ? AND {scope} ORDER BY rank,m.message_id LIMIT ?',
                    ['"' + query.strip().replace('"', '""') + '"'] + args + [limit * 4]))
            seen = set()
            for row in sorted(hits, key=lambda r: (r['rank'], r['message_id'])):
                mid = row['message_id']
                if mid in seen:
                    continue
                seen.add(mid)
                rows[mid] = dict(row)
                ranks[mid] = {'fts': len(seen)}
            if self.embed:
                try:
                    qvector = self.embed([query])[0]
                    vectors = db.execute(
                        'SELECT m.*,v.embedding FROM archive_vectors v JOIN discord_messages m '
                        'ON m.message_id=v.message_id AND m.revision=v.revision '
                        f'WHERE v.model=? AND {scope}', [self.model] + args)
                    scored = [(cosine(qvector, json.loads(row['embedding'])), dict(row)) for row in vectors]
                    scored.sort(key=lambda item: (-item[0], item[1]['message_id']))
                    for pos, (similarity, row) in enumerate(scored[:limit * 4], 1):
                        if similarity < 0.3:
                            continue
                        mid = row['message_id']
                        rows[mid] = row
                        ranks.setdefault(mid, {})['vector'] = pos
                except Exception:
                    pass  # The archive/FTS path survives embedding outages.
        result = []
        for mid, matches in ranks.items():
            score = sum(1 / (60 + rank) for rank in matches.values())
            exact = query.casefold() in (rows[mid]['content'] or '').casefold()
            if exact:
                score += 1 / 61
            result.append(evidence(rows[mid]) | {'score': round(score, 8),
                           'match_type': list(matches) + (['exact'] if exact else [])})
        result.sort(key=lambda row: (-row['score'], row['message_id']))
        # Re-read after vector calls so concurrent edits/deletes never return stale index text.
        final = []
        for row in result:
            current = self.get_message(row['message_id'])
            if current and current['content'] == row['content']:
                final.append(current | {'score': row['score'], 'match_type': row['match_type']})
            if len(final) == limit:
                break
        return final

    def get_message(self, message_id):
        row = self.archive.get_message(message_id)
        if row and not row['is_deleted'] and self.policy.allows_message(row):
            return evidence(row)
        return None

    def get_message_context(self, message_id, before=10, after=10):
        if not self.get_message(message_id):
            return []
        return [evidence(r) for r in self.archive.get_context(message_id, before, after)
                if self.policy.allows_message(r)]

    def get_messages(self, channel_id, after=None, before=None, limit=20, **filters):
        scope, args = self.scope(dict(filters, after=after, before=before))
        with self.archive.connect() as db:
            rows = db.execute(f'SELECT m.* FROM discord_messages m WHERE {scope} '
                'AND (m.channel_id=? AND m.thread_id IS NULL OR m.thread_id=?) '
                'ORDER BY m.created_at,m.message_id LIMIT ?',
                args + [str(channel_id), str(channel_id), bounded(limit)])
            return [evidence(dict(r)) for r in rows]

    def get_thread(self, thread_id, after=None, limit=100):
        return self.get_messages(thread_id, after=after, limit=limit, thread_id=str(thread_id))

    def list_channels(self, guild_id=None, limit=100):
        scope, args = self.scope({'guild_id': guild_id})
        with self.archive.connect() as db:
            return [dict(r) for r in db.execute(
                'SELECT DISTINCT m.guild_id,m.guild_name,m.channel_id,m.channel_name,m.thread_id,m.thread_name '
                f'FROM discord_messages m WHERE {scope} ORDER BY m.channel_id,m.thread_id LIMIT ?',
                args + [bounded(limit)])]

    def index_pending(self, limit=100, rebuild=False):
        if not self.embed:
            return 0
        scope, args = self.scope({})
        with self.archive.connect() as db:
            if rebuild:
                db.execute('DELETE FROM archive_vectors')
            rows = [dict(r) for r in db.execute(
                'SELECT m.* FROM discord_messages m LEFT JOIN archive_vectors v '
                'ON v.message_id=m.message_id AND v.revision=m.revision AND v.model=? '
                f'WHERE {scope} AND v.message_id IS NULL ORDER BY m.created_at,m.message_id LIMIT ?',
                [self.model] + args + [bounded(limit)])]
        if not rows:
            return 0
        vectors = self.embed([r['content'] for r in rows])
        if len(vectors) != len(rows):
            raise ValueError('Embedding result count mismatch')
        count = 0
        with self.archive.connect() as db:
            for row, vector in zip(rows, vectors):
                count += db.execute('INSERT OR REPLACE INTO archive_vectors '
                    'SELECT message_id,revision,?,? FROM discord_messages '
                    'WHERE message_id=? AND revision=? AND is_deleted=0',
                    (self.model, json.dumps(vector), row['message_id'], row['revision'])).rowcount
        return count


def cosine(a, b):
    if len(a) != len(b):
        return 0
    denominator = math.sqrt(sum(x*x for x in a) * sum(x*x for x in b))
    return sum(x*y for x, y in zip(a, b)) / denominator if denominator else 0


def create_search(archive, policy):
    cfg = policy.config.get('vector', {})
    model = cfg.get('model', 'bge-m3')
    embed = None
    if cfg.get('enabled', False):
        from core.rag_manager import _embed, _load_llm_config
        embed = lambda texts: _embed(texts, model, _load_llm_config()['base_url'])
    return ArchiveSearch(archive, policy, embed=embed, model=model)

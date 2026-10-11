"""Per-channel checkpoints and bounded repair independent of RAG availability."""
import asyncio
from datetime import datetime, timedelta, timezone
import logging
from core.discord_archive import utc
from core.discord_archive_sync import private_thread

log = logging.getLogger(__name__)


class ArchiveReconciler:
    def __init__(self, ingestor):
        self.ingestor = ingestor
        self.archive = ingestor.archive
        self.policy = ingestor.policy
        self.lock = asyncio.Lock()
        with self.archive.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS archive_checkpoints (
                    channel_id TEXT PRIMARY KEY, completed_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS archive_state (key TEXT PRIMARY KEY,value TEXT);
            ''')

    def state(self, key, value):
        with self.archive.connect() as db:
            db.execute('INSERT OR REPLACE INTO archive_state VALUES (?,?)', (key, value))

    async def channel(self, channel):
        parent_id = getattr(channel, 'parent_id', None)
        if not self.policy.allows(channel.guild.id, parent_id or channel.id,
                                  channel.id if parent_id else None, private_thread(channel)):
            return
        started = datetime.now(timezone.utc)
        with self.archive.connect() as db:
            row = db.execute('SELECT completed_at FROM archive_checkpoints WHERE channel_id=?',
                             (str(channel.id),)).fetchone()
        backfill = self.policy.config.get('backfill', {'mode': 'none'})
        mode = backfill.get('mode', 'none')
        limit = None
        after = datetime.fromisoformat(row[0]) - timedelta(seconds=1) if row else None
        oldest_first = True
        if not row:
            if mode == 'none':
                after = started
            elif mode == 'days':
                after = started - timedelta(days=max(0, int(backfill.get('days', 30))))
            elif mode == 'latest':
                limit = max(1, min(10000, int(backfill.get('limit', 100))))
                oldest_first = False
            elif mode != 'full':
                raise ValueError('Invalid backfill mode')
        async for message in channel.history(limit=limit, after=after, before=started,
                                             oldest_first=oldest_first):
            # A failed write is a failed scan: do not use the swallowing realtime handler.
            await asyncio.to_thread(self.ingestor.save, message)
        repair_days = max(0, int(self.policy.config.get('repair_days', 7)))
        if repair_days:
            cutoff = utc(started - timedelta(days=repair_days))
            with self.archive.connect() as db:
                rows = [dict(r) for r in db.execute(
                    'SELECT message_id FROM discord_messages WHERE is_deleted=0 '
                    'AND COALESCE(thread_id,channel_id)=? AND created_at>=? AND created_at<?',
                    (str(channel.id), cutoff, utc(started)))]
            # Explicit fetch: absence in a limited/incomplete scan is never proof of deletion.
            for item in rows:
                try:
                    message = await channel.fetch_message(int(item['message_id']))
                except Exception as exc:
                    if getattr(exc, 'status', None) == 404 and getattr(exc, 'code', None) == 10008:
                        await asyncio.to_thread(self.archive.mark_deleted, item['message_id'])
                    else:
                        raise
                else:
                    await asyncio.to_thread(self.ingestor.save, message)
        current = self.ingestor.current_policy()
        if not current.allows(channel.guild.id, parent_id or channel.id,
                               channel.id if parent_id else None, private_thread(channel)):
            return
        with self.archive.connect() as db:
            db.execute('INSERT OR REPLACE INTO archive_checkpoints VALUES (?,?)',
                       (str(channel.id), utc(started)))

    async def guild(self, guild):
        errors = []
        processed = 0
        channels = list(guild.text_channels) + list(getattr(guild, 'forums', []))
        visited = set()
        for channel in channels:
            if not self.policy.allows(guild.id, channel.id):
                continue
            targets = []
            if hasattr(channel, 'history'):
                targets.append(channel)
            threads = {str(t.id): t for t in getattr(channel, 'threads', [])}
            try:
                async for thread in channel.archived_threads(limit=None):
                    threads[str(thread.id)] = thread
            except Exception as exc:
                errors.append(f'{channel.id}:threads:{type(exc).__name__}')
            targets.extend(threads.values())
            for target in targets:
                try:
                    await self.channel(target)
                    visited.add(str(target.id))
                    processed += 1
                except Exception as exc:
                    errors.append(f'{target.id}:{type(exc).__name__}')
                    log.warning('Archive reconcile channel %s failed (%s)', target.id, type(exc).__name__)
        # Explicit thread consent may exist without consent for its parent channel.
        allowed_ids = self.policy.allow_guilds.get(str(guild.id), {}).get('channels', [])
        known_parents = {str(c.id) for c in channels}
        for cid in allowed_ids:
            if str(cid) in visited or str(cid) in known_parents:
                continue
            try:
                target = getattr(guild, 'get_channel_or_thread', lambda _: None)(int(cid))
                if target is None:
                    target = await guild.fetch_channel(int(cid))
                await self.channel(target)
                processed += 1
            except Exception as exc:
                errors.append(f'{cid}:{type(exc).__name__}')
        return {'channels_processed': processed, 'errors': errors}

    async def run(self, guilds):
        if self.lock.locked():
            return {'channels_processed': 0, 'errors': []}
        async with self.lock:
            result = {'channels_processed': 0, 'errors': []}
            for guild in guilds:
                data = await self.guild(guild)
                result['channels_processed'] += data['channels_processed']
                result['errors'].extend(data['errors'])
            self.state('last_reconcile_at', utc())
            self.state('last_reconcile_error', ';'.join(result['errors']) or None)
            return result


_reconciler = None


async def reconcile(bot):
    global _reconciler
    from core.discord_archive_sync import get_ingestor
    try:
        ingestor = get_ingestor()
        if not ingestor:
            return
        if _reconciler is None or _reconciler.ingestor is not ingestor:
            _reconciler = ArchiveReconciler(ingestor)
        return await _reconciler.run(bot.guilds)
    except Exception as exc:
        log.warning('Archive reconciliation failed (%s)', type(exc).__name__)

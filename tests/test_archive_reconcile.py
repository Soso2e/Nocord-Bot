from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from tests_support import ArchiveTestCase
from test_archive_events import discord_message
from core.discord_archive_sync import ArchiveIngestor
from core.archive_reconcile import ArchiveReconciler


class Channel:
    def __init__(self, cid, messages=(), parent=None, failure=False):
        self.id, self.name, self.parent_id = cid, 'channel', parent
        self.guild = NS(id=10, name='Guild')
        self.threads = []
        self.messages = list(messages)
        self.failure = failure
        self.calls = []

    async def history(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure:
            raise OSError('private secret')
        messages = sorted(self.messages, key=lambda m: m.created_at,
                          reverse=not kwargs['oldest_first'])
        eligible = [m for m in messages if (not kwargs['after'] or m.created_at > kwargs['after'])
                    and m.created_at < kwargs['before']]
        for m in eligible[:kwargs['limit']]:
            yield m

    async def archived_threads(self, limit=None):
        for t in self.threads:
            yield t

    async def fetch_message(self, mid):
        return next(m for m in self.messages if m.id == mid)


class ReconcileTests(ArchiveTestCase):
    def reconciler(self, mode='full'):
        self.policy.config.update(backfill={'mode': mode}, repair_days=0)
        return ArchiveReconciler(ArchiveIngestor(self.archive, self.policy))

    async def test_restart_idempotency_thread_and_failure_checkpoints(self):
        channel = Channel(20, [discord_message()])
        thread = Channel(40, [discord_message(2, 40, 20)], parent=20)
        channel.threads = [thread]
        denied = Channel(21, [discord_message(3, 21)])
        r = self.reconciler()
        result = await r.run([NS(id=10, text_channels=[channel, denied])])
        self.assertEqual(result['errors'], [])
        self.assertEqual(self.archive.get_message('2')['thread_id'], '40')
        self.assertEqual(denied.calls, [])
        fresh = discord_message(4)
        fresh.created_at = datetime(2026, 10, 2, tzinfo=timezone.utc)
        with self.archive.connect() as db:
            db.execute("UPDATE archive_checkpoints SET completed_at='2026-10-01T00:00:00+00:00' WHERE channel_id='20'")
        channel.messages.append(fresh)
        thread.failure = True
        result = await r.run([NS(id=10, text_channels=[channel])])
        self.assertEqual(len(result['errors']), 1)
        self.assertIsNotNone(self.archive.get_message('4'))
        with self.archive.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM discord_messages').fetchone()[0], 3)
        self.assertEqual(self.archive.get_message('1')['revision'], 1)

    async def test_initial_none_and_latest(self):
        channel = Channel(20, [discord_message()])
        await self.reconciler('none').channel(channel)
        self.assertIsNone(self.archive.get_message('1'))
        with self.archive.connect() as db:
            db.execute('DELETE FROM archive_checkpoints')
        r = self.reconciler('latest')
        self.policy.config['backfill']['limit'] = 1
        await r.channel(channel)
        self.assertIsNotNone(self.archive.get_message('1'))

    async def test_failed_channel_does_not_advance_or_stop_other_channels(self):
        self.policy.allow_guilds['10']['channels'].append('22')
        r = self.reconciler()
        bad = Channel(20, failure=True)
        good = Channel(22, [discord_message(2, 22)])
        result = await r.run([NS(id=10, text_channels=[bad, good])])
        self.assertEqual(len(result['errors']), 1)
        self.assertIsNotNone(self.archive.get_message('2'))
        with self.archive.connect() as db:
            checkpoint_ids = [row[0] for row in db.execute('SELECT channel_id FROM archive_checkpoints')]
        self.assertEqual(checkpoint_ids, ['22'])

    async def test_explicit_private_thread_without_parent_consent(self):
        self.policy.allow_guilds['10']['channels'] = ['40']
        r = self.reconciler()
        message = discord_message(2, 40, 20)
        message.channel.is_private = lambda: True
        thread = Channel(40, [message], parent=20)
        thread.is_private = lambda: True
        guild = NS(id=10, text_channels=[Channel(20)], get_channel_or_thread=lambda _: thread)
        result = await r.run([guild])
        self.assertEqual(result['errors'], [])
        self.assertTrue(self.archive.get_message('2')['is_private_thread'])

    async def test_explicit_not_found_repairs_delete_permission_error_does_not(self):
        channel = Channel(20, [discord_message()])
        r = self.reconciler()
        await r.channel(channel)
        self.policy.config['repair_days'] = 1000
        error = type('NotFound', (Exception,), {'status': 404, 'code': 10008})()
        channel.fetch_message = AsyncMock(side_effect=error)
        await r.channel(channel)
        self.assertTrue(self.archive.get_message('1')['is_deleted'])
        self.archive.upsert_message(dict(message_id='2', guild_id='10', channel_id='20',
            content='keep', created_at='2026-10-01T00:00:00Z'))
        channel.fetch_message = AsyncMock(side_effect=PermissionError())
        with self.assertRaises(PermissionError):
            await r.channel(channel)
        self.assertFalse(self.archive.get_message('2')['is_deleted'])

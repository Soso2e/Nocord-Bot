from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from tests_support import ArchiveTestCase
from core.discord_archive_sync import ArchiveIngestor


def discord_message(mid=1, channel=20, parent=None, bot=False):
    return NS(id=mid, guild=NS(id=10, name='Guild'),
        channel=NS(id=channel, name='channel', parent_id=parent, parent=NS(name='parent')),
        author=NS(id=30, name='author', display_name='Author', bot=bot),
        content='WEVORA', created_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        edited_at=None, reference=None, attachments=[])


class EventTests(ArchiveTestCase):
    async def test_create_without_mention_bot_exclusion_and_deny(self):
        ingest = ArchiveIngestor(self.archive, self.policy)
        await ingest.on_create(discord_message())
        await ingest.on_create(discord_message(2, channel=21))
        await ingest.on_create(discord_message(3, bot=True))
        await ingest.on_create(discord_message(4, channel=40, parent=20))
        self.assertIsNotNone(self.archive.get_message('1'))
        self.assertIsNone(self.archive.get_message('2'))
        self.assertIsNone(self.archive.get_message('3'))
        self.assertEqual(self.archive.get_message('4')['thread_id'], '40')

    async def test_uncached_update_bulk_delete_and_scrub(self):
        ingest = ArchiveIngestor(self.archive, self.policy)
        msg = discord_message()
        await ingest.on_create(msg)
        msg.content = 'changed'
        msg.edited_at = datetime(2026, 10, 2, tzinfo=timezone.utc)
        channel = NS(id=20, fetch_message=AsyncMock(return_value=msg))
        bot = NS(get_channel=lambda cid: channel)
        await ingest.on_update(NS(guild_id=10, channel_id=20, message_id=1), bot)
        self.assertEqual(self.archive.get_message('1')['content'], 'changed')
        await ingest.on_delete(NS(guild_id=10, channel_id=20, message_ids={1, 2}), bot)
        self.assertTrue(self.archive.get_message('2')['is_deleted'])
        self.assertEqual(self.archive.get_message('1')['content'], '')
        await ingest.on_create(discord_message(2))
        self.assertTrue(self.archive.get_message('2')['is_deleted'])

    async def test_failure_isolated(self):
        ingest = ArchiveIngestor(self.archive, self.policy)
        with patch.object(self.archive, 'upsert_message', side_effect=OSError('secret body')):
            with self.assertLogs('core.discord_archive_sync') as logs:
                await ingest.on_create(discord_message())
            self.assertNotIn('secret body', str(logs.output))

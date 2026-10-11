import tempfile
import unittest
from pathlib import Path
from core.discord_archive import DiscordArchive


def message(mid='1', **kwargs):
    return dict(message_id=mid, guild_id='10', guild_name='Guild', channel_id='20',
                channel_name='Channel', author_id='30', author_name='User', content='WEVORA',
                created_at='2026-10-01T00:00:00Z', reply_to_message_id='9', **kwargs)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = DiscordArchive(Path(self.tmp.name) / 'archive.sqlite')

    def test_upsert_provenance_and_reopen(self):
        dto = message(thread_id='40', thread_name='Thread', attachments=[
            dict(attachment_id='50', filename='x.pdf', size=4, url='https://example.org/x')])
        self.assertTrue(self.archive.upsert_message(dto))
        self.assertFalse(self.archive.upsert_message(dto))
        row = DiscordArchive(self.archive.path).get_message('1')
        self.assertEqual(row['revision'], 1)
        self.assertEqual(row['reply_to_message_id'], '9')
        self.assertEqual(row['discord_url'], 'https://discord.com/channels/10/40/1')
        self.assertEqual(row['attachments'][0]['filename'], 'x.pdf')

    def test_delete_scrubs_and_stale_history_cannot_resurrect(self):
        self.archive.upsert_message(message())
        self.archive.mark_deleted('1')
        self.assertFalse(self.archive.upsert_message(message()))
        self.assertEqual(self.archive.get_message('1')['content'], '')
        self.assertEqual(self.archive.get_context('1'), [])

    def test_edit_and_utc(self):
        self.archive.upsert_message(message())
        edited = message(edited_at='2026-10-02T09:00:00+09:00')
        edited['content'] = 'changed'
        self.archive.upsert_message(edited)
        self.archive.upsert_message(message())
        self.assertEqual(self.archive.get_message('1')['content'], 'changed')
        self.assertEqual(self.archive.get_message('1')['edited_at'], '2026-10-02T00:00:00+00:00')

    def test_context_isolated_by_thread(self):
        self.archive.upsert_message(message())
        self.archive.upsert_message(message('2'))
        self.archive.upsert_message(message('3', thread_id='40'))
        self.assertEqual([r['message_id'] for r in self.archive.get_context('2')], ['1', '2'])
        self.assertEqual(len(self.archive.list_channels()), 2)

from unittest.mock import Mock
from tests_support import ArchiveTestCase
from test_discord_archive import message
from core.archive_search import ArchiveSearch


class SearchTests(ArchiveTestCase):
    async def test_attachment_only_does_not_block_vector_batch(self):
        empty = message('1'); empty['content'] = ''
        self.archive.upsert_message(empty)
        self.archive.upsert_message(message('2'))
        embed = Mock(return_value=[[1., 0.]])
        search = ArchiveSearch(self.archive, self.policy, embed=embed)
        self.assertEqual(search.index_pending(), 1)
        embed.assert_called_once_with(['WEVORA'])

    async def test_attachment_provenance_in_messages_and_context(self):
        self.archive.upsert_message(message(attachments=[dict(attachment_id='50',
            filename='guide.pdf', content_type='application/pdf', size=4, url='https://example.org/x')]))
        search = ArchiveSearch(self.archive, self.policy)
        self.assertEqual(search.get_messages('20')[0]['attachments'][0]['attachment_id'], '50')
        self.assertEqual(search.get_message_context('1')[0]['attachments'][0]['filename'], 'guide.pdf')

    async def test_fts_filters_provenance_and_rebuild(self):
        self.archive.upsert_message(message())
        denied = message('2'); denied['channel_id'] = '21'
        self.archive.upsert_message(denied)
        search = ArchiveSearch(self.archive, self.policy)
        self.assertEqual([r['message_id'] for r in search.search('WEVORA')], ['1'])
        self.assertEqual(search.search('WEVORA', author_id='other'), [])
        self.assertEqual(search.search('WEVORA', after='2026-10-02T00:00:00Z'), [])
        self.assertIn('/10/20/1', search.search('WEVORA')[0]['discord_url'])
        self.assertEqual(search.get_message_context('2'), [])
        self.assertEqual(search.get_messages('21'), [])
        search.rebuild_fts()
        self.assertEqual(len(search.search('WEVORA')), 1)
        self.archive.mark_deleted('1')
        self.assertEqual(search.search('WEVORA'), [])

    async def test_japanese_url_code_and_literal_query(self):
        dto = message(); dto['content'] = '飛行操作の検証方法 https://host.test/docs AssetChecker PR #20 error_code'
        self.archive.upsert_message(dto)
        search = ArchiveSearch(self.archive, self.policy)
        for term in ('検証方法', 'AssetChecker', '#20', 'error_code', 'https://host.test/docs'):
            self.assertEqual(len(search.search(term)), 1, term)
        self.assertEqual(search.search('" OR *'), [])

    async def test_hybrid_fusion_outage_edits_and_rebuild(self):
        self.archive.upsert_message(message())
        dto = message('2'); dto['content'] = 'semantic equivalent'
        self.archive.upsert_message(dto)
        embed = Mock(return_value=[[1., 0.], [0.9, 0.1]])
        search = ArchiveSearch(self.archive, self.policy, embed=embed)
        self.assertEqual(search.index_pending(), 2)
        embed.return_value = [[1., 0.]]
        hits = search.search('WEVORA')
        self.assertEqual(hits[0]['message_id'], '1')
        self.assertEqual(hits[0]['match_type'], ['fts', 'vector', 'exact'])
        self.assertEqual(hits[1]['match_type'], ['vector'])
        embed.side_effect = OSError('offline')
        self.assertEqual(len(search.search('WEVORA')), 1)
        self.archive.mark_deleted('1')
        embed.side_effect = None
        self.assertEqual([r['message_id'] for r in search.search('WEVORA')], ['2'])
        with self.archive.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM archive_vectors').fetchone()[0], 1)

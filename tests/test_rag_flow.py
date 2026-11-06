import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from core import rag_manager, chat_controller, context_builder, knowledge_search
from interfaces import discord_bot


class RagStorageTests(unittest.TestCase):
    def test_provider_defaults_and_explicit_override(self):
        self.assertEqual(rag_manager._embedding_model({}, {'provider': 'openai'}), 'text-embedding-bge-m3')
        self.assertEqual(rag_manager._embedding_model({}, {'provider': 'ollama'}), 'bge-m3')
        self.assertEqual(rag_manager._embedding_model({'embedding_model': 'custom'}, {}), 'custom')

    def test_incomplete_embeddings_never_save_partial_document(self):
        with patch.object(rag_manager, '_load_rag_config', return_value={'vector_backend': 'json'}), \
             patch.object(rag_manager, '_load_llm_config', return_value={}), \
             patch.object(rag_manager, '_embed', return_value=[]), \
             patch.object(rag_manager, '_json_ingest') as save:
            with self.assertRaises(ValueError):
                rag_manager.ingest_text('test', 'content', 'source')
            save.assert_not_called()

    def test_success_enables_search_and_empty_input_does_not(self):
        with patch.object(rag_manager, 'ingest_text', return_value=2), \
             patch.object(chat_controller, 'rag_enable') as enable:
            self.assertEqual(chat_controller.rag_ingest_text('test', 'text'), 2)
            enable.assert_called_once_with('test')
        with patch.object(rag_manager, 'ingest_text', return_value=0), \
             patch.object(chat_controller, 'rag_enable') as enable:
            with self.assertRaises(ValueError):
                chat_controller.rag_ingest_text('test', '')
            enable.assert_not_called()

    def test_status_storage_failure_is_not_reported_as_empty(self):
        with patch.object(rag_manager, '_get_backend', side_effect=OSError):
            stats = rag_manager.collection_stats('test')
        self.assertIsNone(stats['document_count'])
        self.assertIn('error', stats)

    def test_search_failure_reaches_answer_context(self):
        cfg = {'rag': {'enabled': True}}
        with patch.object(context_builder, '_load_db_config', return_value=cfg), \
             patch.object(context_builder, 'get_history', return_value=[]), \
             patch.object(context_builder, 'find_relevant_memories', return_value=[]), \
             patch('core.gateway_config.load_policy', return_value=NS(enabled=False)), \
             patch.object(knowledge_search, 'load_policy', return_value=NS(enabled=False)), \
             patch.object(rag_manager, 'search', side_effect=RuntimeError('unavailable')):
            messages = context_builder.build_messages('test', 'session', 'question')
        self.assertTrue(any('検索に失敗' in m['content'] for m in messages[1:]))


class ScanFlowTests(unittest.IsolatedAsyncioTestCase):
    def interaction(self, messages):
        async def history(**kwargs):
            for message in messages:
                yield message
        channel = NS(name='docs', mention='#docs', history=history)
        return NS(guild=NS(id=10), channel=channel,
                  response=NS(defer=AsyncMock()),
                  followup=NS(send=AsyncMock(return_value=NS(edit=AsyncMock()))))

    async def test_preflight_failure_stops_before_history(self):
        interaction = self.interaction([])
        interaction.channel.history = unittest.mock.Mock(side_effect=AssertionError('history should not be read'))
        with patch.object(discord_bot, '_require_guild', return_value=True), \
             patch.object(discord_bot, '_has_manage_guild', return_value=True), \
             patch.object(discord_bot, '_db', return_value='test'), \
             patch.object(rag_manager, 'check_connection', side_effect=RuntimeError('unavailable')):
            await discord_bot.rag_scan_channel(interaction)
        interaction.channel.history.assert_not_called()
        self.assertIn('保存は行われていません', interaction.followup.send.call_args.args[0])

    async def test_same_filename_attachments_are_saved_separately(self):
        messages = [NS(content='', attachments=[NS(id=i, filename='guide.pdf', read=AsyncMock(return_value=b'pdf'))])
                    for i in (1, 2)]
        interaction = self.interaction(messages)
        with patch.object(discord_bot, '_require_guild', return_value=True), \
             patch.object(discord_bot, '_has_manage_guild', return_value=True), \
             patch.object(discord_bot, '_db', return_value='test'), \
             patch.object(rag_manager, 'check_connection'), \
             patch.object(chat_controller, 'rag_list_sources', return_value=[]), \
             patch.object(discord_bot, 'read_bytes', return_value='text'), \
             patch.object(chat_controller, 'rag_ingest_text', return_value=1) as ingest, \
             patch.object(chat_controller, 'rag_get_status', return_value={'enabled': True, 'document_count': 2}):
            await discord_bot.rag_scan_channel(interaction)
        self.assertEqual([c.args[2] for c in ingest.call_args_list],
                         ['discord-pdf:1:guide.pdf', 'discord-pdf:2:guide.pdf'])
        progress = interaction.followup.send.return_value
        self.assertIn('今回保存: 2 チャンク', progress.edit.call_args.kwargs['content'])

    async def test_empty_pdf_is_failure_and_not_success(self):
        interaction = self.interaction([NS(content='', attachments=[
            NS(id=1, filename='image.pdf', read=AsyncMock(return_value=b'pdf'))])])
        with patch.object(discord_bot, '_require_guild', return_value=True), \
             patch.object(discord_bot, '_has_manage_guild', return_value=True), \
             patch.object(discord_bot, '_db', return_value='test'), \
             patch.object(rag_manager, 'check_connection'), \
             patch.object(chat_controller, 'rag_list_sources', return_value=[]), \
             patch.object(discord_bot, 'read_bytes', return_value=''), \
             patch.object(rag_manager, 'ingest_text', return_value=0), \
             patch.object(chat_controller, 'rag_enable') as enable, \
             patch.object(chat_controller, 'rag_get_status', return_value={'enabled': False, 'document_count': 0}):
            await discord_bot.rag_scan_channel(interaction)
        enable.assert_not_called()
        content = interaction.followup.send.return_value.edit.call_args.kwargs['content']
        self.assertIn('取り込み失敗', content)
        self.assertIn('今回保存: 0 チャンク', content)
        self.assertNotIn('追加済み', content)

    async def test_server_sync_advances_checkpoint_only_without_errors(self):
        for errors in (['failed PDF'], []):
            with self.subTest(errors=errors):
                interaction = self.interaction([])
                interaction.guild.name = 'guild'
                result = {'channels_processed': 1, 'total_chunks': 1, 'errors': errors}
                with patch.object(discord_bot, '_require_guild', return_value=True), \
                     patch.object(discord_bot, '_has_manage_guild', return_value=True), \
                     patch.object(discord_bot, '_db', return_value='test'), \
                     patch.object(discord_bot, 'get_guild_sync_state', return_value={}), \
                     patch.object(discord_bot, '_sync_guild_to_rag', AsyncMock(return_value=result)), \
                     patch.object(discord_bot, 'update_guild_sync_state') as checkpoint:
                    await discord_bot.rag_sync_server(interaction)
                self.assertEqual(checkpoint.call_count, 0 if errors else 1)


if __name__ == '__main__':
    unittest.main()

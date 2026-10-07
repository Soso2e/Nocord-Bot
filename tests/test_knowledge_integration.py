import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from tests_support import ArchiveTestCase
from test_discord_archive import message
from core import context_builder, chat_controller, knowledge_search, rag_manager
from core.archive_search import ArchiveSearch
from core.discord_archive_sync import ArchiveIngestor


class KnowledgeTests(ArchiveTestCase):
    async def test_archive_fts_with_rag_off_and_bound_guild_only(self):
        self.policy.config['qa_enabled'] = True
        self.archive.upsert_message(message())
        ingestor = ArchiveIngestor(self.archive, self.policy)
        with patch('core.knowledge_search.load_policy', return_value=self.policy), \
             patch('core.discord_archive_sync.get_ingestor', return_value=ingestor), \
             patch('core.db_registry.get_guild_db', return_value='general'):
            docs = knowledge_search.search_knowledge('general', 'WEVORA', rag_enabled=False,
                                                     archive_guild_id='10')
            self.assertIn('discord.com/channels', docs[0]['source'])
            self.assertEqual(knowledge_search.search_knowledge('other', 'WEVORA',
                rag_enabled=False, archive_guild_id='10'), [])
            self.assertEqual(knowledge_search.search_knowledge('general', 'WEVORA', rag_enabled=False), [])

    async def test_legacy_duplicates_removed_pdf_notion_preserved(self):
        docs = [{'source': 'discord:10:20', 'content': 'stale'},
                {'source': 'discord-pdf:50:guide.pdf', 'content': 'pdf'},
                {'source': 'notion:page', 'content': 'notion'}]
        with patch('core.knowledge_search.load_policy', return_value=self.policy), \
             patch('core.rag_manager.search', return_value=docs):
            found = knowledge_search.search_knowledge('general', 'query')
        self.assertEqual([d['content'] for d in found], ['pdf', 'notion'])

    async def test_blocks_are_reproducible_and_reflect_deletion(self):
        for mid, time in [('1', '00:00'), ('2', '00:20'), ('3', '01:00')]:
            row = message(mid); row['created_at'] = f'2026-10-01T{time}:00Z'
            self.archive.upsert_message(row)
        search = ArchiveSearch(self.archive, self.policy)
        blocks = knowledge_search.conversation_blocks(search.get_messages('20'))
        self.assertEqual([b['message_ids'] for b in blocks], [['1', '2'], ['3']])
        self.archive.mark_deleted('2')
        blocks = knowledge_search.conversation_blocks(search.get_messages('20'))
        self.assertEqual([b['message_ids'] for b in blocks], [['1'], ['3']])

    async def test_existing_answer_and_memory_flow(self):
        cfg = {'system_prompt': 'Q&A', 'memory_policy': {}, 'rag': {'enabled': False}}
        with patch('core.chat_controller.init_db'), patch('core.chat_controller._load_db_config', return_value=cfg), \
             patch('core.chat_controller.save_message') as save, \
             patch('core.chat_controller._llm.chat', AsyncMock(return_value='answer')) as llm, \
             patch('core.context_builder._load_db_config', return_value=cfg), \
             patch('core.context_builder.get_history', return_value=[]), \
             patch('core.context_builder.find_relevant_memories', return_value=[]):
            reply = await chat_controller.process('hello', 'session')
        self.assertEqual(reply, 'answer')
        self.assertEqual(save.call_count, 2)
        self.assertEqual(llm.call_args.args[0][-1], {'role': 'user', 'content': 'hello'})

    async def test_json_rag_document_ingest_search_untouched(self):
        from pathlib import Path
        with patch.object(rag_manager, 'DB_BASE', Path(self.tmp.name)), \
             patch.object(rag_manager, '_load_rag_config', return_value={'vector_backend': 'json'}), \
             patch.object(rag_manager, '_load_llm_config', return_value={'base_url': 'http://local'}), \
             patch.object(rag_manager, '_embed', return_value=[[1., 0.]]):
            self.assertEqual(rag_manager.ingest_text('general', 'PDF knowledge', 'guide.pdf'), 1)
            self.assertEqual(rag_manager.search('general', 'question')[0]['source'], 'guide.pdf')

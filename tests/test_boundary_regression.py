import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace as NS
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from starlette.testclient import TestClient
from tests_support import ArchiveTestCase
from test_discord_archive import message
from test_archive_events import discord_message
from core.discord_archive_sync import ArchiveIngestor
from core.archive_search import ArchiveSearch
from core.gateway_config import ArchivePolicy


class BoundaryTests(ArchiveTestCase):
    async def test_private_threads_need_explicit_consent_on_save_and_read(self):
        ingest = ArchiveIngestor(self.archive, self.policy)
        msg = discord_message(1, 40, 20)
        msg.channel.is_private = lambda: True
        await ingest.on_create(msg)
        self.assertIsNone(self.archive.get_message('1'))
        self.policy.allow_guilds['10']['channels'].append('40')
        await ingest.on_create(msg)
        search = ArchiveSearch(self.archive, self.policy)
        self.assertEqual(len(search.search('WEVORA')), 1)
        self.policy.allow_guilds['10']['channels'].remove('40')
        self.assertEqual(search.search('WEVORA'), [])
        self.assertEqual(search.get_message_context('1'), [])
        self.assertEqual(search.get_thread('40'), [])

    async def test_malformed_consent_fails_closed(self):
        with self.assertRaises(ValueError):
            ArchivePolicy({'enabled': True, 'allow_guilds': {'10': {'channels': '20'}}})
        with self.assertRaises(ValueError):
            ArchivePolicy({'trust_zone': '../school'})

    async def test_stdio_real_process_search_then_context_without_discord_token(self):
        self.archive.upsert_message(message())
        config = Path(self.tmp.name) / 'config.json'
        config.write_text(json.dumps({'discord_archive': self.policy.config}), encoding='utf-8')
        params = StdioServerParameters(command=sys.executable,
            args=['-m', 'interfaces.mcp_server'], cwd=str(Path(__file__).resolve().parent.parent),
            env={'NOCORD_CONFIG': str(config), 'NOCORD_ARCHIVE_PATH': str(self.archive.path),
                 'PYTHONIOENCODING': 'utf-8'})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                self.assertEqual(len(tools.tools), 5)
                result = await session.call_tool('search_discord', {'query': 'WEVORA'})
                self.assertFalse(result.is_error)
                self.assertIn('discord.com/channels/10/20/1', str(result))
                context = await session.call_tool('get_message_context', {'message_id': '1'})
                self.assertIn('WEVORA', str(context))

    async def test_http_chat_and_archive_health_auth(self):
        from interfaces.http_api import app
        with patch.dict(os.environ, {'HTTP_API_KEY': 'key'}), \
             patch('core.chat_controller.available_dbs', return_value=['general']), \
             patch('core.chat_controller.process', AsyncMock(return_value='answer')):
            with TestClient(app) as client:
                self.assertEqual(client.get('/db/list').status_code, 401)
                response = client.post('/chat', headers={'X-API-Key': 'key'},
                    json={'message': 'hello', 'session_id': 'session'})
                self.assertEqual(response.json()['reply'], 'answer')
        with patch.dict(os.environ, {}, clear=True):
            with TestClient(app) as client:
                self.assertEqual(client.get('/archive/status').status_code, 403)

    async def test_discord_qa_survives_archive_failure(self):
        from interfaces import discord_bot
        msg = discord_message()
        fake_bot = NS(user=NS(mentioned_in=lambda _: False), process_commands=AsyncMock())
        with patch.object(discord_bot, 'bot', fake_bot), \
             patch('core.discord_archive_sync.get_ingestor', side_effect=OSError('secret')):
            await discord_bot.on_message(msg)
        fake_bot.process_commands.assert_awaited_once_with(msg)

    async def test_notion_answer_flow_keeps_document_context(self):
        from core import chat_controller
        cfg = {'notion': {'enabled': True, 'database_ids': []}, 'rag': {'enabled': True}}
        page = {'id': 'page', 'url': 'https://www.notion.so/page', 'last_edited_time': 'today'}
        with patch('core.chat_controller.init_db'), \
             patch('core.chat_controller._load_db_config', return_value=cfg), \
             patch('core.chat_controller.save_message'), \
             patch('core.chat_controller._select_search_queries', AsyncMock(return_value=['query'])), \
             patch('core.chat_controller._search_notion_pages', AsyncMock(return_value=[page])), \
             patch('core.notion_client.get_api_key', return_value='fake-key'), \
             patch('core.notion_client.get_pdf_urls', return_value=[]), \
             patch('core.notion_client.get_page_title', return_value='Document'), \
             patch('core.notion_client.get_page_text', AsyncMock(return_value='Notion knowledge')), \
             patch('core.knowledge_search.search_knowledge', return_value=[
                 {'content': 'PDF knowledge', 'source': 'guide.pdf'}]), \
             patch('core.context_builder.build_messages_with_synthesized_info', return_value=[]), \
             patch('core.chat_controller._llm.chat', AsyncMock(side_effect=['summary', 'answer'])) as llm:
            answer = await chat_controller.process('question', 'session')
        self.assertIn('answer', answer)
        self.assertIn('https://www.notion.so/page', answer)
        self.assertIn('PDF knowledge', str(llm.call_args_list[0]))
        self.assertIn('Notion knowledge', str(llm.call_args_list[0]))

    async def test_slack_existing_mention_contract(self):
        # Import without connecting to Slack or loading real credentials.
        from unittest.mock import MagicMock
        fake_app = MagicMock()
        fake_app.event.side_effect = lambda _: lambda function: function
        fake_app.command.side_effect = lambda _: lambda function: function
        with patch('slack_bolt.async_app.AsyncApp', return_value=fake_app):
            import importlib
            slack = importlib.import_module('interfaces.slack_bot')
        say = AsyncMock()
        with patch('core.chat_controller.process', AsyncMock(return_value='answer')) as process:
            await slack.handle_mention({'text': '<@BOT> hello', 'channel': 'CH', 'ts': '123'}, say)
        process.assert_awaited_once_with('hello', 'slack-CH', 'general')
        say.assert_awaited_once_with(text='answer', thread_ts='123')

import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import discord
from discord import app_commands

from interfaces.command_catalog import install


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        names = ('slash_status db_current db_list db_create db_use memory_save memory_list '
                 'memory_capture memory_optimize memory_trim memory_clear rag_status rag_on '
                 'rag_off rag_backend rag_auto_sync rag_file rag_url rag_paste rag_sync_server '
                 'rag_scan_channel rag_sources rag_trim rag_clear notion_status notion_on '
                 'notion_off notion_db_add notion_db_remove notion_search').split()
        self.handlers = {name: AsyncMock() for name in names}
        self.tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
        install(self.tree, self.handlers)
        self.interaction = NS(response=NS(send_message=AsyncMock()))

    async def invoke(self, path, **options):
        parts = path.split()
        command = self.tree.get_command(parts[0])
        if len(parts) > 1:
            command = command.get_command(parts[1])
        await command.callback(self.interaction, **options)

    def test_production_tree_has_only_thirteen_commands(self):
        from interfaces.discord_bot import bot
        commands = list(bot.tree.walk_commands())
        leaves = {c.qualified_name for c in commands if isinstance(c, app_commands.Command)}
        self.assertEqual(leaves, {'chat', 'status', 'archive', 'db', 'memory', 'rag config',
                                 'rag add', 'rag sync', 'rag sources', 'rag delete',
                                 'notion config', 'notion db', 'notion search'})

    async def test_all_existing_operations_are_reachable(self):
        routes = [
            ('status', {}, 'slash_status'),
            ('status', {'target': 'db'}, 'db_current'),
            ('status', {'target': 'rag'}, 'rag_status'),
            ('status', {'target': 'notion'}, 'notion_status'),
            ('db', {}, 'db_list'),
            ('db', {'action': 'create', 'db_name': 'test', 'password': 'test'}, 'db_create'),
            ('db', {'action': 'use', 'db_name': 'test', 'password': 'test'}, 'db_use'),
            ('memory', {}, 'memory_list'),
            ('memory', {'action': 'save', 'text': 'test'}, 'memory_save'),
            ('memory', {'action': 'capture'}, 'memory_capture'),
            ('memory', {'action': 'optimize'}, 'memory_optimize'),
            ('memory', {'action': 'delete', 'memory_id': 5}, 'memory_trim'),
            ('memory', {'action': 'clear'}, 'memory_clear'),
            ('rag config', {}, 'rag_status'),
            ('rag config', {'action': 'enable'}, 'rag_on'),
            ('rag config', {'action': 'disable'}, 'rag_off'),
            ('rag config', {'action': 'backend', 'backend': 'json'}, 'rag_backend'),
            ('rag config', {'action': 'auto_sync', 'enabled': False}, 'rag_auto_sync'),
            ('rag add', {}, 'rag_paste'),
            ('rag add', {'url': 'https://example.com', 'source': 'example'}, 'rag_url'),
            ('rag add', {'file': NS(filename='test.txt')}, 'rag_file'),
            ('rag sync', {}, 'rag_sync_server'),
            ('rag sync', {'target': 'channel'}, 'rag_scan_channel'),
            ('rag sources', {}, 'rag_sources'),
            ('rag delete', {}, 'rag_trim'),
            ('rag delete', {'scope': 'all'}, 'rag_clear'),
            ('notion config', {}, 'notion_status'),
            ('notion config', {'action': 'enable', 'api_key': 'secret_test'}, 'notion_on'),
            ('notion config', {'action': 'disable'}, 'notion_off'),
            ('notion db', {'action': 'add', 'database_id': 'test'}, 'notion_db_add'),
            ('notion db', {'action': 'remove'}, 'notion_db_remove'),
            ('notion search', {'query': 'test'}, 'notion_search'),
        ]
        for path, options, expected in routes:
            with self.subTest(path=path, options=options):
                for handler in self.handlers.values():
                    handler.reset_mock()
                await self.invoke(path, **options)
                self.assertEqual([name for name, handler in self.handlers.items() if handler.await_count], [expected])
                self.interaction.response.send_message.assert_not_awaited()

    async def test_archive_scope_and_revocation_are_forwarded(self):
        with patch('interfaces.archive_commands.change', new_callable=AsyncMock) as change:
            await self.invoke('archive', action='register', scope='server')
            change.assert_awaited_once_with(self.interaction, None, True, 'server')
            change.reset_mock()
            await self.invoke('archive', action='unregister', scope='server')
            change.assert_awaited_once_with(self.interaction, None, False, 'server')
        with patch('interfaces.archive_commands.list_registered', new_callable=AsyncMock) as listing:
            await self.invoke('archive')
            await self.invoke('status', target='archive')
            self.assertEqual(listing.await_count, 2)

    async def test_ambiguous_and_missing_options_never_run_operations(self):
        cases = [
            ('db', {'action': 'create'}),
            ('memory', {'action': 'save'}),
            ('memory', {'action': 'delete'}),
            ('memory', {'action': 'clear', 'memory_id': 5}),
            ('rag config', {'action': 'backend'}),
            ('rag config', {'action': 'auto_sync'}),
            ('rag add', {'file': NS(), 'url': 'test'}),
            ('rag sync', {'channel': NS()}),
            ('rag sync', {'target': 'channel', 'mode': 'all'}),
            ('notion config', {'action': 'enable'}),
            ('notion config', {'action': 'disable', 'api_key': 'secret_test'}),
            ('notion db', {'action': 'add'}),
        ]
        for path, options in cases:
            with self.subTest(path=path):
                self.interaction.response.send_message.reset_mock()
                await self.invoke(path, **options)
                self.interaction.response.send_message.assert_awaited_once()
                for handler in self.handlers.values():
                    handler.assert_not_awaited()

    async def test_sync_preserves_zero_limit_and_false_incremental(self):
        await self.invoke('rag sync', limit=0, incremental=False)
        self.handlers['rag_sync_server'].assert_awaited_once_with(self.interaction, 'all', 0, False)

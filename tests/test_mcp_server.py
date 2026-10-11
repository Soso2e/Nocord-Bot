import json
import os
from unittest.mock import patch
from starlette.testclient import TestClient
from tests_support import ArchiveTestCase
from test_discord_archive import message
from core.archive_search import ArchiveSearch
from interfaces.mcp_server import create_server, http_app


class MCPTests(ArchiveTestCase):
    def server(self):
        search = ArchiveSearch(self.archive, self.policy)
        return create_server(lambda: search)

    async def test_tool_schema_search_context_boundary_and_scrub(self):
        self.archive.upsert_message(message())
        denied = message('2'); denied['channel_id'] = '21'
        self.archive.upsert_message(denied)
        server = self.server()
        tools = await server.list_tools()
        self.assertEqual({t.name for t in tools}, {'search_discord', 'get_message_context',
                         'get_thread', 'get_messages', 'list_channels'})
        self.assertTrue(all(t.annotations.read_only_hint for t in tools))
        result = await server.call_tool('search_discord', {'query': 'WEVORA'})
        text = str(result)
        self.assertIn('discord.com/channels/10/20/1', text)
        self.assertNotIn(str(self.archive.path), text)
        denied_result = await server.call_tool('get_message_context', {'message_id': '2'})
        self.assertNotIn('WEVORA', str(denied_result))
        self.policy.allow_guilds.clear()
        result = await server.call_tool('search_discord', {'query': 'WEVORA'})
        self.assertNotIn('WEVORA', str(result))

    async def test_http_initialize_tools_call_and_auth(self):
        self.archive.upsert_message(message())
        with patch.dict(os.environ, {'NOCORD_MCP_TOKEN': 'test-secret'}):
            app = http_app(self.server(), {'host': '127.0.0.1'})
        with TestClient(app, base_url='http://127.0.0.1:8001') as client:
            headers = {'Accept': 'application/json, text/event-stream'}
            req = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                'protocolVersion': '2025-03-26', 'capabilities': {},
                'clientInfo': {'name': 'test', 'version': '1'}}}
            self.assertEqual(client.post('/mcp', json=req, headers=headers).status_code, 401)
            headers['Authorization'] = 'Bearer test-secret'
            response = client.post('/mcp', json=req, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            headers['MCP-Protocol-Version'] = response.json()['result']['protocolVersion']
            result = client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 2,
                'method': 'tools/call', 'params': {'name': 'search_discord',
                                                 'arguments': {'query': 'WEVORA'}}})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertIn('discord.com/channels', result.text)
            self.assertNotIn('test-secret', result.text)

    async def test_remote_requires_secret_and_errors_are_sanitized(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                http_app(self.server(), {'host': '0.0.0.0'})
        def failing():
            raise OSError('secret token /private/db.sqlite')
        server = create_server(failing)
        with self.assertRaises(Exception) as caught:
            await server.call_tool('search_discord', {'query': 'query'})
        self.assertNotIn('/private', str(caught.exception))

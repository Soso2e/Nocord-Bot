import unittest
from unittest.mock import AsyncMock, Mock, patch

from core.llm_client import LLMClient
from core import router_runtime


class RoutingLLMTests(unittest.IsolatedAsyncioTestCase):
    async def test_dedicated_profile_never_falls_back_to_large_model(self):
        with patch('core.llm_client._load_config', return_value={'model': 'large'}):
            with self.assertRaises(ValueError):
                LLMClient('routing_llm')

    async def test_generation_limits_and_schema_reach_api(self):
        options = {'temperature': 0, 'max_tokens': 16,
                   'response_format': {'type': 'json_schema'},
                   'chat_template_kwargs': {'enable_thinking': False}}
        with patch('core.llm_client._load_config', return_value={
            'model': 'large', 'routing_llm': {'model': 'router', 'provider': 'openai',
                                            'base_url': 'http://127.0.0.1:1235', 'max_retries': 0, 'options': options}}):
            client = LLMClient('routing_llm')
        with patch.object(client, '_post_json', AsyncMock(return_value={'choices': [{'message': {'content': '{"reply":true}'}}]})) as post:
            await client.chat([{'role': 'user', 'content': 'request'}])
        payload = post.call_args.args[1]
        self.assertEqual(payload['model'], 'router')
        for key, value in options.items():
            self.assertEqual(payload[key], value)
        self.assertEqual(client._cfg['max_retries'], 0)

    def test_managed_runtime_rejects_remote_server_and_is_optional(self):
        with patch.object(router_runtime, '_load_config', return_value={'routing_llm': {'managed_lms': {'model_key': 'small'}, 'base_url': 'http://remote:1235'}}):
            with self.assertRaises(ValueError):
                router_runtime.prepare(Mock())
        with patch.object(router_runtime, '_load_config', return_value={}), \
             patch.object(router_runtime.subprocess, 'run') as run:
            router_runtime.prepare(Mock())
        run.assert_not_called()

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from core import reply_channels, reply_router
from interfaces import channel_commands, discord_bot


def message(mid=5, author=30, content='このエラーの原因を調べて', channel_id=20, guild_id=10):
    return NS(id=mid, author=NS(id=author, bot=author == 99, display_name=f'user-{author}'),
              content=content, channel=NS(id=channel_id), guild=NS(id=guild_id),
              reference=None, webhook_id=None, mentions=[], role_mentions=[],
              mention_everyone=False, type=discord.MessageType.default,
              created_at=datetime.now(timezone.utc), reply=AsyncMock())


def with_history(latest, *rows):
    reply_router.forget_channel(latest.guild.id, latest.channel.id)
    reply_router.cache_channel(latest.guild.id, latest.channel.id)
    for row in rows:
        reply_router.observe(row)
    async def history(**kwargs):
        for row in rows:
            yield row
    latest.channel.history = MagicMock(side_effect=history)
    latest.channel.fetch_message = AsyncMock()
    return latest


class ChannelSettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / 'app.json'
        self.config.write_text(json.dumps({'discord_archive': {
            'enabled': False, 'trust_zone': 'personal', 'db_path': str(Path(self.tmp.name) / 'archive.sqlite')
        }}), encoding='utf-8')
        self.env = patch.dict('os.environ', {'NOCORD_CONFIG': str(self.config)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def interaction(self, manage=True):
        guild = NS(id=10, me=NS(id=99))
        channel = MagicMock(spec=discord.TextChannel)
        channel.id, channel.guild, channel.mention = 20, guild, '<#20>'
        channel.permissions_for.return_value = NS(view_channel=True, read_message_history=True, send_messages=True)
        return NS(guild=guild, channel=channel, user=NS(guild_permissions=NS(manage_guild=manage)),
                  response=NS(send_message=AsyncMock(), defer=AsyncMock()), followup=NS(send=AsyncMock()))

    async def test_persistent_exact_channel_consent_independent_of_archive(self):
        original = self.config.read_bytes()
        self.assertFalse(reply_channels.is_enabled(10, 20))
        reply_channels.set_enabled(10, 20, True)
        reply_channels.set_enabled(11, 20, True)
        self.assertTrue(reply_channels.is_enabled(10, 20))
        self.assertFalse(reply_channels.is_enabled(10, 40))
        self.assertFalse(reply_channels.is_enabled(None, 20))
        reply_channels.set_enabled(10, 20, False)
        self.assertFalse(reply_channels.is_enabled(10, 20))
        self.assertTrue(reply_channels.is_enabled(11, 20))
        self.assertEqual(self.config.read_bytes(), original)

    async def test_corrupt_zone_mismatch_and_atomic_failure(self):
        reply_channels.set_enabled(10, 20, True)
        path = reply_channels.state_path(reply_channels.policy())
        original = path.read_bytes()
        with patch('core.reply_channels.os.replace', side_effect=OSError):
            with self.assertRaises(OSError):
                reply_channels.set_enabled(10, 21, True)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob('.reply-channels-*.tmp')), [])
        for bad in ('{partial', '[]', '{"trust_zone":"school","guilds":{}}',
                    '{"trust_zone":"personal","guilds":{"10":[20]}}'):
            path.write_text(bad, encoding='utf-8')
            with self.assertRaises(ValueError):
                reply_channels.is_enabled(10, 20)

    async def test_enable_list_disable_and_admin_permissions(self):
        interaction = self.interaction()
        await channel_commands.configure(interaction, True)
        self.assertTrue(reply_channels.is_enabled(10, 20))
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        await channel_commands.configure(interaction)
        self.assertIn('<#20>', interaction.followup.send.call_args.args[0])
        await channel_commands.configure(interaction, False)
        self.assertFalse(reply_channels.is_enabled(10, 20))
        await channel_commands.configure(self.interaction(manage=False), True)
        self.assertFalse(reply_channels.is_enabled(10, 20))

    async def test_dm_cross_guild_and_missing_permissions_cannot_enable(self):
        dm = self.interaction()
        dm.guild = None
        cross = self.interaction()
        cross.channel.guild = NS(id=11)
        denied = self.interaction()
        denied.channel.permissions_for.return_value.read_message_history = False
        for interaction in (dm, cross, denied):
            await channel_commands.configure(interaction, True)
        self.assertFalse(reply_channels.is_enabled(10, 20))


class RouterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reply_router._history_cache.clear()

    async def test_cache_orders_seed_and_keeps_four_before_latest(self):
        latest = with_history(message(mid=10), *(message(mid=i) for i in (8, 7, 6, 5)))
        reply_router.observe(message(mid=9))
        reply_router.observe(latest)
        with patch.object(reply_router._llm, 'chat', AsyncMock(return_value='{"reply":true}')):
            result = await reply_router.decide(latest, 99)
        self.assertEqual([row['id'] for row in json.loads(result.context)['history']], ['6', '7', '8', '9'])
        latest.channel.history.assert_not_called()

    def test_cache_drops_edited_deleted_and_disabled_content(self):
        latest = with_history(message(mid=5), message(mid=1), message(mid=2))
        reply_router.forget_messages(10, 20, [1])
        self.assertEqual([row['id'] for _, row in reply_router._history_cache[('10', '20')]], ['2'])
        reply_router.forget_channel(10, 20)
        reply_router.observe(latest)
        self.assertNotIn(('10', '20'), reply_router._history_cache)

    async def test_decision_uses_bounded_same_channel_history_and_author_identity(self):
        latest = with_history(message(), message(4, 99, '先ほどの説明'), message(3, 30, '状況説明'),
                              message(2, channel_id=21), message(1, guild_id=11))
        with patch.object(reply_router._llm, 'chat', AsyncMock(return_value='{"reply":true}')) as llm:
            result = await reply_router.decide(latest, 99)
        self.assertTrue(result.reply)
        payload = llm.call_args.args[0][-1]['content']
        self.assertIn('U1: 状況説明', payload)
        self.assertIn('Bot: 先ほどの説明', payload)
        self.assertIn('Latest message (U1)', payload)
        self.assertNotIn('user-30', payload)
        latest.channel.history.assert_not_called()
        self.assertEqual(len(json.loads(result.context)['history']), 2)
        self.assertIn('先ほどの説明', result.context)

    async def test_other_human_reply_is_rejected_without_classifier(self):
        latest = with_history(message())
        latest.reference = NS(message_id=1, channel_id=20, resolved=None, cached_message=None)
        latest.channel.fetch_message.return_value = message(1, author=40)
        with patch.object(reply_router._llm, 'chat', AsyncMock()) as llm:
            self.assertFalse((await reply_router.decide(latest, 99)).reply)
        llm.assert_not_awaited()

    async def test_reply_to_bot_passes_reference_to_classifier(self):
        latest = with_history(message(content='それはどう設定する？'))
        latest.reference = NS(message_id=1, channel_id=20, resolved=None, cached_message=None)
        latest.channel.fetch_message.return_value = message(1, author=99, content='設定の説明')
        with patch.object(reply_router._llm, 'chat', AsyncMock(return_value='{"reply":true}')) as llm:
            self.assertTrue((await reply_router.decide(latest, 99)).reply)
        self.assertIn('返信先Bot: 設定の説明', llm.call_args.args[0][-1]['content'])

    async def test_non_candidates_do_not_call_history_or_model(self):
        cases = [message(author=99), message(content='!status'), message(content='')]
        dm = message()
        dm.guild = None
        cases.append(dm)
        other = message()
        other.mentions = [NS(id=40)]
        cases.append(other)
        for field, value in (('mention_everyone', True), ('role_mentions', [NS(id=40)]), ('webhook_id', 55)):
            item = message()
            setattr(item, field, value)
            cases.append(item)
        with patch.object(reply_router._llm, 'chat', AsyncMock()) as llm:
            for item in cases:
                self.assertFalse((await reply_router.decide(item, 99)).reply)
        llm.assert_not_awaited()

    async def test_ignore_malformed_output_model_and_history_failure_stay_silent(self):
        for raw in ('{"reply":false}', '{"reply":"true"}', '{"reply":1}', 'please reply', '[]'):
            with patch.object(reply_router._llm, 'chat', AsyncMock(return_value=raw)):
                self.assertFalse((await reply_router.decide(with_history(message()), 99)).reply)
        with patch.object(reply_router._llm, 'chat', AsyncMock(side_effect=RuntimeError('secret'))):
            self.assertFalse((await reply_router.decide(with_history(message()), 99)).reply)
        latest = message()
        latest.channel.history = MagicMock(side_effect=OSError('secret'))
        with patch.object(reply_router._llm, 'chat', AsyncMock(return_value='{"reply":false}')):
            self.assertFalse((await reply_router.decide(latest, 99)).reply)
        latest.channel.history.assert_not_called()

    async def test_routing_timeout_is_bounded(self):
        async def slow(_):
            await asyncio.sleep(1)
        with patch.object(reply_router, 'ROUTING_TIMEOUT', .01), patch.object(reply_router._llm, 'chat', slow):
            result = await reply_router.decide(with_history(message()), 99)
            self.assertFalse(result.reply)
            self.assertEqual(result.reason, 'timeout')

    async def test_slow_success_within_limit_is_distinct_from_model_ignore(self):
        async def slow_success(_):
            await asyncio.sleep(.02)
            return '{"reply":true}'
        with patch.object(reply_router, 'ROUTING_TIMEOUT', .1), patch.object(reply_router._llm, 'chat', slow_success):
            result = await reply_router.decide(with_history(message(content='このサーバーについて要約して')), 99)
        self.assertTrue(result.reply)
        self.assertEqual(result.reason, 'model_reply')
        with patch.object(reply_router._llm, 'chat', AsyncMock(return_value='{"reply":false}')):
            result = await reply_router.decide(with_history(message(content='今日は疲れた')), 99)
        self.assertFalse(result.reply)
        self.assertEqual(result.reason, 'model_ignore')

    async def test_context_size_and_fast_acknowledgements(self):
        latest = with_history(message(content='問' * 2000), *(message(i, content='履' * 2000) for i in range(4, 0, -1)))
        with patch.object(reply_router._llm, 'chat', AsyncMock(return_value='{"reply":true}')) as llm:
            result = await reply_router.decide(latest, 99)
        context = json.loads(result.context)
        self.assertEqual(len(context['history']), 4)
        self.assertTrue(all(len(row['content']) == 128 for row in context['history']))
        prompt = llm.call_args.args[0][-1]['content']
        self.assertEqual(prompt.count('問'), 256)
        with patch.object(reply_router._llm, 'chat', AsyncMock()) as llm:
            result = await reply_router.decide(message(content='了解です。'), 99)
        self.assertEqual(result.reason, 'acknowledgement')
        llm.assert_not_awaited()


class DiscordReplyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reply_router._history_cache.clear()
        self.bot = NS(user=NS(id=99, mentioned_in=lambda msg: any(u.id == 99 for u in msg.mentions)),
                      process_commands=AsyncMock())
        self.patches = [patch.object(discord_bot, 'bot', self.bot),
                        patch('core.discord_archive_sync.dispatch', AsyncMock()),
                        patch.object(discord_bot.reply_channels, 'is_enabled', return_value=True)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    async def test_auto_reply_requires_classifier_and_carries_context(self):
        latest = message()
        with patch.object(reply_router, 'decide', AsyncMock(return_value=reply_router.ReplyDecision(True, 'history'))) as route, \
             patch.object(discord_bot, '_reply_to_message', AsyncMock()) as reply:
            await discord_bot.on_message(latest)
        route.assert_awaited_once_with(latest, 99, '!')
        reply.assert_awaited_once_with(latest, latest.content, conversation_context='history', automatic=True)

    async def test_ignored_or_unregistered_message_never_generates_or_captures_memory(self):
        with patch.object(reply_router, 'decide', AsyncMock(return_value=reply_router.ReplyDecision())) as route, \
             patch.object(discord_bot, '_reply_to_message', AsyncMock()) as reply:
            await discord_bot.on_message(message(content='ありがとう'))
            reply.assert_not_awaited()
            route.reset_mock()
            with patch.object(discord_bot.reply_channels, 'is_enabled', return_value=False):
                await discord_bot.on_message(message())
            route.assert_not_awaited()
            reply.assert_not_awaited()

    async def test_prefix_and_mentions_use_existing_paths_once(self):
        with patch.object(reply_router, 'decide', AsyncMock()) as route, \
             patch.object(discord_bot, '_reply_to_message', AsyncMock()) as reply:
            command = message(content='!chat hello')
            await discord_bot.on_message(command)
            self.bot.process_commands.assert_awaited_once_with(command)
            mentioned = message(content='<@99> hello')
            mentioned.mentions = [NS(id=99)]
            await discord_bot.on_message(mentioned)
            reply.assert_awaited_once_with(mentioned, 'hello')
            route.assert_not_awaited()

    async def test_disable_during_generation_suppresses_send_and_context_reaches_process(self):
        latest = message()
        latest.channel.typing = MagicMock(return_value=AsyncMock())
        with patch.object(discord_bot, '_db', return_value='general'), \
             patch.object(discord_bot.chat_controller, 'process', AsyncMock(return_value='answer')) as process, \
             patch.object(discord_bot.reply_channels, 'is_enabled', return_value=False):
            await discord_bot._reply_to_message(latest, latest.content, conversation_context='history', automatic=True)
        latest.reply.assert_not_awaited()
        self.assertEqual(process.call_args.kwargs['conversation_context'], 'history')
        self.assertIsNone(process.call_args.kwargs['progress'])

    async def test_auto_reply_model_error_does_not_post_error_message(self):
        latest = message()
        latest.channel.typing = MagicMock(return_value=AsyncMock())
        with patch.object(discord_bot, '_db', return_value='general'), \
             patch.object(discord_bot.chat_controller, 'process', AsyncMock(side_effect=RuntimeError('secret'))):
            await discord_bot._reply_to_message(latest, latest.content, automatic=True)
        latest.reply.assert_not_awaited()

    async def test_same_channel_events_are_serialized(self):
        events = []
        async def reply(item, *args, **kwargs):
            events.append(('start', item.id))
            await asyncio.sleep(.01)
            events.append(('end', item.id))
        with patch.object(reply_router, 'decide', AsyncMock(return_value=reply_router.ReplyDecision(True))), \
             patch.object(discord_bot, '_reply_to_message', reply):
            await asyncio.gather(discord_bot.on_message(message(mid=5)), discord_bot.on_message(message(mid=6)))
        self.assertEqual(events, [('start', 5), ('end', 5), ('start', 6), ('end', 6)])


class ReplyContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_controller_passes_context_but_saves_only_latest_message(self):
        from core import chat_controller
        with patch.object(chat_controller, 'init_db'), \
             patch.object(chat_controller, '_load_db_config', return_value={}), \
             patch.object(chat_controller, 'build_messages', return_value=[]) as build, \
             patch.object(chat_controller._llm, 'chat', AsyncMock(return_value='answer')), \
             patch.object(chat_controller, 'save_message') as save:
            await chat_controller.process('それは？', 'session', conversation_context='quoted history')
        self.assertEqual(build.call_args.kwargs['conversation_context'], 'quoted history')
        self.assertEqual(save.call_args_list[0].args, ('general', 'session', 'user', 'それは？'))
        self.assertEqual(save.call_args_list[1].args, ('general', 'session', 'assistant', 'answer'))

    def test_standard_and_notion_builders_keep_quoted_history_below_system_priority(self):
        from core import context_builder
        from core.gateway_config import ArchivePolicy
        with patch.object(context_builder, '_load_db_config', return_value={}), \
             patch.object(context_builder, 'get_history', return_value=[]), \
             patch.object(context_builder, 'find_relevant_memories', return_value=[]), \
             patch('core.gateway_config.load_policy', return_value=ArchivePolicy({})):
            normal = context_builder.build_messages('general', 'session', 'それは？', conversation_context='quoted history')
            notion = context_builder.build_messages_with_synthesized_info('general', 'session', 'それは？',
                                                                          'knowledge', conversation_context='quoted history')
        for messages in (normal, notion):
            self.assertEqual(messages[-2], {'role': 'user', 'content': 'quoted history'})
            self.assertEqual(messages[-1], {'role': 'user', 'content': 'それは？'})

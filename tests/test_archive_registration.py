import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
import discord

from tests_support import ArchiveTestCase
from test_discord_archive import message
from core.archive_allowlist import set_registration, state_path
from core.archive_search import ArchiveSearch
from core.gateway_config import load_policy
from interfaces import archive_commands


class RegistrationTests(ArchiveTestCase):
    def setUp(self):
        super().setUp()
        self.config = Path(self.tmp.name) / 'app.json'
        self.policy.config['db_path'] = str(self.archive.path)
        self.config.write_text(json.dumps({'discord_archive': self.policy.config}), encoding='utf-8')
        self.addCleanup(patch.stopall)
        patch.dict('os.environ', {'NOCORD_CONFIG': str(self.config)}).start()

    async def test_register_persist_revoke_static_consent_and_read_boundary(self):
        original = self.config.read_bytes()
        set_registration(load_policy(), 10, 21, True)
        self.assertTrue(load_policy().allows('10', '21'))
        self.assertFalse(load_policy().allows('11', '21'))
        self.assertEqual(self.config.read_bytes(), original)
        self.archive.upsert_message(message())
        set_registration(load_policy(), 10, 20, False)
        denied = load_policy()
        self.assertFalse(denied.allows('10', '20'))
        self.assertEqual(ArchiveSearch(self.archive, denied).search('WEVORA'), [])
        set_registration(denied, 10, 20, True)
        self.assertTrue(load_policy().allows('10', '20'))

    async def test_public_thread_revoke_overrides_parent_inherit(self):
        self.archive.upsert_message(message('2'))
        self.archive.upsert_message(message('3', thread_id='41'))
        self.archive.upsert_message(message(thread_id='40'))
        set_registration(load_policy(), 10, 40, False)
        policy = load_policy()
        self.assertFalse(policy.allows('10', '20', '40'))
        search = ArchiveSearch(self.archive, policy)
        self.assertEqual({r['message_id'] for r in search.search('WEVORA')}, {'2', '3'})
        self.assertEqual(search.get_thread('40'), [])
        self.assertTrue(policy.allows('10', '20', '41'))

    async def test_running_ingestion_observes_revocation(self):
        from core.discord_archive_sync import ArchiveIngestor
        from test_archive_events import discord_message
        ingest = ArchiveIngestor(self.archive, load_policy(), policy_provider=load_policy)
        set_registration(load_policy(), 10, 20, False)
        await ingest.on_create(discord_message())
        self.assertIsNone(self.archive.get_message('1'))

    async def test_zone_mismatch_corrupt_state_and_disabled_fail_closed(self):
        state_path(self.policy).write_text(json.dumps({'trust_zone': 'school', 'guilds': {}}), encoding='utf-8')
        with self.assertRaises(ValueError):
            load_policy()
        state_path(self.policy).write_text('{partial', encoding='utf-8')
        with self.assertRaises(ValueError):
            load_policy()
        self.policy.enabled = False
        with self.assertRaises(ValueError):
            set_registration(self.policy, 10, 20, True)

    def interaction(self, manage=True, guild=10):
        target = MagicMock(spec=discord.TextChannel)
        target.id, target.mention = 21, '<#21>'
        target.guild = NS(id=guild)
        return NS(guild=NS(id=guild), channel=target,
                  user=NS(guild_permissions=NS(manage_guild=manage)),
                  response=NS(send_message=AsyncMock(), defer=AsyncMock()),
                  followup=NS(send=AsyncMock()))

    async def test_command_register_current_channel_ephemeral(self):
        interaction = self.interaction()
        await archive_commands.register.callback(interaction)
        self.assertTrue(load_policy().allows('10', '21'))
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        self.assertTrue(interaction.followup.send.call_args.kwargs['ephemeral'])
        await archive_commands.unregister.callback(interaction)
        self.assertFalse(load_policy().allows('10', '21'))

    async def test_no_permission_dm_or_cross_guild_cannot_register(self):
        for interaction in (self.interaction(manage=False), self.interaction()):
            if interaction.user.guild_permissions.manage_guild:
                interaction.guild = None
            await archive_commands.register.callback(interaction)
            interaction.response.send_message.assert_awaited_once()
        interaction = self.interaction()
        target = self.interaction(guild=11).channel
        await archive_commands.register.callback(interaction, target)
        interaction.response.send_message.assert_awaited_once()
        self.assertFalse(state_path(self.policy).exists())

    async def test_list_only_current_guild_and_bounds(self):
        interaction = self.interaction()
        set_registration(load_policy(), 11, 999, True)
        await archive_commands.list_registered.callback(interaction)
        self.assertIn('<#20>', interaction.response.send_message.call_args.args[0])
        self.assertNotIn('<#999>', interaction.response.send_message.call_args.args[0])

    async def test_command_group_is_registered_and_admin_only(self):
        from interfaces.discord_bot import bot
        group = bot.tree.get_command('archive')
        self.assertEqual({cmd.name for cmd in group.commands}, {'register', 'unregister', 'list'})
        self.assertTrue(group.default_permissions.manage_guild)

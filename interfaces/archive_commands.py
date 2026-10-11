"""Guild-admin consent commands; channel IDs never need to be copied by hand."""
import asyncio
import logging
from typing import Literal
import discord

from core.archive_allowlist import set_registration, set_registrations
from core.gateway_config import load_policy

log = logging.getLogger(__name__)


async def authorize(interaction):
    if interaction.guild is None:
        await interaction.response.send_message('サーバー内で実行してください。', ephemeral=True)
        return False
    if not getattr(interaction.user.guild_permissions, 'manage_guild', False):
        await interaction.response.send_message('サーバーの管理権限が必要です。', ephemeral=True)
        return False
    return True


async def change_server(interaction, policy, enabled):
    guild = interaction.guild
    skipped = 0
    if enabled:
        member = guild.me
        if member is None:
            raise ValueError('Bot guild membership is unavailable')
        channels = await guild.fetch_channels()
        candidates = [c for c in channels if isinstance(c, (discord.TextChannel, discord.ForumChannel))]
        ids = []
        for target in candidates:
            permissions = target.permissions_for(member)
            if permissions.view_channel and permissions.read_message_history:
                ids.append(target.id)
            else:
                skipped += 1
    else:
        # Include separately registered threads and channels no longer in Discord.
        ids = policy.allow_guilds.get(str(guild.id), {}).get('channels', [])
    await asyncio.to_thread(set_registrations, policy, guild.id, ids, enabled)
    if enabled:
        text = (f'このサーバーの {len(ids)} チャンネルを保存対象に登録しました。'
                f'\n権限不足で除外: {skipped} 件。新しい投稿から自動保存します。'
                '\n公開スレッドは親の許可を継承します。個別解除済みのスレッドは対象外です。'
                '\n非公開スレッドはそのスレッド内で別途登録してください。'
                '\n追加されたチャンネルは /archive action:register scope:server を再実行して登録してください。'
                '\n過去ログは /rag sync で、設定済みのBackfill範囲を取り込めます。')
    else:
        text = (f'このサーバーの保存許可を {len(ids)} 件解除しました（個別登録したスレッドを含む）。'
                '\n過去の原文は物理削除しません。')
    await interaction.followup.send(text, ephemeral=True)


async def change(interaction, channel, enabled, scope='channel'):
    if not await authorize(interaction):
        return
    if scope not in ('channel', 'server') or (scope == 'server' and channel is not None):
        await interaction.response.send_message('サーバー全体を指定する場合は channel を指定しないでください。', ephemeral=True)
        return
    target = channel or interaction.channel
    if scope == 'channel' and (not isinstance(target, (discord.TextChannel, discord.Thread, discord.ForumChannel)) or target.guild.id != interaction.guild.id):
        await interaction.response.send_message('このサーバーのテキストチャンネルまたはスレッドを指定してください。', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        policy = load_policy()
        if not policy.enabled:
            await interaction.followup.send('先に起動設定の discord_archive.enabled を true にしてください。', ephemeral=True)
            return
        if scope == 'server':
            await change_server(interaction, policy, enabled)
            return
        await asyncio.to_thread(set_registration, policy, interaction.guild.id, target.id, enabled)
    except Exception as exc:
        log.warning('Archive registration failed (%s)', type(exc).__name__)
        await interaction.followup.send('保存対象の更新に失敗しました。ローカル設定を確認してください。', ephemeral=True)
        return
    if enabled:
        text = (f'{target.mention} を保存対象に登録しました。新しい投稿から自動保存します。'
                '\n過去ログは /rag sync で、設定済みのBackfill範囲を取り込めます。')
        if isinstance(target, discord.TextChannel) or isinstance(target, discord.ForumChannel):
            text += '\n公開スレッドも対象です。privateスレッドはそのスレッド内で別途登録してください。'
    else:
        text = (f'{target.mention} の保存許可を外しました。'
                '\n過去の原文は物理削除しません。明示登録された子スレッドは個別に解除してください。')
    await interaction.followup.send(text, ephemeral=True)


async def register(interaction: discord.Interaction,
                   channel: discord.TextChannel | discord.Thread | discord.ForumChannel | None = None,
                   scope: Literal['channel', 'server'] = 'channel'):
    await change(interaction, channel, True, scope)


async def unregister(interaction: discord.Interaction,
                     channel: discord.TextChannel | discord.Thread | discord.ForumChannel | None = None,
                     scope: Literal['channel', 'server'] = 'channel'):
    await change(interaction, channel, False, scope)


async def list_registered(interaction: discord.Interaction):
    if not await authorize(interaction):
        return
    try:
        policy = load_policy()
        channels = policy.allow_guilds.get(str(interaction.guild.id), {}).get('channels', []) if policy.enabled else []
        if not channels:
            text = '保存対象は未登録です。/archive action:register または /archive action:register scope:server を実行してください。'
        else:
            text = f'保存許可: {len(channels)}件\n' + '\n'.join(f'<#{c}>' for c in channels[:50])
            if len(channels) > 50:
                text += '\n最初の50件を表示しています。'
        await interaction.response.send_message(text, ephemeral=True)
    except Exception as exc:
        log.warning('Archive registration listing failed (%s)', type(exc).__name__)
        await interaction.response.send_message('ローカル設定を確認してください。', ephemeral=True)

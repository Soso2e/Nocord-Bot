"""Guild-admin consent commands; channel IDs never need to be copied by hand."""
import asyncio
import logging
import discord
from discord import app_commands

from core.archive_allowlist import set_registration
from core.gateway_config import load_policy

log = logging.getLogger(__name__)
archive_group = app_commands.Group(name='archive', description='Discordログの保存対象を管理する',
                                   guild_only=True, default_permissions=discord.Permissions(manage_guild=True))


async def authorize(interaction):
    if interaction.guild is None:
        await interaction.response.send_message('サーバー内で実行してください。', ephemeral=True)
        return False
    if not getattr(interaction.user.guild_permissions, 'manage_guild', False):
        await interaction.response.send_message('サーバーの管理権限が必要です。', ephemeral=True)
        return False
    return True


async def change(interaction, channel, enabled):
    if not await authorize(interaction):
        return
    target = channel or interaction.channel
    if not isinstance(target, (discord.TextChannel, discord.Thread, discord.ForumChannel)) or target.guild.id != interaction.guild.id:
        await interaction.response.send_message('このサーバーのテキストチャンネルまたはスレッドを指定してください。', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        policy = load_policy()
        if not policy.enabled:
            await interaction.followup.send('先に起動設定の discord_archive.enabled を true にしてください。', ephemeral=True)
            return
        await asyncio.to_thread(set_registration, policy, interaction.guild.id, target.id, enabled)
    except Exception as exc:
        log.warning('Archive registration failed (%s)', type(exc).__name__)
        await interaction.followup.send('保存対象の更新に失敗しました。ローカル設定を確認してください。', ephemeral=True)
        return
    if enabled:
        text = (f'{target.mention} を保存対象に登録しました。新しい投稿から自動保存します。'
                '\n過去ログは /rag sync_server で、設定済みのBackfill範囲を取り込めます。')
        if isinstance(target, discord.TextChannel) or isinstance(target, discord.ForumChannel):
            text += '\n公開スレッドも対象です。privateスレッドはそのスレッド内で別途登録してください。'
    else:
        text = (f'{target.mention} の保存許可を外しました。'
                '\n過去の原文は物理削除しません。明示登録された子スレッドは個別に解除してください。')
    await interaction.followup.send(text, ephemeral=True)


@archive_group.command(name='register', description='今いるチャンネル、または指定チャンネルを保存対象に登録する')
async def register(interaction: discord.Interaction,
                   channel: discord.TextChannel | discord.Thread | discord.ForumChannel | None = None):
    await change(interaction, channel, True)


@archive_group.command(name='unregister', description='今いるチャンネル、または指定チャンネルの明示保存許可を外す')
async def unregister(interaction: discord.Interaction,
                     channel: discord.TextChannel | discord.Thread | discord.ForumChannel | None = None):
    await change(interaction, channel, False)


@archive_group.command(name='list', description='このサーバーで許可された保存対象を表示する')
async def list_registered(interaction: discord.Interaction):
    if not await authorize(interaction):
        return
    try:
        policy = load_policy()
        channels = policy.allow_guilds.get(str(interaction.guild.id), {}).get('channels', []) if policy.enabled else []
        if not channels:
            text = '保存対象は未登録です。対象チャンネルで /archive register を実行してください。'
        else:
            text = f'保存許可: {len(channels)}件\n' + '\n'.join(f'<#{c}>' for c in channels[:50])
            if len(channels) > 50:
                text += '\n最初の50件を表示しています。'
        await interaction.response.send_message(text, ephemeral=True)
    except Exception as exc:
        log.warning('Archive registration listing failed (%s)', type(exc).__name__)
        await interaction.response.send_message('ローカル設定を確認してください。', ephemeral=True)

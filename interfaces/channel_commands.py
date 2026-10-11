"""Manage channels that may receive replies without explicit mentions."""
import asyncio
import logging

import discord

from core import reply_channels, reply_router
from interfaces.archive_commands import authorize

log = logging.getLogger(__name__)


async def configure(interaction, enabled=None, channel=None):
    if not await authorize(interaction):
        return
    target = channel or interaction.channel
    if not isinstance(target, (discord.TextChannel, discord.Thread)) or target.guild.id != interaction.guild.id:
        await interaction.response.send_message('このサーバーのテキストチャンネルまたはスレッドを指定してください。', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        if enabled is None:
            channels = await asyncio.to_thread(reply_channels.registered_channels, interaction.guild.id)
            status = '有効' if str(target.id) in channels else '無効'
            text = f'{target.mention} のメンションなし応答: {status}\n有効なチャンネル: {len(channels)}件'
            if channels:
                text += '\n' + '\n'.join(f'<#{cid}>' for cid in channels[:50])
            if len(channels) > 50:
                text += '\n最初の50件を表示しています。'
        else:
            if enabled:
                member = interaction.guild.me
                if member is None:
                    raise ValueError('Bot membership unavailable')
                permissions = target.permissions_for(member)
                can_send = permissions.send_messages_in_threads if isinstance(target, discord.Thread) else permissions.send_messages
                if not (permissions.view_channel and permissions.read_message_history and can_send):
                    await interaction.followup.send('Botに閲覧・履歴取得・投稿権限が必要です。', ephemeral=True)
                    return
            await asyncio.to_thread(reply_channels.set_enabled, interaction.guild.id, target.id, enabled)
            if enabled:
                reply_router.cache_channel(interaction.guild.id, target.id)
            else:
                reply_router.forget_channel(interaction.guild.id, target.id)
            text = f'{target.mention} のメンションなし応答を{"有効" if enabled else "無効"}にしました。'
            if enabled:
                text += '\n直近の会話からBotへの質問・依頼と判定した投稿にだけ返信します。スレッドは個別に設定してください。'
    except Exception as exc:
        log.warning('Reply channel configuration failed (%s)', type(exc).__name__)
        text = '応答チャンネルの設定を確認・更新できませんでした。ローカル設定を確認してください。'
    await interaction.followup.send(text, ephemeral=True)

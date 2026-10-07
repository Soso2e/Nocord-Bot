"""Consent-aware ingestion; persistence never invokes embeddings or chat."""
import asyncio
import logging
from core.discord_archive import DiscordArchive, utc

log = logging.getLogger(__name__)


def message_dto(message):
    channel = message.channel
    parent_id = getattr(channel, 'parent_id', None)
    parent = getattr(channel, 'parent', None)
    author = message.author
    return dict(
        message_id=str(message.id), guild_id=str(message.guild.id), guild_name=message.guild.name,
        channel_id=str(parent_id or channel.id),
        channel_name=getattr(parent, 'name', None) if parent_id else channel.name,
        thread_id=str(channel.id) if parent_id else None,
        thread_name=channel.name if parent_id else None,
        author_id=str(author.id), author_name=getattr(author, 'display_name', author.name),
        content=message.content, created_at=message.created_at, edited_at=message.edited_at,
        reply_to_message_id=str(message.reference.message_id)
            if message.reference and message.reference.message_id else None,
        attachments=[dict(attachment_id=str(a.id), filename=a.filename,
                          content_type=a.content_type, size=a.size, url=a.url)
                     for a in message.attachments])


class ArchiveIngestor:
    def __init__(self, archive, policy):
        self.archive = archive
        self.policy = policy

    def save(self, message):
        guild = getattr(message, 'guild', None)
        channel = message.channel
        parent = getattr(channel, 'parent_id', None)
        if not self.policy.allows(getattr(guild, 'id', None), parent or channel.id):
            return False
        if message.author.bot and not self.policy.include_bots:
            return False
        changed = self.archive.upsert_message(message_dto(message))
        self.event_seen()
        return changed

    def event_seen(self):
        with self.archive.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS archive_state (key TEXT PRIMARY KEY,value TEXT)')
            db.execute('INSERT OR REPLACE INTO archive_state VALUES (?,?)', ('last_event_at', utc()))

    async def on_create(self, message):
        try:
            await asyncio.to_thread(self.save, message)
        except Exception as exc:
            log.warning('Archive create failed (%s)', type(exc).__name__)

    async def on_update(self, payload, bot):
        try:
            if payload.guild_id is None:
                return
            channel = bot.get_channel(payload.channel_id)
            if channel is None:
                channel = await bot.fetch_channel(payload.channel_id)
            parent = getattr(channel, 'parent_id', None)
            if not self.policy.allows(payload.guild_id, parent or channel.id):
                return
            # Partial raw payloads omit unchanged fields; fetch full authoritative DTO.
            message = await channel.fetch_message(payload.message_id)
            await asyncio.to_thread(self.save, message)
        except Exception as exc:
            log.warning('Archive update failed (%s)', type(exc).__name__)

    def delete(self, guild_id, channel_id, message_ids, parent_id=None):
        for mid in message_ids:
            old = self.archive.get_message(mid)
            if old:
                if not self.policy.allows_message(old):
                    continue
            elif not self.policy.allows(guild_id, parent_id or channel_id):
                continue
            else:
                # Uncached deletion before history finishes must also prevent resurrection.
                self.archive.upsert_message(dict(message_id=str(mid), guild_id=str(guild_id),
                    channel_id=str(parent_id or channel_id),
                    thread_id=str(channel_id) if parent_id else None, content='', created_at=utc()))
            self.archive.mark_deleted(mid)
            self.event_seen()

    async def on_delete(self, payload, bot):
        try:
            channel = bot.get_channel(payload.channel_id)
            mids = getattr(payload, 'message_ids', None) or [payload.message_id]
            await asyncio.to_thread(self.delete, payload.guild_id, payload.channel_id,
                                    mids, getattr(channel, 'parent_id', None))
        except Exception as exc:
            log.warning('Archive delete failed (%s)', type(exc).__name__)


_ingestor = None


def get_ingestor():
    global _ingestor
    from core.gateway_config import load_policy
    policy = load_policy()
    if not policy.enabled:
        return None
    if _ingestor is None or _ingestor.policy.config != policy.config:
        _ingestor = ArchiveIngestor(DiscordArchive(policy.database_path()), policy)
    return _ingestor


async def dispatch(event, *args):
    try:
        ingestor = get_ingestor()
        if ingestor:
            await getattr(ingestor, 'on_' + event)(*args)
    except Exception as exc:
        log.warning('Archive dispatch failed (%s)', type(exc).__name__)

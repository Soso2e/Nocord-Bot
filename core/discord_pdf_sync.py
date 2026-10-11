"""PDF attachment ingestion for Discord server RAG synchronization.

The Discord interface already owns the text-message synchronization loop. This
module wraps that loop so the same manual and daily sync operations also ingest
PDF attachments without duplicating the existing command implementation.
"""

from __future__ import annotations

import asyncio
import datetime
from functools import wraps
from typing import Any, Iterable

_PATCH_FLAG = "_nocord_pdf_sync_installed"


def _created_after(message: Any, after: datetime.datetime | None) -> bool:
    if after is None:
        return True
    created_at = getattr(message, "created_at", None)
    if created_at is None:
        return True
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=datetime.timezone.utc)
    if after.tzinfo is None:
        after = after.replace(tzinfo=datetime.timezone.utc)
    return created_at > after


def _eligible_message(message: Any, after: datetime.datetime | None) -> bool:
    author = getattr(message, "author", None)
    return not bool(getattr(author, "bot", False)) and _created_after(message, after)


def _pdf_source(attachment: Any) -> str:
    """Build a stable source ID while retaining the filename for readability."""
    filename = str(getattr(attachment, "filename", "document.pdf"))
    attachment_id = getattr(attachment, "id", None)
    if attachment_id is None:
        return filename
    return f"discord-pdf:{attachment_id}:{filename}"


def collect_pdf_attachments(
    messages: Iterable[Any],
    existing_sources: set[str],
) -> list[tuple[Any, str]]:
    """Return unique, not-yet-ingested PDF attachments and their source IDs.

    A plain filename is also checked because the older `/rag scan_channel`
    command stored PDFs using only the filename as the RAG source.
    """
    queued_sources: set[str] = set()
    queue: list[tuple[Any, str]] = []

    for message in messages:
        for attachment in getattr(message, "attachments", ()):
            filename = str(getattr(attachment, "filename", ""))
            if not filename.lower().endswith(".pdf"):
                continue
            source = _pdf_source(attachment)
            if source in existing_sources or filename in existing_sources or source in queued_sources:
                continue
            queued_sources.add(source)
            queue.append((attachment, source))

    return queue


async def _read_messages(
    channel: Any,
    mode: str,
    limit: int | None,
    after: datetime.datetime | None,
) -> list[Any]:
    if mode == "pinned":
        pinned = await channel.pins()
        return [
            message
            for message in reversed(pinned)
            if _eligible_message(message, after)
        ]

    messages: list[Any] = []
    async for message in channel.history(limit=limit, after=after, oldest_first=True):
        if _eligible_message(message, after):
            messages.append(message)
    return messages


async def _ingest_pdf_queue(
    queue: list[tuple[Any, str]],
    db_name: str,
    existing_sources: set[str],
) -> dict:
    from core import chat_controller
    from core.ingest_helpers import read_bytes

    processed = 0
    chunks = 0
    errors: list[str] = []

    for attachment, source in queue:
        filename = str(getattr(attachment, "filename", "document.pdf"))
        try:
            data = await attachment.read()
            text = await asyncio.to_thread(read_bytes, data, filename)
            if not text or not text.strip():
                raise ValueError("テキストを抽出できませんでした（画像のみのPDFの可能性があります）")
            count = await asyncio.to_thread(
                chat_controller.rag_ingest_text,
                db_name,
                text,
                source,
            )
            existing_sources.add(source)
            processed += 1
            chunks += count
        except Exception as exc:
            errors.append(f"PDF `{filename}`: {exc}")

    return {"pdfs_processed": processed, "pdf_chunks": chunks, "errors": errors}


async def sync_guild_pdfs(
    guild: Any,
    db_name: str,
    mode: str = "all",
    limit_per_channel: int | None = 500,
    after: datetime.datetime | None = None,
) -> dict:
    """Scan accessible Discord channels and threads for new PDF attachments."""
    from core import chat_controller

    existing_sources = set(
        await asyncio.to_thread(chat_controller.rag_list_sources, db_name)
    )
    pdf_queue: list[tuple[Any, str]] = []
    errors: list[str] = []
    me = getattr(guild, "me", None)
    from core.gateway_config import load_policy
    archive_policy = load_policy()

    for channel in getattr(guild, "text_channels", ()):
        if archive_policy.enabled and not archive_policy.allows(guild.id, channel.id):
            continue
        try:
            if me is not None:
                permissions = channel.permissions_for(me)
                if not (
                    getattr(permissions, "read_messages", False)
                    and getattr(permissions, "read_message_history", False)
                ):
                    continue

            if mode != "threads":
                messages = await _read_messages(channel, mode, limit_per_channel, after)
                pdf_queue.extend(collect_pdf_attachments(messages, existing_sources))

            if mode in ("all", "threads"):
                thread_by_id = {
                    getattr(thread, "id", id(thread)): thread
                    for thread in getattr(channel, "threads", ())
                }
                try:
                    async for thread in channel.archived_threads(limit=50):
                        thread_by_id[getattr(thread, "id", id(thread))] = thread
                except Exception as exc:
                    errors.append(f"#{getattr(channel, 'name', channel)} archived threads: {exc}")

                for thread in thread_by_id.values():
                    try:
                        messages = await _read_messages(
                            thread,
                            "all",
                            limit_per_channel,
                            after,
                        )
                        pdf_queue.extend(collect_pdf_attachments(messages, existing_sources))
                    except Exception as exc:
                        errors.append(f"thread {getattr(thread, 'name', thread)}: {exc}")
        except Exception as exc:
            errors.append(f"#{getattr(channel, 'name', channel)}: {exc}")

    # A source can appear in multiple channels. Remove duplicate queue entries
    # after all channel histories have been collected.
    unique_queue: list[tuple[Any, str]] = []
    seen_sources: set[str] = set()
    for attachment, source in pdf_queue:
        if source in seen_sources:
            continue
        seen_sources.add(source)
        unique_queue.append((attachment, source))

    result = await _ingest_pdf_queue(unique_queue, db_name, existing_sources)
    result["errors"] = errors + result["errors"]
    if result["pdfs_processed"] or result["errors"]:
        print(
            f"[PDFSync] {getattr(guild, 'name', getattr(guild, 'id', 'unknown'))}: "
            f"{result['pdfs_processed']} PDFs, {result['pdf_chunks']} chunks, "
            f"{len(result['errors'])} errors"
        )
    return result


def install(discord_bot_module: Any) -> None:
    """Wrap `interfaces.discord_bot._sync_guild_to_rag` once."""
    if getattr(discord_bot_module, _PATCH_FLAG, False):
        return

    original_sync = discord_bot_module._sync_guild_to_rag

    @wraps(original_sync)
    async def sync_with_pdfs(
        guild: Any,
        db_name: str,
        mode: str = "all",
        limit_per_channel: int | None = 500,
        after: datetime.datetime | None = None,
    ) -> dict:
        result = dict(
            await original_sync(
                guild,
                db_name,
                mode=mode,
                limit_per_channel=limit_per_channel,
                after=after,
            )
        )
        pdf_result = await sync_guild_pdfs(
            guild,
            db_name,
            mode=mode,
            limit_per_channel=limit_per_channel,
            after=after,
        )
        result["pdfs_processed"] = pdf_result["pdfs_processed"]
        result["pdf_chunks"] = pdf_result["pdf_chunks"]
        result["total_chunks"] = result.get("total_chunks", 0) + pdf_result["pdf_chunks"]
        result.setdefault("errors", []).extend(pdf_result["errors"])
        return result

    discord_bot_module._sync_guild_to_rag = sync_with_pdfs
    setattr(discord_bot_module, _PATCH_FLAG, True)

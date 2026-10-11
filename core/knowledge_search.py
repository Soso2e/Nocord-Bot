"""Compose existing document RAG with consented Discord evidence for Discord Q&A."""
from datetime import datetime
from core.gateway_config import load_policy


def conversation_blocks(messages):
    """Reproducible 30-minute conversation blocks, never original storage."""
    blocks = []
    current = None
    previous = None
    for row in sorted(messages, key=lambda r: (r['channel_id'], r.get('thread_id') or '',
                                              r['created_at'], r['message_id'])):
        if not row['content']:
            continue
        ts = datetime.fromisoformat(row['created_at'])
        scope = (row['channel_id'], row.get('thread_id'), ts.date())
        if current is None or current['scope'] != scope or (ts - previous).total_seconds() > 1800:
            current = {'scope': scope, 'message_ids': [], 'lines': []}
            blocks.append(current)
        current['message_ids'].append(row['message_id'])
        current['lines'].append(f"[{row['created_at']} | {row['discord_url']}] "
                                f"{row['author_name']}: {row['content']}")
        previous = ts
    return [{'message_ids': b['message_ids'], 'content': '\n'.join(b['lines'])} for b in blocks]


def search_knowledge(db_name, query, k=4, score_threshold=0.3, *, archive_guild_id=None,
                     rag_enabled=True):
    policy = load_policy()
    docs = []
    if rag_enabled:
        try:
            from core.rag_manager import search
            docs = search(db_name, query, k=k, score_threshold=score_threshold)
        except Exception:
            pass
    if policy.enabled:
        # Old direct-history sources are stale after edits/deletes and cannot bypass consent.
        docs = [d for d in docs if not d.get('source', '').startswith('discord:')]
    if policy.enabled and policy.config.get('qa_enabled', False) and archive_guild_id is not None:
        from core.discord_archive_sync import get_ingestor
        from core.archive_search import create_search
        from core.db_registry import get_guild_db
        from core.gateway_config import load_app_config
        bound = get_guild_db(int(archive_guild_id)) or load_app_config().get('default_db', 'general')
        if bound == db_name:
            try:
                ingestor = get_ingestor()
                search = create_search(ingestor.archive, policy)
                hits = search.search(
                    query, limit=k, guild_id=str(archive_guild_id))
                seen = set()
                for hit in hits:
                    if hit['message_id'] in seen:
                        continue
                    # Generate the semantic conversation block from current raw messages.
                    # Retrieval context is bounded; storage remains message-level.
                    blocks = conversation_blocks(search.get_message_context(hit['message_id']))
                    block = next((b for b in blocks if hit['message_id'] in b['message_ids']), None)
                    if block:
                        seen.update(block['message_ids'])
                        docs.append(dict(content=block['content'], source=hit['discord_url'], score=hit['score']))
            except Exception:
                pass
    return docs

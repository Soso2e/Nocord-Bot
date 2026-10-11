"""Synthetic live-model check; never sends messages to Discord."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import reply_router, reply_channels

CASES = [
    ('このサーバーについて要約して', True),
    ('ありがとう', False),
    ('明日の予定は？', True),
    ('今日は疲れた', False),
    ('使い方を教えてください', True),
    ('了解です', False),
    ('このエラーを調べて', True),
    ('作業が終わりました', False),
]


def message(mid, text, author=30):
    return NS(id=mid, content=text, author=NS(id=author, bot=author == 99),
              guild=NS(id=10), channel=NS(id=20), reference=None, mentions=[],
              role_mentions=[], mention_everyone=False, webhook_id=None,
              created_at=datetime.now(timezone.utc))


async def main():
    results = []
    raw_outputs = []
    original_chat = reply_router._llm.chat
    async def capture(messages):
        raw = await original_chat(messages)
        raw_outputs.append(raw)
        return raw
    reply_router._llm.chat = capture
    for text, expected in CASES:
        raw_outputs.clear()
        latest = message(10, text)
        reply_router.forget_channel(10, 20)
        reply_router.cache_channel(10, 20)
        for mid in range(4, 0, -1):
            reply_router.observe(message(mid, '作業の状況を共有しています。'))
        async def history(**kwargs):
            for mid in range(4, 0, -1):
                yield message(mid, '作業の状況を共有しています。')
        latest.channel.history = history
        started = time.monotonic()
        decision = await reply_router.decide(latest, 99)
        result = dict(text=text, expected=expected, actual=decision.reply,
                      reason=decision.reason, seconds=round(time.monotonic() - started, 3),
                      raw=raw_outputs[-1] if raw_outputs else '')
        results.append(result)
        print(json.dumps(result, ensure_ascii=True), flush=True)
    print(json.dumps({'correct': sum(r['expected'] == r['actual'] and r['reason'] in ('model_reply', 'model_ignore', 'acknowledgement') for r in results),
                      'total': len(results), 'max_seconds': max(r['seconds'] for r in results)}, ensure_ascii=True))
    if '--discord-history' in sys.argv:
        await discord_history_check()


async def discord_history_check():
    """Read four messages from one registered channel; no Discord writes."""
    import httpx
    from dotenv import dotenv_values
    values = dotenv_values(Path(__file__).resolve().parent.parent / '.env', encoding='utf-8-sig')
    from unittest.mock import patch
    with patch.dict('os.environ', {k: values[k] for k in ('NOCORD_CONFIG', 'NOCORD_ARCHIVE_PATH') if values.get(k)}):
        state = reply_channels.read_state(reply_channels.policy())
    guild_id, channels = next((g, ids) for g, ids in state['guilds'].items() if ids)
    channel_id = channels[0]
    headers = {'Authorization': 'Bot ' + values['DISCORD_TOKEN']}
    async with httpx.AsyncClient(timeout=.5, headers=headers) as client:
        identity = await client.get('https://discord.com/api/v10/users/@me')
        identity.raise_for_status()
        bot_id = int(identity.json()['id'])
        latest = message(1, 'このサーバーについて要約して')
        latest.guild.id, latest.channel.id = int(guild_id), int(channel_id)
        async def history(**kwargs):
            response = await client.get(f'https://discord.com/api/v10/channels/{channel_id}/messages',
                                        params={'limit': kwargs['limit']})
            response.raise_for_status()
            for row in response.json():
                created = datetime.fromisoformat(row['timestamp'])
                if created < kwargs['after']:
                    continue
                item = message(int(row['id']), row['content'], int(row['author']['id']))
                item.guild.id, item.channel.id = int(guild_id), int(channel_id)
                item.author.bot = row['author'].get('bot', False)
                yield item
        latest.channel.history = history
        reply_router.forget_channel(guild_id, channel_id)
        reply_router.cache_channel(guild_id, channel_id)
        from datetime import timedelta
        seed_started = time.monotonic()
        newest = 0
        async for item in history(limit=4, after=latest.created_at - timedelta(seconds=600)):
            reply_router.observe(item)
            newest = max(newest, item.id)
        latest.id = newest + 1
        seed_seconds = round(time.monotonic() - seed_started, 3)
        started = time.monotonic()
        decision = await reply_router.decide(latest, bot_id)
        print(json.dumps({'discord_history_read_only': True, 'reply': decision.reply,
                          'reason': decision.reason, 'seed_seconds': seed_seconds,
                          'cached_decision_seconds': round(time.monotonic() - started, 3)}))


if __name__ == '__main__':
    asyncio.run(main())

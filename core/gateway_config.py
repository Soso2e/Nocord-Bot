"""One process, token, database and read policy per trust zone."""
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_app_config():
    path = Path(os.getenv('NOCORD_CONFIG', str(ROOT / 'config' / 'app.json')))
    with path.open(encoding='utf-8') as stream:
        return json.load(stream)


class ArchivePolicy:
    def __init__(self, config):
        self.config = config
        self.enabled = config.get('enabled', False) is True
        self.zone = config.get('trust_zone', 'personal')
        if not isinstance(self.zone, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', self.zone):
            raise ValueError('Invalid trust zone name')
        self.include_bots = config.get('include_bots', False) is True
        self.allow_guilds = config.get('allow_guilds', {})
        self.denials = config.get('_registration_denials', {})
        if not isinstance(self.allow_guilds, dict):
            raise ValueError('allow_guilds must be a mapping')
        for guild, entry in self.allow_guilds.items():
            if not str(guild).isdigit() or not isinstance(entry, dict):
                raise ValueError('Allowlist requires guild IDs and channel lists')
            channels = entry.get('channels', [])
            if not isinstance(channels, list) or any(not str(c).isdigit() for c in channels):
                raise ValueError('Allowlist channels must be a list of IDs')

    def allows(self, guild_id, channel_id, thread_id=None, private=False):
        # Threads inherit explicit parent-channel consent. DMs never qualify.
        if not self.enabled or guild_id is None:
            return False
        channels = self.allow_guilds.get(str(guild_id), {}).get('channels', [])
        allowed = {str(c) for c in channels}
        denied = set(self.denials.get(str(guild_id), []))
        if str(thread_id if thread_id is not None else channel_id) in denied:
            return False
        if private:
            return thread_id is not None and str(thread_id) in allowed
        return str(channel_id) in allowed or (thread_id is not None and str(thread_id) in allowed)

    def allows_message(self, row):
        return self.allows(row['guild_id'], row['channel_id'], row.get('thread_id'),
                           private=bool(row.get('is_private_thread', False)))

    def sql_scope(self, alias='m'):
        pairs = [(str(g), str(c)) for g, data in self.allow_guilds.items()
                 for c in data.get('channels', [])] if self.enabled else []
        if not pairs:
            return '0', []
        sql = '(' + ' OR '.join(
            f'({alias}.guild_id=? AND (({alias}.channel_id=? AND {alias}.is_private_thread=0) '
            f'OR {alias}.thread_id=?))' for _ in pairs) + ')'
        params = [v for g,c in pairs for v in (g,c,c)]
        for guild, channels in self.denials.items():
            for channel in channels:
                sql += (f" AND NOT ({alias}.guild_id=? AND (COALESCE({alias}.thread_id,'')=? OR "
                        f'({alias}.thread_id IS NULL AND {alias}.channel_id=?)))')
                params.extend([str(guild), str(channel), str(channel)])
        return sql, params

    def database_path(self):
        path = Path(os.getenv('NOCORD_ARCHIVE_PATH', self.config.get(
            'db_path', f'data/{self.zone}/archive.sqlite')))
        return path if path.is_absolute() else ROOT / path


def load_policy():
    from core.archive_allowlist import effective_config
    base = ArchivePolicy(load_app_config().get('discord_archive', {}))
    if not base.enabled:
        return base
    return ArchivePolicy(effective_config(base))

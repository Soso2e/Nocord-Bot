"""One process, token, database and read policy per trust zone."""
import json
import os
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
        self.include_bots = config.get('include_bots', False) is True
        self.allow_guilds = config.get('allow_guilds', {})
        if not isinstance(self.allow_guilds, dict):
            raise ValueError('allow_guilds must be a mapping')

    def allows(self, guild_id, channel_id, thread_id=None):
        # Threads inherit explicit parent-channel consent. DMs never qualify.
        if not self.enabled or guild_id is None:
            return False
        channels = self.allow_guilds.get(str(guild_id), {}).get('channels', [])
        return str(channel_id) in {str(c) for c in channels}

    def allows_message(self, row):
        return self.allows(row['guild_id'], row['channel_id'], row.get('thread_id'))

    def sql_scope(self, alias='m'):
        pairs = [(str(g), str(c)) for g, data in self.allow_guilds.items()
                 for c in data.get('channels', [])] if self.enabled else []
        if not pairs:
            return '0', []
        return '(' + ' OR '.join(f'({alias}.guild_id=? AND {alias}.channel_id=?)'
                                for _ in pairs) + ')', [v for p in pairs for v in p]

    def database_path(self):
        path = Path(os.getenv('NOCORD_ARCHIVE_PATH', self.config.get(
            'db_path', f'data/{self.zone}/archive.sqlite')))
        return path if path.is_absolute() else ROOT / path


def load_policy():
    return ArchivePolicy(load_app_config().get('discord_archive', {}))

"""Machine-local, trust-zone-scoped consent for unmentioned replies."""
import json
import os
from pathlib import Path
import tempfile
import threading

from core.gateway_config import ArchivePolicy, load_app_config

_lock = threading.RLock()


def policy():
    return ArchivePolicy(load_app_config().get('discord_archive', {}))


def state_path(current):
    return current.database_path().with_suffix('.reply-channels.json')


def read_state(current):
    path = state_path(current)
    if not path.exists():
        return {'trust_zone': current.zone, 'guilds': {}}
    state = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(state, dict) or state.get('trust_zone') != current.zone or not isinstance(state.get('guilds'), dict):
        raise ValueError('Invalid reply channel trust zone')
    for guild_id, channels in state['guilds'].items():
        if not str(guild_id).isdigit() or not isinstance(channels, list) or any(not isinstance(c, str) or not c.isdigit() for c in channels):
            raise ValueError('Invalid reply channel registration')
    return state


def registered_channels(guild_id):
    if guild_id is None:
        return []
    return read_state(policy())['guilds'].get(str(guild_id), [])


def is_enabled(guild_id, channel_id):
    return str(channel_id) in registered_channels(guild_id)


def set_enabled(guild_id, channel_id, enabled):
    guild_id, channel_id = str(guild_id), str(channel_id)
    if not guild_id.isdigit() or not channel_id.isdigit() or type(enabled) is not bool:
        raise ValueError('Reply channel registration requires IDs and a boolean')
    current = policy()
    path = state_path(current)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        state = read_state(current)
        channels = set(state['guilds'].get(guild_id, []))
        if enabled:
            channels.add(channel_id)
        else:
            channels.discard(channel_id)
        state['guilds'][guild_id] = sorted(channels)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.reply-channels-', suffix='.tmp')
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(state, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

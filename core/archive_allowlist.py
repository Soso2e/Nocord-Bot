"""Machine-local Discord registrations layered over configured consent."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading

_lock = threading.RLock()


def state_path(policy):
    return policy.database_path().with_suffix('.allowlist.json')


def read_state(policy):
    path = state_path(policy)
    if not path.exists():
        return {'trust_zone': policy.zone, 'guilds': {}}
    state = json.loads(path.read_text(encoding='utf-8'))
    if state.get('trust_zone') != policy.zone or not isinstance(state.get('guilds'), dict):
        raise ValueError('Invalid registration trust zone')
    for guild, entries in state['guilds'].items():
        if not str(guild).isdigit() or not isinstance(entries, dict):
            raise ValueError('Invalid registration guild')
        for channel, enabled in entries.items():
            if not str(channel).isdigit() or type(enabled) is not bool:
                raise ValueError('Invalid registration channel')
    return state


def effective_config(policy):
    config = deepcopy(policy.config)
    config['_registration_denials'] = {}
    for guild, changes in read_state(policy)['guilds'].items():
        entry = config.setdefault('allow_guilds', {}).setdefault(guild, {'channels': []})
        allowed = {str(c) for c in entry.get('channels', [])}
        for channel, enabled in changes.items():
            if enabled:
                allowed.add(channel)
            else:
                allowed.discard(channel)
                config['_registration_denials'].setdefault(guild, []).append(channel)
        entry['channels'] = sorted(allowed)
    return config


def set_registration(policy, guild_id, channel_id, enabled):
    if not policy.enabled:
        raise ValueError('Archive must be enabled before registration')
    guild_id, channel_id = str(guild_id), str(channel_id)
    if not guild_id.isdigit() or not channel_id.isdigit() or type(enabled) is not bool:
        raise ValueError('Registration requires guild/channel IDs and a boolean')
    path = state_path(policy)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        state = read_state(policy)
        state['guilds'].setdefault(guild_id, {})[channel_id] = enabled
        # Publish a complete snapshot; readers never observe a partially written policy.
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.allowlist-', suffix='.tmp')
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(state, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if Path(temporary).exists():
                Path(temporary).unlink()

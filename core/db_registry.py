import hashlib
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

STATE_PATH = Path(__file__).parent.parent / "config" / "discord_state.json"


def _default_state() -> dict:
    return {
        "guilds": {},
        "db_credentials": {},
    }


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return _default_state()
    with open(STATE_PATH, encoding="utf-8") as f:
        data = json.load(f)
    state = _default_state()
    state.update(data)
    state["guilds"] = data.get("guilds", {})
    state["db_credentials"] = data.get("db_credentials", {})
    return state


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _hash_password(password: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def register_db(db_name: str, password: str, guild_id: int) -> None:
    state = _load_state()
    if db_name in state["db_credentials"]:
        raise ValueError(f"DB '{db_name}' already exists")

    salt = secrets.token_hex(16)
    state["db_credentials"][db_name] = {
        "salt": salt,
        "password_hash": _hash_password(password, salt),
        "created_by_guild_id": str(guild_id),
        "created_at": _now_iso(),
    }
    state["guilds"][str(guild_id)] = {"db_name": db_name, "bound_at": _now_iso()}
    _save_state(state)


def verify_db_password(db_name: str, password: str) -> bool:
    creds = _load_state()["db_credentials"].get(db_name)
    if not creds:
        return False
    return creds["password_hash"] == _hash_password(password, creds["salt"])


def bind_guild_db(guild_id: int, db_name: str) -> None:
    state = _load_state()
    state["guilds"][str(guild_id)] = {"db_name": db_name, "bound_at": _now_iso()}
    _save_state(state)


def get_guild_db(guild_id: int) -> str | None:
    guild = _load_state()["guilds"].get(str(guild_id), {})
    return guild.get("db_name")


def get_guild_sync_state(guild_id: int) -> dict:
    guild = _load_state()["guilds"].get(str(guild_id), {})
    return guild.get("sync_state", {"last_sync_at": None, "auto_sync_enabled": False})


def update_guild_sync_state(guild_id: int, **kwargs) -> None:
    state = _load_state()
    guild_entry = state["guilds"].setdefault(str(guild_id), {})
    sync_state = guild_entry.setdefault("sync_state", {"last_sync_at": None, "auto_sync_enabled": False})
    sync_state.update(kwargs)
    _save_state(state)

"""Track per-DB Notion page sync state to avoid redundant RAG ingestion.

Sync records are stored in databases/{db_name}/notion_sync.json:
  {page_id: {"last_edited_time": "...", "rag_source": "notion:{page_id}"}}
"""
import json
from pathlib import Path

DB_BASE = Path(__file__).parent.parent / "databases"


def _sync_path(db_name: str) -> Path:
    return DB_BASE / db_name / "notion_sync.json"


def _load(db_name: str) -> dict:
    path = _sync_path(db_name)
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(db_name: str, records: dict) -> None:
    path = _sync_path(db_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def needs_sync(db_name: str, page_id: str, last_edited_time: str) -> bool:
    """Return True if the page is new or has been edited since it was last synced."""
    record = _load(db_name).get(page_id)
    if not record:
        return True
    return record.get("last_edited_time", "") != last_edited_time


def mark_synced(db_name: str, page_id: str, last_edited_time: str) -> None:
    """Record that this page's PDF has been synced to RAG."""
    records = _load(db_name)
    records[page_id] = {
        "last_edited_time": last_edited_time,
        "rag_source": get_rag_source(page_id),
    }
    _save(db_name, records)


def get_rag_source(page_id: str) -> str:
    """Return the RAG source identifier for a Notion page."""
    return f"notion:{page_id}"

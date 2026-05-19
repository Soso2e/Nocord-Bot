import json
from pathlib import Path
from core.memory_manager import find_relevant_memories, get_history

DB_BASE = Path(__file__).parent.parent / "databases"

# Appended to system_prompt when RAG/Notion knowledge base is active
_RAG_CONSTRAINT = (
    "\n\n【回答ルール】\n"
    "あなたはデータベース（Notion・知識ベース）をもとに回答する専用ボットです。以下の3パターンで回答してください。\n\n"
    "1. データベースに情報がある場合:\n"
    "   データベースの内容を主軸に回答してください。文章を読みやすくする程度の最低限の補足は許可しますが、"
    "詳細な説明や独自の見解は加えないでください。\n\n"
    "2. データベースに情報がない場合:\n"
    "   1〜2文の簡単な概要（「〜は〇〇を担当する部署です」程度）を述べたうえで、"
    "「詳細情報はデータベースに登録されていません」と伝えてください。\n\n"
    "3. 挨拶・雑談など情報検索を必要としない場合:\n"
    "   自然に会話として返してください。"
)


def _load_db_config(db_name: str) -> dict:
    path = DB_BASE / db_name / "config.json"
    if not path.exists():
        return {"system_prompt": "You are a helpful assistant.", "memory_policy": {}}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_messages(db_name: str, session_id: str, user_input: str) -> list[dict]:
    cfg = _load_db_config(db_name)
    system_prompt = cfg.get("system_prompt", "You are a helpful assistant.")
    max_ctx = cfg.get("memory_policy", {}).get("max_context_messages", 20)
    rag_cfg = cfg.get("rag", {})
    rag_enabled = rag_cfg.get("enabled", False)

    history = get_history(db_name, session_id, limit=max_ctx)
    memories = find_relevant_memories(db_name, user_input, limit=5)

    if rag_enabled:
        system_prompt = system_prompt + _RAG_CONSTRAINT

    messages = [{"role": "system", "content": system_prompt}]

    # RAG knowledge (highest priority — placed before conversation memories)
    if rag_enabled:
        try:
            from core import rag_manager
            k = rag_cfg.get("retrieval_k", 4)
            threshold = rag_cfg.get("score_threshold", 0.3)
            rag_docs = rag_manager.search(db_name, user_input, k=k, score_threshold=threshold)
            if rag_docs:
                doc_sections = "\n---\n".join(
                    f"[出典: {d['source']}]\n{d['content']}" for d in rag_docs
                )
                messages.append({
                    "role": "system",
                    "content": "参照知識ベース:\n" + doc_sections,
                })
        except Exception:
            pass  # RAG unavailable — degrade gracefully to memory-only mode

    # Conversation memories (lower priority than knowledge docs)
    if memories:
        memory_lines = "\n".join(f"- {m['content']}" for m in memories)
        messages.append({
            "role": "system",
            "content": "長期記憶（会話から学んだ情報）:\n" + memory_lines,
        })

    messages.extend(history)
    messages.append({"role": "user", "content": user_input})
    return messages


def build_messages_with_synthesized_info(
    db_name: str,
    session_id: str,
    user_input: str,
    synthesized_info: str,
) -> list[dict]:
    """Like build_messages but uses pre-synthesized external context instead of raw RAG docs.

    Called by the 2-stage Notion LLM flow: LLM① produces synthesized_info,
    which replaces the raw knowledge-base injection in the final LLM② prompt.
    """
    cfg = _load_db_config(db_name)
    system_prompt = cfg.get("system_prompt", "You are a helpful assistant.")
    max_ctx = cfg.get("memory_policy", {}).get("max_context_messages", 20)

    history = get_history(db_name, session_id, limit=max_ctx)
    memories = find_relevant_memories(db_name, user_input, limit=5)

    messages = [{"role": "system", "content": system_prompt + _RAG_CONSTRAINT}]

    if synthesized_info:
        messages.append({
            "role": "system",
            "content": "統制済み情報（RAG・Notion検索結果を統合）:\n" + synthesized_info,
        })

    if memories:
        memory_lines = "\n".join(f"- {m['content']}" for m in memories)
        messages.append({
            "role": "system",
            "content": "長期記憶（会話から学んだ情報）:\n" + memory_lines,
        })

    messages.extend(history)
    messages.append({"role": "user", "content": user_input})
    return messages


def list_available_dbs() -> list[str]:
    if not DB_BASE.exists():
        return []
    return [d.name for d in DB_BASE.iterdir() if (d / "config.json").exists()]

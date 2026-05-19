import json
import inspect
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from core.context_builder import build_messages, list_available_dbs
from core.db_registry import bind_guild_db, register_db, verify_db_password
from core.llm_client import LLMClient
from core.memory_manager import (
    clear_history,
    delete_memory,
    get_all_memories,
    init_db,
    list_memories,
    replace_all_memories,
    save_memory,
    save_message,
    vacuum_db,
)

_llm = LLMClient()
_query_llm = LLMClient(profile="query_llm")
ProgressCallback = Callable[[str], Awaitable[None] | None]
_DB_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{3,32}$")
_MEMORY_JSON_RE = re.compile(r"\[[\s\S]*\]")
_HISTORY_LINE_RE = re.compile(r"^\[(?P<user_id>\d+)\|(?P<name>[^\]]+)\]:\s*(?P<content>.+)$")
_SELF_NAME_PATTERNS = [
    re.compile(r"(?:ぼく|僕|おれ|俺|わたし|私)[はって]?\s*(?P<alias>[^\s。、「」]+?)\s*(?:っていう|って言う|です|だよ|だ|といいます|と言います)"),
    re.compile(r"(?P<alias>[^\s。、「」]+?)\s*(?:って呼んで|ってよんで|と呼んで|でいいよ)"),
]
_MEMORY_CAPTURE_MAX_LINES = 40
_MEMORY_CAPTURE_MAX_CHARS = 6000
_NOTION_ASCII_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,}")
_NOTION_QUOTED_RE = re.compile(r"[「『\"']([^」』\"']{2,})[」』\"']")
_JSON_ARRAY_RE = re.compile(r"\[[\s\S]*\]")


def _db_dir(db_name: str) -> Path:
    return Path(__file__).parent.parent / "databases" / db_name


def _default_db_config(db_name: str) -> dict:
    return {
        "name": db_name,
        "system_prompt": "あなたはデータベース（Notion・知識ベース）をもとに回答する専用ボットです。提供されたデータベースの情報を使って回答し、データベースに情報がない場合は「データベースに登録されていません」と伝えてください。",
        "style": "friendly",
        "memory_policy": {
            "auto_save": True,
            "max_context_messages": 20,
        },
        "allowed_tools": ["save_memory"],
    }


async def _emit_progress(progress: ProgressCallback | None, message: str) -> None:
    if progress is None:
        return
    try:
        result = progress(message)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        print(f"[Progress] Callback failed: {exc}")


def _memory_extraction_messages(history_text: str, existing_memories: list[str] | None = None) -> list[dict]:
    existing_section = ""
    if existing_memories:
        existing_section = "\n\nAlready saved memories — do NOT re-extract these or anything semantically equivalent:\n" + "\n".join(
            f"- {m}" for m in existing_memories
        )
    prompt = (
        "You extract durable long-term memories from chat logs.\n"
        "Return JSON only.\n"
        "Output format: [{\"content\": \"...\"}, ...]\n"
        "Rules:\n"
        "- Keep only information worth remembering later.\n"
        "- Prefer user preferences, profile facts, ongoing projects, decisions, promises, recurring workflows, and constraints.\n"
        "- Ignore small talk, one-off jokes, and temporary chatter.\n"
        "- Write each memory as a short standalone sentence in Japanese.\n"
        "- Return only NEW facts not already covered by existing memories.\n"
        "- Do not duplicate near-identical items.\n"
        "- If nothing new is worth saving, return []"
        + existing_section
    )
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": history_text},
    ]


def _parse_memory_candidates(raw_text: str, limit: int = 5) -> list[str]:
    text = raw_text.strip()
    candidates: list[str] = []

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = _MEMORY_JSON_RE.search(text)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                data = None
        else:
            data = None

    if isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                value = item.strip()
            elif isinstance(item, dict):
                value = str(item.get("content", "")).strip()
            else:
                value = ""
            if value:
                candidates.append(value)

    if candidates:
        return candidates[:limit] if limit > 0 else candidates

    for line in text.splitlines():
        cleaned = line.strip().lstrip("-*0123456789. ").strip()
        if cleaned:
            candidates.append(cleaned)
    return candidates[:limit] if limit > 0 else candidates


def _normalize_memory_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _extract_rule_based_memories(history_lines: list[str]) -> list[str]:
    memories: list[str] = []

    for line in history_lines:
        match = _HISTORY_LINE_RE.match(line.strip())
        if not match:
            continue

        user_id = match.group("user_id")
        content = match.group("content").strip()
        for pattern in _SELF_NAME_PATTERNS:
            alias_match = pattern.search(content)
            if not alias_match:
                continue
            alias = _normalize_memory_text(alias_match.group("alias"))
            alias = alias.strip("。、「」\"'")
            if len(alias) > 24:
                continue
            if alias:
                memories.append(f"{user_id}: {alias}")
                break

    return memories[:5]


def _prepare_history_for_memory_extraction(history_lines: list[str]) -> str:
    trimmed_lines = [line.strip() for line in history_lines if line and line.strip()]
    if not trimmed_lines:
        return ""

    trimmed_lines = trimmed_lines[-_MEMORY_CAPTURE_MAX_LINES:]
    if len(trimmed_lines) == 1:
        return trimmed_lines[0][-_MEMORY_CAPTURE_MAX_CHARS:]

    result: list[str] = []
    total = 0
    for line in reversed(trimmed_lines):
        line_len = len(line) + 1
        if result and total + line_len > _MEMORY_CAPTURE_MAX_CHARS:
            break
        if not result and line_len > _MEMORY_CAPTURE_MAX_CHARS:
            result.append(line[-_MEMORY_CAPTURE_MAX_CHARS:])
            break
        result.append(line)
        total += line_len
    result.reverse()
    return "\n".join(result)


_SYNTHESIS_SYSTEM_PROMPT = (
    "あなたは情報抽出アシスタントです。提供されたデータベース情報（RAG知識ベース・Notion）をもとに、"
    "ユーザーの質問に答えるための情報サマリーを日本語で生成してください。\n\n"
    "ルール:\n"
    "- 提供された情報源に書かれた内容だけを使う。詳細説明に自分の学習済み知識は使わない\n"
    "- 重複情報は1つにまとめる\n"
    "- 矛盾する情報がある場合は両方を記載し「※情報源間で矛盾あり」と注記する\n"
    "- 質問に無関係な情報は省略する\n"
    "- データベースに該当情報がない場合は「関連情報なし」のみ返す（補完・推測禁止）"
)


def _build_synthesis_messages(
    user_input: str,
    rag_docs: list[dict],
    notion_results: list[dict],
) -> list[dict]:
    sections: list[str] = [f"質問: {user_input}"]

    if rag_docs:
        rag_lines = "\n---\n".join(
            f"[出典: {d['source']}]\n{d['content']}" for d in rag_docs
        )
        sections.append("【RAG知識ベース検索結果】\n" + rag_lines)

    if notion_results:
        notion_lines = "\n---\n".join(
            f"[Notionページ: {r['title']}]\n{r['text']}" for r in notion_results
        )
        sections.append("【Notion検索結果】\n" + notion_lines)

    return [
        {"role": "system", "content": _SYNTHESIS_SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(sections)},
    ]


def _build_notion_search_queries(message: str, max_queries: int = 4) -> list[str]:
    """Build robust Notion search queries from natural chat text.

    Notion search is sensitive to query wording. A page that matches "cad" may not
    match "cadについて何か情報ある", so chat requests should try the original text
    plus compact keyword candidates.
    """
    queries: list[str] = []

    def add(value: str) -> None:
        cleaned = re.sub(r"\s+", " ", value).strip(" 　?？!！。、「」『』\"'")
        if cleaned and cleaned.lower() not in {q.lower() for q in queries}:
            queries.append(cleaned)

    add(message)

    for match in _NOTION_QUOTED_RE.finditer(message):
        add(match.group(1))

    for token in _NOTION_ASCII_TOKEN_RE.findall(message):
        add(token)

    return queries[:max_queries]


def _parse_search_query_json(raw_text: str) -> list[str]:
    text = raw_text.strip()
    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_ARRAY_RE.search(text)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                data = None

    queries: list[str] = []
    if isinstance(data, list):
        values = data
    elif isinstance(data, dict):
        values = data.get("queries", [])
    else:
        values = []

    for item in values:
        value = str(item).strip() if item is not None else ""
        if value and value.lower() not in {q.lower() for q in queries}:
            queries.append(value)
    return queries


def _expand_search_queries(queries: list[str], max_queries: int = 4) -> list[str]:
    expanded: list[str] = []

    def add(value: str) -> None:
        cleaned = re.sub(r"\s+", " ", value).strip(" 　?？!！。、「」『』\"'")
        if cleaned and cleaned.lower() not in {q.lower() for q in expanded}:
            expanded.append(cleaned)

    for query in queries:
        ascii_tokens = _NOTION_ASCII_TOKEN_RE.findall(query)
        for token in ascii_tokens:
            add(token)
        add(query)

    return expanded[:max_queries]


async def _select_search_queries(message: str, max_queries: int = 4) -> list[str]:
    prompt = (
        "あなたはNotion/RAG検索用の検索語を選定する係です。\n"
        "ユーザー発話から、実際に検索すべき名詞・固有名詞・略語だけを抽出してください。\n"
        "助詞や依頼表現（について、教えて、情報ある、調べて等）は除外します。\n"
        "英字略語は原文の表記を優先し、必要なら日本語表記も追加します。\n"
        "返答はJSON配列のみ。例: [\"CAD\"]\n"
        "最大4件。検索語が不明なら [] を返してください。"
    )
    try:
        raw = await _query_llm.chat([
            {"role": "system", "content": prompt},
            {"role": "user", "content": message},
        ])
        queries = _parse_search_query_json(raw)
    except Exception as exc:
        print(f"[SearchQuery] LLM query selection failed: {exc}")
        queries = []
    if not queries:
        queries = _build_notion_search_queries(message, max_queries=max_queries)
    return _expand_search_queries(queries, max_queries=max_queries)



async def _search_notion_pages(
    queries: list[str],
    database_ids: list[str],
    api_key: str,
    max_results: int,
    errors: list[str] | None = None,
) -> list[dict]:
    from core import notion_client

    pages: list[dict] = []
    seen_ids: set[str] = set()

    for query in queries:
        if len(pages) >= max_results:
            break
        try:
            found = await notion_client.search_pages(
                query,
                database_ids,
                api_key,
                max_results,
            )
        except Exception as exc:
            error = f"{query}: {exc}"
            if errors is not None:
                errors.append(error)
            print(f"[Notion] Search failed for query '{query}': {exc}")
            continue

        for page in found:
            page_id = page.get("id", "")
            if page_id and page_id not in seen_ids:
                seen_ids.add(page_id)
                pages.append(page)
                if len(pages) >= max_results:
                    break

    return pages


def _format_notion_page_ref(page: dict) -> str:
    from core import notion_client

    title = notion_client.get_page_title(page) or "Untitled"
    page_id = page.get("id", "")
    url = page.get("url") or (f"https://www.notion.so/{page_id.replace('-', '')}" if page_id else "")
    if url:
        return f"[{title}]({url})"
    return f"{title} (`{page_id}`)"


def _format_notion_found_message(pages: list[dict]) -> str:
    if not pages:
        return "Notionで該当ページは見つかりませんでした"

    refs = [_format_notion_page_ref(page) for page in pages[:3]]
    suffix = f" ほか{len(pages) - 3}件" if len(pages) > 3 else ""
    return "Notionで該当ページを発見: " + " / ".join(refs) + suffix


def _format_notion_results_found_message(notion_results: list[dict]) -> str:
    if not notion_results:
        return "Notionで該当ページは見つかりませんでした"

    refs: list[str] = []
    for result in notion_results[:3]:
        title = result.get("title") or "Untitled"
        url = result.get("url", "")
        page_id = result.get("id", "")
        if url:
            refs.append(f"[{title}]({url})")
        elif page_id:
            refs.append(f"{title} (`{page_id}`)")
        else:
            refs.append(title)
    suffix = f" ほか{len(notion_results) - 3}件" if len(notion_results) > 3 else ""
    return "Notionで該当ページを発見: " + " / ".join(refs) + suffix


def _format_notion_references(notion_results: list[dict]) -> str:
    refs: list[str] = []
    seen: set[str] = set()
    for result in notion_results:
        title = result.get("title") or "Untitled"
        url = result.get("url", "")
        page_id = result.get("id", "")
        key = url or page_id or title
        if key in seen:
            continue
        seen.add(key)
        if url:
            refs.append(f"- [{title}]({url})")
        elif page_id:
            refs.append(f"- {title} (`{page_id}`)")
    return "\n".join(refs)


async def _process_with_notion(
    message: str,
    session_id: str,
    db_name: str,
    cfg: dict,
    notion_cfg: dict,
    progress: ProgressCallback | None = None,
) -> str:
    """2-stage LLM flow: LLM① synthesizes RAG+Notion info, LLM② generates the reply."""
    from core import notion_client, notion_rag_sync
    from core.context_builder import build_messages_with_synthesized_info
    from core.rag_manager import delete_by_source, ingest_text
    from core.rag_manager import search as rag_search

    await _emit_progress(progress, "Notion連携を確認中...")
    api_key = notion_client.get_api_key(notion_cfg)
    if not api_key:
        raise ValueError("Notion API key not configured (set notion.api_key in DB config or NOTION_API_KEY env var)")

    database_ids: list[str] = notion_cfg.get("database_ids", [])
    pdf_property: str = notion_cfg.get("pdf_property", "PDF")
    max_results: int = notion_cfg.get("max_results", 5)

    await _emit_progress(progress, "検索語を選定中...")
    search_queries = await _select_search_queries(message)
    if search_queries:
        await _emit_progress(progress, "検索語: " + " / ".join(search_queries))
    else:
        await _emit_progress(progress, "検索語を選定できませんでした")

    await _emit_progress(progress, "Notionを検索中...")
    notion_pages = await _search_notion_pages(search_queries, database_ids, api_key, max_results)
    if notion_pages:
        await _emit_progress(progress, f"Notion候補を{len(notion_pages)}件取得")

    # Sync PDFs to RAG for new/updated pages
    for page in notion_pages:
        page_id = page.get("id", "")
        last_edited = page.get("last_edited_time", "")
        pdf_urls = notion_client.get_pdf_urls(page, pdf_property)

        if pdf_urls and notion_rag_sync.needs_sync(db_name, page_id, last_edited):
            rag_source = notion_rag_sync.get_rag_source(page_id)
            delete_by_source(db_name, rag_source)
            for url in pdf_urls:
                try:
                    pdf_text = await notion_client.download_pdf_text(url)
                    if pdf_text:
                        ingest_text(db_name, pdf_text, source=rag_source)
                except Exception as exc:
                    print(f"[Notion] PDF sync failed for page {page_id}: {exc}")
            notion_rag_sync.mark_synced(db_name, page_id, last_edited)

    # RAG search after sync so newly indexed PDFs are included
    rag_docs: list[dict] = []
    rag_cfg = cfg.get("rag", {})
    if rag_cfg.get("enabled", False):
        await _emit_progress(progress, "RAGを検索中...")
        try:
            rag_query = search_queries[0] if search_queries else message
            rag_docs = rag_search(
                db_name,
                rag_query,
                k=rag_cfg.get("retrieval_k", 4),
                score_threshold=rag_cfg.get("score_threshold", 0.3),
            )
        except Exception:
            pass

    # Fetch Notion page texts
    # Trust Notion's search API for relevance — no secondary string-match filter.
    notion_results: list[dict] = []
    for page in notion_pages:
        try:
            title = notion_client.get_page_title(page)
            text = await notion_client.get_page_text(page["id"], api_key)
            if title or text:
                notion_results.append({
                    "id": page.get("id", ""),
                    "title": title,
                    "url": page.get("url", ""),
                    "text": text,
                })
        except Exception as exc:
            print(f"[Notion] Page text fetch failed for {page.get('id', '')}: {exc}")

    await _emit_progress(progress, _format_notion_results_found_message(notion_results))

    # No external info found — fall back to the normal single-stage flow
    if not rag_docs and not notion_results:
        await _emit_progress(progress, "メモリを検索中...")
        messages = build_messages(db_name, session_id, message)
        await _emit_progress(progress, "回答を生成中...")
        return await _llm.chat(messages)

    # LLM① — synthesize RAG + Notion into a compact information summary
    synthesis_msgs = _build_synthesis_messages(message, rag_docs, notion_results)
    synthesized = await _llm.chat(synthesis_msgs)

    # LLM② — generate the final user-facing reply
    await _emit_progress(progress, "メモリを検索中...")
    final_messages = build_messages_with_synthesized_info(
        db_name, session_id, message, synthesized
    )
    await _emit_progress(progress, "回答を生成中...")
    reply = await _llm.chat(final_messages)
    references = _format_notion_references(notion_results)
    if references:
        reply = reply.rstrip() + "\n\n参照したNotionページ:\n" + references
    return reply


async def process(
    message: str,
    session_id: str,
    db_name: str = "general",
    progress: ProgressCallback | None = None,
) -> str:
    init_db(db_name)
    cfg = _load_db_config(db_name)
    notion_cfg = cfg.get("notion", {})

    if notion_cfg.get("enabled", False):
        try:
            reply = await _process_with_notion(message, session_id, db_name, cfg, notion_cfg, progress)
        except Exception as exc:
            print(f"[Notion] Integration error, falling back to standard flow: {exc}")
            await _emit_progress(progress, f"Notion検索をスキップ: {exc}")
            if cfg.get("rag", {}).get("enabled", False):
                await _emit_progress(progress, "RAGを検索中...")
            await _emit_progress(progress, "メモリを検索中...")
            messages = build_messages(db_name, session_id, message)
            await _emit_progress(progress, "回答を生成中...")
            reply = await _llm.chat(messages)
    else:
        await _emit_progress(progress, "Notion検索をスキップ: Notion連携が無効です")
        if cfg.get("rag", {}).get("enabled", False):
            await _emit_progress(progress, "RAGを検索中...")
        await _emit_progress(progress, "メモリを検索中...")
        messages = build_messages(db_name, session_id, message)
        await _emit_progress(progress, "回答を生成中...")
        reply = await _llm.chat(messages)

    save_message(db_name, session_id, "user", message)
    save_message(db_name, session_id, "assistant", reply)
    return reply


async def capture_memories_from_history(
    db_name: str,
    history_lines: list[str],
    author_id: str = "",
    source: str = "discord_capture",
) -> dict:
    cleaned_lines = [line.strip() for line in history_lines if line and line.strip()]
    if not cleaned_lines:
        return {"saved": [], "error": ""}

    rule_based_candidates = _extract_rule_based_memories(cleaned_lines)
    history_text = _prepare_history_for_memory_extraction(cleaned_lines)

    existing_items = list_memories(db_name, limit=200)
    existing_contents = [item["content"] for item in existing_items]
    existing_set = {_normalize_memory_text(c).lower() for c in existing_contents}

    raw = "[]"
    llm_error = ""
    try:
        raw = await _llm.chat(_memory_extraction_messages(history_text, existing_contents))
    except RuntimeError as exc:
        llm_error = str(exc)
        print(f"[MemoryCapture] LLM extraction skipped due to error: {exc}")
    candidates = rule_based_candidates + _parse_memory_candidates(raw, limit=0)
    if not candidates:
        return {"saved": [], "error": llm_error}

    saved: list[dict] = []
    for candidate in candidates:
        normalized = _normalize_memory_text(candidate)
        key = normalized.lower()
        if not normalized or key in existing_set:
            continue
        memory_id = save_memory(
            db_name,
            normalized,
            author_id=author_id,
            source=source,
        )
        existing_set.add(key)
        saved.append(
            {
                "id": memory_id,
                "content": normalized,
                "source": source,
            }
        )
    return {"saved": saved, "error": llm_error}


def clear_session(db_name: str, session_id: str) -> int:
    return clear_history(db_name, session_id)


def available_dbs() -> list[str]:
    return list_available_dbs()


def create_db(db_name: str, password: str, guild_id: int) -> None:
    if not _DB_NAME_RE.fullmatch(db_name):
        raise ValueError("DB name must be 3-32 chars and use only letters, numbers, '_' or '-'")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")

    db_dir = _db_dir(db_name)
    db_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = db_dir / "config.json"
    if not cfg_path.exists():
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(_default_db_config(db_name), f, ensure_ascii=False, indent=2)

    init_db(db_name)
    register_db(db_name, password, guild_id)


def switch_guild_db(guild_id: int, db_name: str, password: str) -> None:
    if db_name not in available_dbs():
        raise ValueError(f"DB '{db_name}' not found")
    if not verify_db_password(db_name, password):
        raise ValueError("Invalid password")
    bind_guild_db(guild_id, db_name)


def remember(db_name: str, content: str, author_id: str = "", source: str = "manual") -> int:
    return save_memory(db_name, content, author_id=author_id, source=source)


def recent_memories(db_name: str, limit: int = 10) -> list[dict]:
    return list_memories(db_name, limit=limit)


def optimize_db(db_name: str) -> bool:
    return vacuum_db(db_name)


_CONSOLIDATE_MEMORIES_PROMPT = (
    "You reorganize a list of long-term memory entries for a chatbot.\n"
    "Given the current memory list, return a reorganized version that:\n"
    "- Splits overly dense entries (containing multiple facts) into atomic, single-fact entries\n"
    "- Merges near-duplicate or highly similar entries into one\n"
    "- Removes redundant information\n"
    "- Keeps each entry as a short, standalone sentence in Japanese\n"
    "- Preserves all distinct facts — do not lose information\n"
    "Output format: [\"memory 1\", \"memory 2\", ...]\n"
    "Return JSON array only, no explanation."
)


async def consolidate_memories(db_name: str, author_id: str = "") -> dict:
    all_memories = get_all_memories(db_name)
    if not all_memories:
        return {"before": 0, "after": 0, "entries": []}

    lines = [f"{i + 1}. {m['content']}" for i, m in enumerate(all_memories)]
    messages = [
        {"role": "system", "content": _CONSOLIDATE_MEMORIES_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]

    raw = await _llm.chat(messages)
    candidates = _parse_memory_candidates(raw, limit=0)
    normalized = [_normalize_memory_text(c) for c in candidates if c.strip()]
    if not normalized:
        return {"before": len(all_memories), "after": 0, "entries": []}

    new_ids = replace_all_memories(db_name, normalized, author_id=author_id, source="db_refresh")
    entries = [{"id": new_ids[i], "content": normalized[i]} for i in range(len(normalized))]
    return {"before": len(all_memories), "after": len(normalized), "entries": entries}


# ---------------------------------------------------------------------------
# RAG management helpers
# ---------------------------------------------------------------------------

_VALID_RAG_BACKENDS = ("chroma", "json")


def _load_db_config(db_name: str) -> dict:
    path = _db_dir(db_name) / "config.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_db_config(db_name: str, cfg: dict) -> None:
    path = _db_dir(db_name) / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def rag_enable(db_name: str) -> None:
    cfg = _load_db_config(db_name)
    cfg.setdefault("rag", {})["enabled"] = True
    _save_db_config(db_name, cfg)


def rag_disable(db_name: str) -> None:
    cfg = _load_db_config(db_name)
    cfg.setdefault("rag", {})["enabled"] = False
    _save_db_config(db_name, cfg)


def rag_set_backend(db_name: str, backend: str) -> None:
    if backend not in _VALID_RAG_BACKENDS:
        raise ValueError(f"backend は {_VALID_RAG_BACKENDS} のいずれかを指定してください")
    cfg = _load_db_config(db_name)
    cfg.setdefault("rag", {})["vector_backend"] = backend
    _save_db_config(db_name, cfg)


def rag_get_status(db_name: str) -> dict:
    from core.rag_manager import collection_stats
    return collection_stats(db_name)


def rag_ingest_text(db_name: str, text: str, source: str = "") -> int:
    """Ingest plain text into the RAG index. Returns number of chunks stored."""
    from core.rag_manager import ingest_text
    return ingest_text(db_name, text, source=source)


def rag_clear_documents(db_name: str) -> int:
    """Delete all documents from the RAG index. Returns count of deleted chunks."""
    from core.rag_manager import clear_collection
    return clear_collection(db_name)


def rag_delete_by_source(db_name: str, source: str) -> int:
    """Delete RAG chunks matching the given source name. Returns count of deleted chunks."""
    from core.rag_manager import delete_by_source
    return delete_by_source(db_name, source)


def rag_list_sources(db_name: str) -> list[str]:
    """Return sorted list of source names in the RAG index."""
    from core.rag_manager import list_sources
    return list_sources(db_name)


def memory_delete(db_name: str, memory_id: int) -> bool:
    """Delete a single memory entry by ID. Returns True if deleted."""
    return delete_memory(db_name, memory_id)


def notion_enable(db_name: str, api_key: str) -> None:
    cfg = _load_db_config(db_name)
    cfg.setdefault("notion", {})["enabled"] = True
    cfg["notion"]["api_key"] = api_key
    _save_db_config(db_name, cfg)


def notion_disable(db_name: str) -> None:
    cfg = _load_db_config(db_name)
    cfg.setdefault("notion", {})["enabled"] = False
    _save_db_config(db_name, cfg)


def notion_db_add(db_name: str, database_id: str) -> bool:
    """Add a Notion database ID. Returns False if already registered."""
    cfg = _load_db_config(db_name)
    notion_cfg = cfg.setdefault("notion", {})
    ids: list = notion_cfg.setdefault("database_ids", [])
    normalized = database_id.strip()
    if normalized in ids:
        return False
    ids.append(normalized)
    _save_db_config(db_name, cfg)
    return True


def notion_db_remove(db_name: str, database_id: str) -> bool:
    """Remove a Notion database ID. Returns False if not found."""
    cfg = _load_db_config(db_name)
    ids: list = cfg.get("notion", {}).get("database_ids", [])
    if database_id not in ids:
        return False
    ids.remove(database_id)
    _save_db_config(db_name, cfg)
    return True


def notion_get_status(db_name: str) -> dict:
    """Return Notion config and connectivity info for the given DB."""
    cfg = _load_db_config(db_name)
    notion_cfg = cfg.get("notion", {})
    from core import notion_client
    api_key = notion_client.get_api_key(notion_cfg)
    return {
        "enabled": notion_cfg.get("enabled", False),
        "has_api_key": bool(api_key),
        "database_ids": notion_cfg.get("database_ids", []),
        "pdf_property": notion_cfg.get("pdf_property", "PDF"),
        "max_results": notion_cfg.get("max_results", 5),
    }


async def notion_test_search(db_name: str, query: str) -> dict:
    """Run a test Notion search and return pages found (titles + page IDs).

    Returns dict with keys: pages (list of {title, id}), error (str or None).
    """
    cfg = _load_db_config(db_name)
    notion_cfg = cfg.get("notion", {})
    from core import notion_client
    api_key = notion_client.get_api_key(notion_cfg)
    if not api_key:
        return {"pages": [], "error": "APIキーが設定されていません"}

    database_ids = notion_cfg.get("database_ids", [])
    max_results = notion_cfg.get("max_results", 5)
    errors: list[str] = []
    queries = await _select_search_queries(query)
    pages = await _search_notion_pages(queries, database_ids, api_key, max_results, errors)
    if not pages and errors:
        return {"pages": [], "error": "\n".join(errors)}

    return {
        "pages": [
            {"title": notion_client.get_page_title(p), "id": p.get("id", "")}
            for p in pages
        ],
        "error": None,
    }

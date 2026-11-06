import hashlib
import json
import math
from pathlib import Path

import chromadb
import httpx

DB_BASE = Path(__file__).parent.parent / "databases"
CONFIG_BASE = Path(__file__).parent.parent / "config"

# Cache PersistentClient per db_name to avoid file-lock conflicts within one process
_clients: dict[str, chromadb.PersistentClient] = {}


def _load_llm_config() -> dict:
    path = CONFIG_BASE / "llm.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_rag_config(db_name: str) -> dict:
    path = DB_BASE / db_name / "config.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg.get("rag", {})


def _vector_db_path(db_name: str) -> Path:
    return DB_BASE / db_name / "vector_db"


def _json_vector_path(db_name: str) -> Path:
    return DB_BASE / db_name / "vector_db.json"


def _get_backend(db_name: str) -> str:
    """Return the configured vector backend: 'chroma' (default) or 'json'."""
    return _load_rag_config(db_name).get("vector_backend", "chroma")


# ---------------------------------------------------------------------------
# ChromaDB backend
# ---------------------------------------------------------------------------

def _chroma_get_collection(db_name: str) -> chromadb.Collection:
    if db_name not in _clients:
        path = _vector_db_path(db_name)
        path.mkdir(parents=True, exist_ok=True)
        _clients[db_name] = chromadb.PersistentClient(path=str(path))
    client = _clients[db_name]
    return client.get_or_create_collection(
        name=db_name,
        metadata={"hnsw:space": "cosine"},
    )


def _chroma_ingest(db_name: str, chunks: list[str], embeddings: list[list[float]], ids: list[str], metadatas: list[dict]) -> None:
    collection = _chroma_get_collection(db_name)
    collection.upsert(documents=chunks, embeddings=embeddings, ids=ids, metadatas=metadatas)


def _chroma_search(db_name: str, query_embedding: list[float], k: int, score_threshold: float) -> list[dict]:
    collection = _chroma_get_collection(db_name)
    count = collection.count()
    if count == 0:
        return []
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=min(k, count),
        include=["documents", "distances", "metadatas"],
    )
    docs = []
    for doc, dist, meta in zip(
        results["documents"][0],
        results["distances"][0],
        results["metadatas"][0],
    ):
        # ChromaDB cosine distance: 0=identical → similarity = 1 - distance
        similarity = max(0.0, 1.0 - dist)
        if similarity < score_threshold:
            continue
        docs.append({"content": doc, "source": meta.get("source", ""), "score": round(similarity, 4)})
    return docs


def _chroma_clear(db_name: str) -> int:
    collection = _chroma_get_collection(db_name)
    count = collection.count()
    if count > 0:
        all_ids = collection.get(include=[])["ids"]
        collection.delete(ids=all_ids)
    return count


def _chroma_delete_by_source(db_name: str, source: str) -> int:
    collection = _chroma_get_collection(db_name)
    results = collection.get(where={"source": source}, include=[])
    ids = results["ids"]
    if ids:
        collection.delete(ids=ids)
    return len(ids)


def _chroma_count(db_name: str) -> int:
    return _chroma_get_collection(db_name).count()


# ---------------------------------------------------------------------------
# JSON backend (pure-Python brute-force cosine similarity, no extra deps)
# ---------------------------------------------------------------------------

def _json_load(db_name: str) -> list[dict]:
    path = _json_vector_path(db_name)
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _json_save(db_name: str, records: list[dict]) -> None:
    path = _json_vector_path(db_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _json_ingest(db_name: str, chunks: list[str], embeddings: list[list[float]], ids: list[str], metadatas: list[dict]) -> None:
    records = _json_load(db_name)
    existing = {r["id"]: i for i, r in enumerate(records)}
    for chunk, emb, chunk_id, meta in zip(chunks, embeddings, ids, metadatas):
        entry = {"id": chunk_id, "text": chunk, "embedding": emb, "metadata": meta}
        if chunk_id in existing:
            records[existing[chunk_id]] = entry
        else:
            records.append(entry)
    _json_save(db_name, records)


def _json_search(db_name: str, query_embedding: list[float], k: int, score_threshold: float) -> list[dict]:
    records = _json_load(db_name)
    if not records:
        return []
    scored = []
    for r in records:
        sim = _cosine_similarity(query_embedding, r["embedding"])
        if sim >= score_threshold:
            scored.append((sim, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {"content": r["text"], "source": r["metadata"].get("source", ""), "score": round(sim, 4)}
        for sim, r in scored[:k]
    ]


def _json_clear(db_name: str) -> int:
    records = _json_load(db_name)
    count = len(records)
    if count > 0:
        _json_save(db_name, [])
    return count


def _json_delete_by_source(db_name: str, source: str) -> int:
    records = _json_load(db_name)
    remaining = [r for r in records if r["metadata"].get("source", "") != source]
    deleted = len(records) - len(remaining)
    if deleted > 0:
        _json_save(db_name, remaining)
    return deleted


def _json_count(db_name: str) -> int:
    return len(_json_load(db_name))


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _llm_headers(llm_cfg: dict) -> dict:
    api_key = llm_cfg.get("api_key", "")
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _embedding_model(rag_cfg: dict, llm_cfg: dict) -> str:
    return rag_cfg.get("embedding_model") or (
        "bge-m3" if llm_cfg.get("provider") == "ollama" else "text-embedding-bge-m3"
    )


def check_connection(db_name: str) -> None:
    """Check embedding availability and storage before reading channel history."""
    rag_cfg = _load_rag_config(db_name)
    llm_cfg = _load_llm_config()
    _embed(["RAG 接続確認"], _embedding_model(rag_cfg, llm_cfg),
           llm_cfg.get("base_url", "http://localhost:11434"))
    if _get_backend(db_name) == "json":
        _json_load(db_name)
    else:
        _chroma_count(db_name)


def _embed(texts: list[str], model: str, base_url: str) -> list[list[float]]:
    """Get embeddings from the configured provider."""
    llm_cfg = _load_llm_config()
    provider = llm_cfg.get("provider", "openai")
    headers = _llm_headers(llm_cfg)

    with httpx.Client(timeout=60) as client:
        if provider == "ollama":
            url = base_url.rstrip("/") + "/api/embed"
            resp = client.post(url, json={"model": model, "input": texts}, headers=headers)
            resp.raise_for_status()
            return resp.json()["embeddings"]

        if provider == "openai":
            url = base_url.rstrip("/") + "/v1/embeddings"
            resp = client.post(url, json={"model": model, "input": texts}, headers=headers)
            resp.raise_for_status()
            return [item["embedding"] for item in resp.json()["data"]]

        raise ValueError(f"Unknown provider for embeddings: {provider}")


def _chunk_text(text: str, chunk_size: int = 500, chunk_overlap: int = 50) -> list[str]:
    """Split text into overlapping character-count chunks with natural boundary detection."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        if end >= len(text):
            chunks.append(text[start:])
            break
        for sep in ("\n\n", "\n", "。", ".", " "):
            pos = text.rfind(sep, start + chunk_size // 2, end)
            if pos != -1:
                end = pos + len(sep)
                break
        chunks.append(text[start:end])
        start = end - chunk_overlap

    return [c.strip() for c in chunks if c.strip()]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ingest_text(
    db_name: str,
    text: str,
    source: str = "",
    metadata: dict | None = None,
) -> int:
    """Chunk text, embed, and upsert into the configured vector backend. Returns number of chunks stored."""
    rag_cfg = _load_rag_config(db_name)
    llm_cfg = _load_llm_config()

    embedding_model = _embedding_model(rag_cfg, llm_cfg)
    chunk_size = rag_cfg.get("chunk_size", 500)
    chunk_overlap = rag_cfg.get("chunk_overlap", 50)
    base_url = llm_cfg.get("base_url", "http://localhost:11434")
    backend = _get_backend(db_name)

    chunks = _chunk_text(text, chunk_size, chunk_overlap)
    if not chunks:
        return 0

    embeddings = _embed(chunks, embedding_model, base_url)
    if len(embeddings) != len(chunks) or any(not embedding for embedding in embeddings):
        raise ValueError("埋め込みの件数が本文と一致しないか、空のベクトルが返されました")

    ids = []
    metadatas = []
    for i, chunk in enumerate(chunks):
        chunk_id = hashlib.sha256(f"{source}:{i}:{chunk[:64]}".encode()).hexdigest()[:16]
        ids.append(chunk_id)
        meta: dict = {"source": source, "chunk_index": i}
        if metadata:
            meta.update(metadata)
        metadatas.append(meta)

    if backend == "json":
        _json_ingest(db_name, chunks, embeddings, ids, metadatas)
    else:
        _chroma_ingest(db_name, chunks, embeddings, ids, metadatas)

    return len(chunks)


def search(
    db_name: str,
    query: str,
    k: int = 4,
    score_threshold: float = 0.3,
) -> list[dict]:
    """Semantic similarity search. Returns list of {content, source, score} dicts."""
    rag_cfg = _load_rag_config(db_name)
    llm_cfg = _load_llm_config()

    embedding_model = _embedding_model(rag_cfg, llm_cfg)
    base_url = llm_cfg.get("base_url", "http://localhost:11434")
    backend = _get_backend(db_name)

    query_embedding = _embed([query], embedding_model, base_url)[0]

    if backend == "json":
        return _json_search(db_name, query_embedding, k, score_threshold)
    else:
        return _chroma_search(db_name, query_embedding, k, score_threshold)


def clear_collection(db_name: str) -> int:
    """Delete all documents from the collection. Returns count of deleted docs."""
    backend = _get_backend(db_name)
    if backend == "json":
        return _json_clear(db_name)
    return _chroma_clear(db_name)


def delete_by_source(db_name: str, source: str) -> int:
    """Delete all chunks whose source matches the given string. Returns count of deleted chunks."""
    backend = _get_backend(db_name)
    if backend == "json":
        return _json_delete_by_source(db_name, source)
    return _chroma_delete_by_source(db_name, source)


def list_sources(db_name: str) -> list[str]:
    """Return a sorted, deduplicated list of source names stored in the collection."""
    backend = _get_backend(db_name)
    if backend == "json":
        records = _json_load(db_name)
        return sorted({r["metadata"].get("source", "") for r in records if r["metadata"].get("source", "")})
    collection = _chroma_get_collection(db_name)
    if collection.count() == 0:
        return []
    results = collection.get(include=["metadatas"])
    return sorted({m.get("source", "") for m in results["metadatas"] if m.get("source", "")})


def collection_stats(db_name: str) -> dict:
    """Return document count and RAG enabled status for a DB."""
    try:
        backend = _get_backend(db_name)
        rag_cfg = _load_rag_config(db_name)
        if backend == "json":
            count = _json_count(db_name)
        else:
            count = _chroma_count(db_name)
        return {
            "document_count": count,
            "enabled": rag_cfg.get("enabled", False),
            "embedding_model": _embedding_model(rag_cfg, _load_llm_config()),
            "vector_backend": backend,
        }
    except Exception:
        return {"document_count": None, "enabled": False, "embedding_model": "", "vector_backend": "chroma",
                "error": "RAG保存先の状態を取得できませんでした"}

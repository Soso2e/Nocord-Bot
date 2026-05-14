"""Notion API integration: search databases, extract page text, download PDFs."""
import asyncio
import io
import os

import httpx

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
_PAGE_TEXT_DEFAULT_MAX_CHARS = 3000


def _headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _normalize_id(notion_id: str) -> str:
    """Normalize Notion ID by removing hyphens for consistent comparison."""
    return notion_id.replace("-", "")


def get_api_key(notion_cfg: dict) -> str:
    """Return API key from per-DB config or NOTION_API_KEY env var."""
    return notion_cfg.get("api_key") or os.environ.get("NOTION_API_KEY", "")


async def search_pages(
    query: str,
    database_ids: list[str],
    api_key: str,
    max_results: int = 5,
) -> list[dict]:
    """Search Notion for pages matching query, filtered to specified database IDs.

    Uses /v1/search which supports free-text queries across titles and content.
    """
    url = f"{NOTION_API_BASE}/search"
    payload = {
        "query": query,
        "filter": {"property": "object", "value": "page"},
        "sort": {"direction": "descending", "timestamp": "last_edited_time"},
        "page_size": min(max_results * 4, 50),
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload, headers=_headers(api_key))
        resp.raise_for_status()
        data = resp.json()

    results = data.get("results", [])

    if database_ids:
        normalized_db_ids = {_normalize_id(d) for d in database_ids}
        results = [
            p for p in results
            if _normalize_id(p.get("parent", {}).get("database_id", "")) in normalized_db_ids
        ]

    return results[:max_results]


def get_page_title(page: dict) -> str:
    """Extract plain-text title from a Notion page object."""
    props = page.get("properties", {})
    for prop_val in props.values():
        if prop_val.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in prop_val.get("title", []))
    return page.get("id", "")


def get_pdf_urls(page: dict, pdf_property: str = "PDF") -> list[str]:
    """Extract PDF file URLs from a Notion page's file-type property."""
    props = page.get("properties", {})
    prop = props.get(pdf_property, {})

    urls = []
    if prop.get("type") != "files":
        return urls

    for file_obj in prop.get("files", []):
        file_type = file_obj.get("type", "")
        if file_type == "file":
            url = file_obj.get("file", {}).get("url", "")
            if url:
                urls.append(url)
        elif file_type == "external":
            url = file_obj.get("external", {}).get("url", "")
            if url and url.lower().endswith(".pdf"):
                urls.append(url)

    return urls


def _extract_block_text(block: dict) -> str:
    block_type = block.get("type", "")
    if block_type not in {
        "paragraph", "heading_1", "heading_2", "heading_3",
        "bulleted_list_item", "numbered_list_item", "toggle",
        "quote", "callout", "code",
    }:
        return ""
    rich_text = block.get(block_type, {}).get("rich_text", [])
    return "".join(t.get("plain_text", "") for t in rich_text)


async def _fetch_blocks_text(
    block_id: str,
    api_key: str,
    depth: int = 0,
    max_chars: int = _PAGE_TEXT_DEFAULT_MAX_CHARS,
    _accumulated: list[int] | None = None,
) -> str:
    """Recursively fetch text from blocks, stopping when max_chars is reached."""
    if depth > 3:
        return ""

    acc = _accumulated if _accumulated is not None else [0]
    url = f"{NOTION_API_BASE}/blocks/{block_id}/children"
    parts: list[str] = []

    async with httpx.AsyncClient(timeout=30) as client:
        has_more = True
        cursor = None
        while has_more:
            params: dict = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor

            resp = await client.get(url, params=params, headers=_headers(api_key))
            resp.raise_for_status()
            data = resp.json()

            for block in data.get("results", []):
                if acc[0] >= max_chars:
                    return "\n".join(parts)

                text = _extract_block_text(block)
                if text:
                    parts.append(text)
                    acc[0] += len(text)

                if block.get("has_children") and acc[0] < max_chars:
                    child_text = await _fetch_blocks_text(
                        block["id"], api_key, depth + 1, max_chars, acc
                    )
                    if child_text:
                        parts.append(child_text)

            has_more = data.get("has_more", False)
            cursor = data.get("next_cursor")
            if not has_more:
                break

    return "\n".join(p for p in parts if p)


async def get_page_text(
    page_id: str,
    api_key: str,
    max_chars: int = _PAGE_TEXT_DEFAULT_MAX_CHARS,
) -> str:
    """Fetch and return plain-text content from a Notion page's blocks."""
    text = await _fetch_blocks_text(page_id, api_key, max_chars=max_chars)
    return text[:max_chars] if text else ""


async def download_pdf_text(url: str) -> str:
    """Download a PDF from the given URL and extract its text using pdfminer."""
    from pdfminer.high_level import extract_text as pdf_extract

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    pdf_bytes = io.BytesIO(resp.content)
    # pdfminer is synchronous/CPU-bound — offload to thread pool
    text = await asyncio.to_thread(pdf_extract, pdf_bytes)
    return text.strip() if text else ""

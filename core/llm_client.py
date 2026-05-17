import asyncio
import json
from pathlib import Path

import httpx

CONFIG_PATH = Path(__file__).parent.parent / "config" / "llm.json"


def _load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


class LLMClient:
    def __init__(self, profile: str | None = None):
        self._profile = profile
        self._cfg = self._load_profile_config()

    def _load_profile_config(self) -> dict:
        cfg = _load_config()
        if not self._profile:
            return cfg

        profile_cfg = cfg.get(self._profile, {})
        if self._profile == "query_llm" and not profile_cfg:
            profile_cfg = cfg.get("light_llm", {})
        if not isinstance(profile_cfg, dict) or not profile_cfg:
            return cfg

        merged = cfg.copy()
        merged.update(profile_cfg)
        for key in ("query_llm", "light_llm"):
            merged.pop(key, None)
        return merged

    def reload(self):
        self._cfg = self._load_profile_config()

    def _timeout(self) -> httpx.Timeout:
        raw = self._cfg.get("timeout", 120)
        if isinstance(raw, dict):
            return httpx.Timeout(
                timeout=raw.get("timeout", None),
                connect=raw.get("connect", 10.0),
                read=raw.get("read", 120.0),
                write=raw.get("write", 120.0),
                pool=raw.get("pool", 120.0),
            )

        total = float(raw)
        return httpx.Timeout(
            timeout=total,
            connect=float(self._cfg.get("connect_timeout", min(total, 10.0))),
            read=float(self._cfg.get("read_timeout", total)),
            write=float(self._cfg.get("write_timeout", total)),
            pool=float(self._cfg.get("pool_timeout", total)),
        )

    async def _post_json(self, url: str, payload: dict, headers: dict | None = None) -> dict:
        retries = max(0, int(self._cfg.get("max_retries", 1)))
        backoff = float(self._cfg.get("retry_backoff_seconds", 1.5))
        timeout = self._timeout()
        last_error: Exception | None = None

        for attempt in range(retries + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(url, json=payload, headers=headers or {})
                    resp.raise_for_status()
                    return resp.json()
            except httpx.TimeoutException as exc:
                last_error = exc
                if attempt >= retries:
                    raise RuntimeError(
                        f"LLM request timed out after {timeout.read}s while waiting for {url}"
                    ) from exc
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if attempt >= retries:
                    detail = exc.response.text.strip()
                    if len(detail) > 500:
                        detail = detail[:500] + "..."
                    raise RuntimeError(
                        f"LLM request failed for {url}: {exc}. Response body: {detail}"
                    ) from exc
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt >= retries:
                    raise RuntimeError(f"LLM request failed for {url}: {exc}") from exc

            await asyncio.sleep(backoff * (attempt + 1))

        raise RuntimeError(f"LLM request failed for {url}: {last_error}")

    async def chat(self, messages: list[dict]) -> str:
        provider = self._cfg.get("provider", "ollama")
        if provider == "ollama":
            return await self._chat_ollama(messages)
        elif provider == "openai":
            return await self._chat_openai(messages)
        else:
            raise ValueError(f"Unknown provider: {provider}")

    # ── Ollama ──────────────────────────────────────────────────────────────

    async def _chat_ollama(self, messages: list[dict]) -> str:
        url = self._cfg["base_url"].rstrip("/") + "/api/chat"
        payload = {
            "model": self._cfg["model"],
            "messages": messages,
            "stream": False,
            "options": self._cfg.get("options", {}),
        }
        data = await self._post_json(url, payload)
        return data["message"]["content"]

    # ── OpenAI-compatible ────────────────────────────────────────────────────

    async def _chat_openai(self, messages: list[dict]) -> str:
        url = self._cfg["base_url"].rstrip("/") + "/v1/chat/completions"
        headers = {}
        api_key = self._cfg.get("api_key", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        opts = self._cfg.get("options", {})
        payload = {
            "model": self._cfg["model"],
            "messages": messages,
            "temperature": opts.get("temperature", 0.7),
        }
        data = await self._post_json(url, payload, headers=headers)
        return data["choices"][0]["message"]["content"]

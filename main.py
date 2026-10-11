"""
PAI-Chatbot エントリポイント

Discord Bot / Slack Bot (Socket Mode) / HTTP API を同時起動する。
各インターフェースは config/app.json の enabled フラグで制御。

起動: python main.py
"""

import asyncio
import json
import os
import threading
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / '.env', encoding='utf-8-sig')

CFG_PATH = Path(__file__).parent / "config" / "app.json"


def _load_app_cfg() -> dict:
    from core.gateway_config import load_app_config
    return load_app_config()


# ── HTTP API (uvicorn, 別スレッド) ────────────────────────────────────────────

def _run_http(host: str, port: int):
    from interfaces.http_api import app
    uvicorn.run(app, host=host, port=port, log_level="info")


# ── Discord Bot (asyncio, 別スレッド) ─────────────────────────────────────────

def _run_discord(token: str):
    from core.discord_pdf_sync import install as install_discord_pdf_sync
    from interfaces import discord_bot

    install_discord_pdf_sync(discord_bot)
    discord_bot.bot.run(token)


# ── Slack Bot (asyncio) ───────────────────────────────────────────────────────

async def _run_slack():
    from interfaces.slack_bot import run as slack_run
    await slack_run()


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    cfg = _load_app_cfg()
    tasks = []
    threads = []

    if cfg.get("mcp", {}).get("enabled", False):
        from interfaces.mcp_server import run_http
        t = threading.Thread(target=run_http, args=(cfg["mcp"],), daemon=True)
        t.start()
        threads.append(t)
        print("[MCP] Read-only archive server starting")

    # HTTP API
    if cfg.get("http", {}).get("enabled", True):
        host = cfg["http"].get("host", "0.0.0.0")
        port = cfg["http"].get("port", 8000)
        t = threading.Thread(target=_run_http, args=(host, port), daemon=True)
        t.start()
        threads.append(t)
        print(f"[HTTP] API server starting on {host}:{port}")

    # Discord Bot
    if cfg.get("discord", {}).get("enabled", True):
        token = os.getenv("DISCORD_TOKEN")
        if not token:
            print("[Discord] DISCORD_TOKEN not set — skipping")
        else:
            t = threading.Thread(target=_run_discord, args=(token,), daemon=True)
            t.start()
            threads.append(t)
            print("[Discord] Bot starting...")

    # Slack Bot
    if cfg.get("slack", {}).get("enabled", True):
        bot_token = os.getenv("SLACK_BOT_TOKEN")
        app_token = os.getenv("SLACK_APP_TOKEN")
        if not bot_token or not app_token:
            print("[Slack] SLACK_BOT_TOKEN / SLACK_APP_TOKEN not set — skipping")
        else:
            tasks.append(asyncio.create_task(_run_slack()))
            print("[Slack] Bot starting (Socket Mode)...")

    await _monitor_interfaces(threads, tasks)


async def _monitor_interfaces(threads, tasks):
    # A failed Discord thread must not leave an apparently healthy idle process.
    # A nonzero process exit allows Task Scheduler to restart the application.
    try:
        while True:
            if any(not thread.is_alive() for thread in threads):
                raise RuntimeError('A Nocord interface stopped')
            for task in tasks:
                if task.done():
                    if not task.cancelled() and task.exception():
                        raise RuntimeError('A Nocord interface failed') from None
                    raise RuntimeError('A Nocord interface stopped')
            await asyncio.sleep(1)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    from core.process_lock import AlreadyRunning, ProcessLock
    try:
        with ProcessLock(Path(__file__).parent / 'data' / 'runtime' / 'bot.lock'):
            asyncio.run(main())
    except AlreadyRunning:
        print('Nocord is already running.')

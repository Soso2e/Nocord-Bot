import asyncio
import json
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from core import chat_controller
from core.db_registry import get_guild_db
from core.ingest_helpers import SUPPORTED_EXTENSIONS, fetch_url, read_bytes

_VALID_RAG_BACKENDS = ("chroma", "json")

_PREFIX = "!"
APP_CONFIG_PATH = Path(__file__).parent.parent / "config" / "app.json"
LLM_CONFIG_PATH = Path(__file__).parent.parent / "config" / "llm.json"
_MEMORY_TRIGGER_PHRASES = (
    "覚えておいて",
    "覚えといて",
    "remember this",
    "remember that",
    "save this to memory",
)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=_PREFIX, intents=intents)
_slash_synced = False
_background_tasks: set[asyncio.Task] = set()


def _load_app_config() -> dict:
    try:
        with open(APP_CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _load_llm_config() -> dict:
    with open(LLM_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _session(channel_id: int) -> str:
    return f"discord-{channel_id}"


def _default_db() -> str:
    return _load_app_config().get("default_db", "general")


def _db(guild_id: int | None) -> str:
    if guild_id is None:
        return _default_db()
    return get_guild_db(guild_id) or _default_db()


def _should_capture_memory(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in text or phrase in lowered for phrase in _MEMORY_TRIGGER_PHRASES)


def _llm_error_message(error: Exception | str) -> str:
    text = str(error)
    if "timed out" in text.lower():
        return "LLM API がタイムアウトしました。モデル応答が遅いため、`config/llm.json` の `read_timeout` または `timeout` を延ばしてください。"
    return f"LLM API 呼び出しに失敗しました: {text}"


class _DiscordProgressMessage:
    def __init__(self, send_message):
        self._send_message = send_message
        self._message = None
        self._lines: list[str] = []

    async def __call__(self, line: str) -> None:
        if not line or line in self._lines:
            return
        self._lines.append(line)
        content = "**検索状況**\n" + "\n".join(f"- {item}" for item in self._lines)
        if self._message is None:
            self._message = await self._send_message(content)
            return
        await self._message.edit(content=content)


def _track_background_task(task: asyncio.Task) -> None:
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _memory_capture_result_message(capture_result: dict) -> str:
    saved = capture_result["saved"]
    error = capture_result["error"]

    lines: list[str] = []
    if saved:
        lines.append("長期記憶に保存しました:")
        lines.extend(f"- #{item['id']} {item['content']}" for item in saved)
    if error:
        prefix = "メモリ抽出は一部失敗しました。" if saved else "メモリ抽出は失敗しました。"
        lines.append(prefix)
        lines.append(_llm_error_message(error))
    return "\n".join(lines)


async def _notify_memory_capture_result(
    capture_task: asyncio.Task,
    send_message,
) -> None:
    try:
        capture_result = await capture_task
    except Exception as exc:
        await send_message(f"メモリ抽出は失敗しました。\n{_llm_error_message(exc)}")
        return

    message = _memory_capture_result_message(capture_result)
    if message:
        await send_message(message)


async def _build_history_lines(
    channel: discord.abc.Messageable,
    limit: int = 40,
) -> list[str]:
    if not hasattr(channel, "history"):
        return []

    rows: list[str] = []
    async for item in channel.history(limit=limit, oldest_first=False):
        author = getattr(item.author, "display_name", None) or getattr(item.author, "name", "unknown")
        content = (item.content or "").strip()
        if not content:
            continue
        rows.append(f"[{item.author.id}|{author}]: {content}")
    rows.reverse()
    return rows


async def _capture_channel_memories(
    channel: discord.abc.Messageable,
    guild_id: int | None,
    author_id: str,
    source: str = "discord_capture",
    limit: int = 40,
) -> dict:
    history_lines = await _build_history_lines(channel, limit=limit)
    return await chat_controller.capture_memories_from_history(
        _db(guild_id),
        history_lines,
        author_id=author_id,
        source=source,
    )


def _require_guild(interaction: discord.Interaction) -> bool:
    return interaction.guild is not None and interaction.channel is not None


def _has_manage_guild(interaction: discord.Interaction) -> bool:
    permissions = getattr(interaction.user, "guild_permissions", None)
    return bool(permissions and permissions.manage_guild)


async def _send_permission_error(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "このコマンドを使用するには`サーバーの管理`権限が必要です。",
        ephemeral=True,
    )


def _status_embed(guild_id: int | None) -> discord.Embed:
    db_name = _db(guild_id)
    llm = _load_llm_config()
    embed = discord.Embed(title="PAI-Chatbot ステータス", color=0x5865F2)
    embed.add_field(name="DB", value=f"`{db_name}`", inline=True)
    embed.add_field(name="LLMプロバイダー", value=f"`{llm['provider']}`", inline=True)
    embed.add_field(name="モデル", value=f"`{llm['model']}`", inline=True)
    embed.add_field(name="エンドポイント", value=f"`{llm['base_url']}`", inline=False)
    return embed


async def _sync_slash_commands() -> None:
    global _slash_synced
    if _slash_synced:
        return

    cfg = _load_app_config().get("discord", {})
    guild_id = cfg.get("guild_id") or cfg.get("dev_guild_id")
    if guild_id:
        guild = discord.Object(id=int(guild_id))
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f"[Discord] Synced {len(synced)} slash commands to guild {guild_id}")
    else:
        synced = await bot.tree.sync()
        print(f"[Discord] Synced {len(synced)} global slash commands")

    _slash_synced = True


db_group = app_commands.Group(name="db", description="このDiscordサーバーのメモリDBを管理する")
memory_group = app_commands.Group(name="memory", description="このDiscordサーバーの長期記憶を管理する")
rag_group = app_commands.Group(name="rag", description="現在のDBのRAG（知識検索）を管理する")
notion_group = app_commands.Group(name="notion", description="Notion連携の確認・テストを行う")


@bot.event
async def on_ready():
    await _sync_slash_commands()
    print(f"[Discord] Logged in as {bot.user} (id={bot.user.id})")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if bot.user.mentioned_in(message) and not message.mention_everyone:
        text = message.content
        for mention in message.mentions:
            text = text.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
        text = text.strip()
        if text:
            capture_task = None
            if _should_capture_memory(text):
                capture_task = asyncio.create_task(
                    _capture_channel_memories(
                        message.channel,
                        message.guild.id if message.guild else None,
                        str(message.author.id),
                        source="discord_auto",
                    )
                )
            async with message.channel.typing():
                try:
                    progress = _DiscordProgressMessage(message.channel.send)
                    reply = await chat_controller.process(
                        text,
                        _session(message.channel.id),
                        _db(message.guild.id if message.guild else None),
                        progress=progress,
                    )
                except RuntimeError as exc:
                    if capture_task is not None:
                        capture_task.cancel()
                    await message.reply(_llm_error_message(exc))
                    return
            await message.reply(reply)
            if capture_task is not None:
                _track_background_task(
                    asyncio.create_task(
                        _notify_memory_capture_result(
                            capture_task,
                            lambda content: message.channel.send(content),
                        )
                    )
                )
            return

    await bot.process_commands(message)


@bot.command(name="chat")
async def cmd_chat(ctx: commands.Context, *, text: str):
    capture_task = None
    if _should_capture_memory(text):
        capture_task = asyncio.create_task(
            _capture_channel_memories(
                ctx.channel,
                ctx.guild.id if ctx.guild else None,
                str(ctx.author.id),
                source="discord_auto",
            )
        )
    async with ctx.typing():
        try:
            progress = _DiscordProgressMessage(ctx.send)
            reply = await chat_controller.process(
                text,
                _session(ctx.channel.id),
                _db(ctx.guild.id if ctx.guild else None),
                progress=progress,
            )
        except RuntimeError as exc:
            if capture_task is not None:
                capture_task.cancel()
            await ctx.reply(_llm_error_message(exc))
            return
    await ctx.reply(reply)
    if capture_task is not None:
        _track_background_task(
            asyncio.create_task(
                _notify_memory_capture_result(
                    capture_task,
                    lambda content: ctx.send(content),
                )
            )
        )


@bot.command(name="status")
async def cmd_status(ctx: commands.Context):
    await ctx.send(embed=_status_embed(ctx.guild.id if ctx.guild else None))


@bot.tree.command(name="chat", description="現在のチャンネルでボットとチャットする")
@app_commands.describe(text="ボットに回答させたいメッセージ")
async def slash_chat(interaction: discord.Interaction, text: str):
    if interaction.channel is None:
        await interaction.response.send_message("このコマンドはチャンネル内でのみ使用できます。", ephemeral=True)
        return

    capture_task = None
    if _should_capture_memory(text):
        capture_task = asyncio.create_task(
            _capture_channel_memories(
                interaction.channel,
                interaction.guild.id if interaction.guild else None,
                str(interaction.user.id),
                source="discord_auto",
            )
        )
    await interaction.response.defer(thinking=True)
    try:
        progress = _DiscordProgressMessage(
            lambda content: interaction.followup.send(content, wait=True)
        )
        reply = await chat_controller.process(
            text,
            _session(interaction.channel.id),
            _db(interaction.guild.id if interaction.guild else None),
            progress=progress,
        )
    except RuntimeError as exc:
        if capture_task is not None:
            capture_task.cancel()
        await interaction.followup.send(_llm_error_message(exc), ephemeral=True)
        return
    await interaction.followup.send(reply)
    if capture_task is not None:
        _track_background_task(
            asyncio.create_task(
                _notify_memory_capture_result(
                    capture_task,
                    lambda content: interaction.followup.send(content),
                )
            )
        )


@bot.tree.command(name="status", description="現在のDBとモデル設定を表示する")
async def slash_status(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=_status_embed(interaction.guild.id if interaction.guild else None),
        ephemeral=True,
    )


@db_group.command(name="list", description="このボットで使用できるDBの一覧を表示する")
async def db_list(interaction: discord.Interaction):
    current = _db(interaction.guild.id if interaction.guild else None)
    dbs = chat_controller.available_dbs()
    lines = [f"{'*' if d == current else '-'} `{d}`" for d in dbs]
    content = "**利用可能なDB**\n" + "\n".join(lines) if lines else "まだDBがありません。"
    await interaction.response.send_message(content, ephemeral=True)


@db_group.command(name="current", description="このDiscordサーバーが使用しているDBを表示する")
async def db_current(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"このサーバーは `{_db(interaction.guild.id if interaction.guild else None)}` を使用しています。",
        ephemeral=True,
    )


@db_group.command(name="create", description="新しいメモリDBを作成してこのDiscordサーバーに紐付ける")
@app_commands.describe(
    db_name="作成するDB名。英数字、'_'、'-' が使用可能",
    password="後でDBを切り替える際に必要なパスワード",
)
async def db_create(interaction: discord.Interaction, db_name: str, password: str):
    if not _require_guild(interaction):
        await interaction.response.send_message("このコマンドはDiscordサーバー内でのみ使用できます。", ephemeral=True)
        return
    if not _has_manage_guild(interaction):
        await _send_permission_error(interaction)
        return

    try:
        chat_controller.create_db(db_name, password, interaction.guild.id)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    await interaction.response.send_message(
        f"DB `{db_name}` を作成してこのサーバーに紐付けました。",
        ephemeral=True,
    )


@db_group.command(name="use", description="このDiscordサーバーを既存のメモリDBに切り替える")
@app_commands.describe(
    db_name="このサーバーで使用するDB名",
    password="対象DBのパスワード",
)
async def db_use(interaction: discord.Interaction, db_name: str, password: str):
    if not _require_guild(interaction):
        await interaction.response.send_message("このコマンドはDiscordサーバー内でのみ使用できます。", ephemeral=True)
        return
    if not _has_manage_guild(interaction):
        await _send_permission_error(interaction)
        return

    try:
        chat_controller.switch_guild_db(interaction.guild.id, db_name, password)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    await interaction.response.send_message(
        f"このサーバーは `{db_name}` を使用するようになりました。",
        ephemeral=True,
    )


class _RefreshConfirmView(discord.ui.View):
    def __init__(self, db_name: str, author_id: str):
        super().__init__(timeout=30)
        self._db_name = db_name
        self._author_id = author_id

    @discord.ui.button(label="実行する", style=discord.ButtonStyle.primary)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="長期記憶を再構成中...", view=None)
        result = await chat_controller.consolidate_memories(self._db_name, author_id=self._author_id)
        if result["after"] == 0:
            await interaction.edit_original_response(content="長期記憶が見つからなかったため、何もしませんでした。")
            self.stop()
            return
        lines = [f"`#{e['id']}` {e['content']}" for e in result["entries"]]
        header = (
            f"DB `{self._db_name}` の長期記憶を再構成しました。\n"
            f"{result['before']} 件 → {result['after']} 件\n\n"
        )
        body = header + "\n".join(lines)
        if len(body) > 1900:
            body = header + "\n".join(lines)[:1900 - len(header)] + "\n…（省略）"
        await interaction.edit_original_response(content=body)
        self.stop()

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="キャンセルしました。", view=None)
        self.stop()


@memory_group.command(name="optimize", description="長期記憶をAIで再構成し、重複・分散した情報を整理する")
async def memory_optimize(interaction: discord.Interaction):
    if not _require_guild(interaction):
        await interaction.response.send_message("このコマンドはDiscordサーバー内でのみ使用できます。", ephemeral=True)
        return
    if not _has_manage_guild(interaction):
        await _send_permission_error(interaction)
        return

    db_name = _db(interaction.guild.id)
    view = _RefreshConfirmView(db_name, str(interaction.user.id))
    await interaction.response.send_message(
        f"DB `{db_name}` の長期記憶をAIで再構成します。\n"
        "密度が高い記憶の分割・重複の統合が行われます。元の記憶は置き換えられます。",
        view=view,
        ephemeral=True,
    )


@memory_group.command(name="save", description="このサーバーのDBに長期記憶を保存する")
@app_commands.describe(text="ボットに覚えさせたい内容")
async def memory_save(interaction: discord.Interaction, text: str):
    if not _require_guild(interaction):
        await interaction.response.send_message("このコマンドはDiscordサーバー内でのみ使用できます。", ephemeral=True)
        return
    if not text.strip():
        await interaction.response.send_message("保存するテキストを入力してください。", ephemeral=True)
        return

    db_name = _db(interaction.guild.id)
    memory_id = chat_controller.remember(
        db_name,
        text.strip(),
        author_id=str(interaction.user.id),
        source="discord_manual",
    )
    await interaction.response.send_message(
        f"`{db_name}` に記憶 #{memory_id} を保存しました。",
        ephemeral=True,
    )


@memory_group.command(name="list", description="このサーバーに保存された最近の長期記憶を表示する")
async def memory_list(interaction: discord.Interaction):
    if not _require_guild(interaction):
        await interaction.response.send_message("このコマンドはDiscordサーバー内でのみ使用できます。", ephemeral=True)
        return

    db_name = _db(interaction.guild.id)
    items = chat_controller.recent_memories(db_name, limit=20)
    if not items:
        await interaction.response.send_message("まだ保存された記憶はありません。", ephemeral=True)
        return

    lines = [f"`#{item['id']}` {item['content']}" for item in items]
    body = "**長期記憶一覧**\n" + "\n".join(lines)
    if len(body) > 1900:
        body = body[:1900] + "\n…（省略）"
    await interaction.response.send_message(body, ephemeral=True)


@memory_group.command(name="trim", description="IDを指定して長期記憶を1件削除する")
@app_commands.describe(memory_id="削除する記憶のID（/memory list で確認できます）")
async def memory_trim(interaction: discord.Interaction, memory_id: int):
    if not _require_guild(interaction):
        await interaction.response.send_message("このコマンドはDiscordサーバー内でのみ使用できます。", ephemeral=True)
        return
    if not _has_manage_guild(interaction):
        await _send_permission_error(interaction)
        return

    db_name = _db(interaction.guild.id)
    deleted = chat_controller.memory_delete(db_name, memory_id)
    if deleted:
        await interaction.response.send_message(
            f"記憶 `#{memory_id}` を削除しました。",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"記憶 `#{memory_id}` が見つかりませんでした。",
            ephemeral=True,
        )


@memory_group.command(name="capture", description="このチャンネルの最近のメッセージから長期記憶を抽出して保存する")
@app_commands.describe(limit="調査する最近のメッセージ数（10〜100）")
async def memory_capture(interaction: discord.Interaction, limit: app_commands.Range[int, 10, 100] = 40):
    if not _require_guild(interaction):
        await interaction.response.send_message("このコマンドはDiscordサーバー内でのみ使用できます。", ephemeral=True)
        return
    if interaction.channel is None:
        await interaction.response.send_message("このコマンドはチャンネル内でのみ使用できます。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    capture_result = await _capture_channel_memories(
        interaction.channel,
        interaction.guild.id,
        str(interaction.user.id),
        source="discord_manual_capture",
        limit=limit,
    )
    saved = capture_result["saved"]
    if capture_result["error"]:
        message = "メモリ抽出は失敗しました。"
        if saved:
            lines = [f"`#{item['id']}` {item['content']}" for item in saved]
            message += "\nただし、ルールベースで抽出できた内容は保存しました:\n" + "\n".join(lines)
        message += f"\n\n詳細: {_llm_error_message(capture_result['error'])}"
        await interaction.followup.send(message, ephemeral=True)
        return
    if not saved:
        await interaction.followup.send("保存候補は見つかりませんでした。", ephemeral=True)
        return

    lines = [f"`#{item['id']}` {item['content']}" for item in saved]
    await interaction.followup.send(
        "長期記憶に保存しました:\n" + "\n".join(lines),
        ephemeral=True,
    )


@memory_group.command(name="clear", description="現在のチャンネルのチャット履歴を消去する")
async def memory_clear(interaction: discord.Interaction):
    if not _require_guild(interaction):
        await interaction.response.send_message("このコマンドはDiscordサーバー内でのみ使用できます。", ephemeral=True)
        return

    deleted = chat_controller.clear_session(
        _db(interaction.guild.id),
        _session(interaction.channel.id),
    )
    await interaction.response.send_message(
        f"このチャンネルのチャット履歴を消去しました。{deleted} 件のメッセージを削除しました。",
        ephemeral=True,
    )


class _IngestTextModal(discord.ui.Modal, title="テキストをRAGに追加"):
    source = discord.ui.TextInput(
        label="ソース名（タイトル）",
        placeholder="例: 社内マニュアル2024",
        max_length=100,
        required=True,
    )
    content = discord.ui.TextInput(
        label="テキスト内容",
        style=discord.TextStyle.paragraph,
        placeholder="ここにテキストを貼り付けてください（最大4000文字）",
        max_length=4000,
        required=True,
    )

    def __init__(self, db_name: str):
        super().__init__()
        self._db_name = db_name

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            count = await asyncio.to_thread(
                chat_controller.rag_ingest_text,
                self._db_name,
                self.content.value,
                self.source.value,
            )
        except Exception as exc:
            await interaction.followup.send(f"インジェスト中にエラーが発生しました: {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            f"DB `{self._db_name}` に **{count}** チャンクを追加しました。\nソース: `{self.source.value}`",
            ephemeral=True,
        )


class _RagClearConfirmView(discord.ui.View):
    def __init__(self, db_name: str):
        super().__init__(timeout=30)
        self._db_name = db_name

    @discord.ui.button(label="全削除する", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="削除中...", view=None)
        count = await asyncio.to_thread(chat_controller.rag_clear_documents, self._db_name)
        await interaction.edit_original_response(
            content=f"DB `{self._db_name}` の RAG インデックスから **{count}** チャンクを削除しました。"
        )
        self.stop()

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="キャンセルしました。", view=None)
        self.stop()


@rag_group.command(name="on", description="現在のDBのRAGを有効にする")
async def rag_on(interaction: discord.Interaction):
    if not _require_guild(interaction):
        await interaction.response.send_message("このコマンドはDiscordサーバー内でのみ使用できます。", ephemeral=True)
        return
    if not _has_manage_guild(interaction):
        await _send_permission_error(interaction)
        return

    db_name = _db(interaction.guild.id)
    chat_controller.rag_enable(db_name)
    await interaction.response.send_message(
        f"DB `{db_name}` の RAG を有効にしました。",
        ephemeral=True,
    )


@rag_group.command(name="off", description="現在のDBのRAGを無効にする")
async def rag_off(interaction: discord.Interaction):
    if not _require_guild(interaction):
        await interaction.response.send_message("このコマンドはDiscordサーバー内でのみ使用できます。", ephemeral=True)
        return
    if not _has_manage_guild(interaction):
        await _send_permission_error(interaction)
        return

    db_name = _db(interaction.guild.id)
    chat_controller.rag_disable(db_name)
    await interaction.response.send_message(
        f"DB `{db_name}` の RAG を無効にしました。",
        ephemeral=True,
    )


@rag_group.command(name="status", description="現在のDBのRAG状態（有効/無効・バックエンド・文書数）を表示する")
async def rag_status(interaction: discord.Interaction):
    if not _require_guild(interaction):
        await interaction.response.send_message("このコマンドはDiscordサーバー内でのみ使用できます。", ephemeral=True)
        return

    db_name = _db(interaction.guild.id)
    stats = chat_controller.rag_get_status(db_name)
    enabled_label = "有効" if stats.get("enabled") else "無効"
    embed = discord.Embed(title=f"RAG ステータス — `{db_name}`", color=0x57F287 if stats.get("enabled") else 0x99AAB5)
    embed.add_field(name="状態", value=enabled_label, inline=True)
    embed.add_field(name="バックエンド", value=f"`{stats.get('vector_backend', 'chroma')}`", inline=True)
    embed.add_field(name="文書数", value=str(stats.get("document_count", 0)), inline=True)
    embed.add_field(name="埋め込みモデル", value=f"`{stats.get('embedding_model', '-')}`", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@rag_group.command(name="backend", description="現在のDBのベクターDBバックエンドを切り替える（chroma / json）")
@app_commands.describe(backend="使用するバックエンド: chroma（高速・永続）または json（依存なし・小規模向け）")
@app_commands.choices(backend=[
    app_commands.Choice(name="chroma（ChromaDB・推奨）", value="chroma"),
    app_commands.Choice(name="json（純粋Python・依存なし）", value="json"),
])
async def rag_backend(interaction: discord.Interaction, backend: str):
    if not _require_guild(interaction):
        await interaction.response.send_message("このコマンドはDiscordサーバー内でのみ使用できます。", ephemeral=True)
        return
    if not _has_manage_guild(interaction):
        await _send_permission_error(interaction)
        return

    db_name = _db(interaction.guild.id)
    try:
        chat_controller.rag_set_backend(db_name, backend)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    await interaction.response.send_message(
        f"DB `{db_name}` のベクターDBバックエンドを `{backend}` に切り替えました。\n"
        "既存のインデックスはそのまま残ります。再インデックスが必要な場合は `scripts/ingest.py` を使用してください。",
        ephemeral=True,
    )


@rag_group.command(name="file", description="ファイルをアップロードしてRAGに追加する（.txt/.md/.pdf/.json、最大10MB）")
@app_commands.describe(
    file="追加するドキュメントファイル",
    source="ソース名（省略時はファイル名）",
)
async def rag_file(interaction: discord.Interaction, file: discord.Attachment, source: str = ""):
    if not _require_guild(interaction):
        await interaction.response.send_message("このコマンドはDiscordサーバー内でのみ使用できます。", ephemeral=True)
        return
    if not _has_manage_guild(interaction):
        await _send_permission_error(interaction)
        return

    suffix = "." + file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if suffix not in SUPPORTED_EXTENSIONS:
        await interaction.response.send_message(
            f"対応していないファイル形式です: `{suffix}`\n対応形式: {', '.join(sorted(SUPPORTED_EXTENSIONS))}",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        data = await file.read()
        text = await asyncio.to_thread(read_bytes, data, file.filename)
        db_name = _db(interaction.guild.id)
        label = source or file.filename
        count = await asyncio.to_thread(chat_controller.rag_ingest_text, db_name, text, label)
    except Exception as exc:
        await interaction.followup.send(f"インジェスト中にエラーが発生しました: {exc}", ephemeral=True)
        return

    await interaction.followup.send(
        f"DB `{db_name}` に `{file.filename}` を追加しました。\nチャンク数: **{count}**",
        ephemeral=True,
    )


@rag_group.command(name="url", description="URLからコンテンツを取得してRAGに追加する（Google Docs/Sheets/Webページ）")
@app_commands.describe(
    url="取得するURL（Google Docs共有リンクなど）",
    source="ソース名（省略時はURL）",
)
async def rag_url(interaction: discord.Interaction, url: str, source: str = ""):
    if not _require_guild(interaction):
        await interaction.response.send_message("このコマンドはDiscordサーバー内でのみ使用できます。", ephemeral=True)
        return
    if not _has_manage_guild(interaction):
        await _send_permission_error(interaction)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        text = await asyncio.to_thread(fetch_url, url)
        db_name = _db(interaction.guild.id)
        label = source or url
        count = await asyncio.to_thread(chat_controller.rag_ingest_text, db_name, text, label)
    except RuntimeError as exc:
        await interaction.followup.send(f"URL取得に失敗しました: {exc}", ephemeral=True)
        return
    except Exception as exc:
        await interaction.followup.send(f"インジェスト中にエラーが発生しました: {exc}", ephemeral=True)
        return

    await interaction.followup.send(
        f"DB `{db_name}` に URL のコンテンツを追加しました。\nチャンク数: **{count}**\nソース: `{label}`",
        ephemeral=True,
    )


@rag_group.command(name="paste", description="テキストを直接貼り付けてRAGに追加する（最大4000文字）")
async def rag_paste(interaction: discord.Interaction):
    if not _require_guild(interaction):
        await interaction.response.send_message("このコマンドはDiscordサーバー内でのみ使用できます。", ephemeral=True)
        return
    if not _has_manage_guild(interaction):
        await _send_permission_error(interaction)
        return

    db_name = _db(interaction.guild.id)
    await interaction.response.send_modal(_IngestTextModal(db_name))


@rag_group.command(name="sources", description="現在のDBのRAGインデックスに登録されているソース一覧を表示する")
async def rag_sources(interaction: discord.Interaction):
    if not _require_guild(interaction):
        await interaction.response.send_message("このコマンドはDiscordサーバー内でのみ使用できます。", ephemeral=True)
        return

    db_name = _db(interaction.guild.id)
    sources = chat_controller.rag_list_sources(db_name)
    if not sources:
        await interaction.response.send_message("RAGインデックスにソースがありません。", ephemeral=True)
        return

    lines = [f"- `{s}`" for s in sources]
    await interaction.response.send_message(
        f"**DB `{db_name}` のRAGソース一覧**\n" + "\n".join(lines),
        ephemeral=True,
    )


class _RagTrimConfirmView(discord.ui.View):
    def __init__(self, db_name: str, source: str):
        super().__init__(timeout=30)
        self._db_name = db_name
        self._source = source

    @discord.ui.button(label="削除する", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="削除中...", view=None)
        count = await asyncio.to_thread(
            chat_controller.rag_delete_by_source, self._db_name, self._source
        )
        await interaction.edit_original_response(
            content=f"ソース `{self._source}` の **{count}** チャンクを削除しました。"
        )
        self.stop()

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="キャンセルしました。", view=None)
        self.stop()


class _RagTrimSelectView(discord.ui.View):
    def __init__(self, db_name: str, sources: list[str]):
        super().__init__(timeout=60)
        self._db_name = db_name
        # Discord Select は最大25件
        options = [
            discord.SelectOption(label=s[:100], value=s[:100])
            for s in sources[:25]
        ]
        select = discord.ui.Select(
            placeholder="削除するソースを選んでください",
            min_values=1,
            max_values=1,
            options=options,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        source = interaction.data["values"][0]
        confirm_view = _RagTrimConfirmView(self._db_name, source)
        await interaction.response.edit_message(
            content=f"ソース `{source}` のチャンクを削除します。よろしいですか？",
            view=confirm_view,
        )
        self.stop()


@rag_group.command(name="trim", description="プルダウンからソースを選んでRAGチャンクを削除する")
async def rag_trim(interaction: discord.Interaction):
    if not _require_guild(interaction):
        await interaction.response.send_message("このコマンドはDiscordサーバー内でのみ使用できます。", ephemeral=True)
        return
    if not _has_manage_guild(interaction):
        await _send_permission_error(interaction)
        return

    db_name = _db(interaction.guild.id)
    sources = chat_controller.rag_list_sources(db_name)
    if not sources:
        await interaction.response.send_message("RAGインデックスにソースがありません。", ephemeral=True)
        return

    view = _RagTrimSelectView(db_name, sources)
    note = f"（25件を超えるため先頭25件を表示しています）" if len(sources) > 25 else ""
    await interaction.response.send_message(
        f"DB `{db_name}` から削除するソースを選んでください。{note}",
        view=view,
        ephemeral=True,
    )


@rag_group.command(name="clear", description="現在のDBのRAGインデックスを全削除する")
async def rag_clear(interaction: discord.Interaction):
    if not _require_guild(interaction):
        await interaction.response.send_message("このコマンドはDiscordサーバー内でのみ使用できます。", ephemeral=True)
        return
    if not _has_manage_guild(interaction):
        await _send_permission_error(interaction)
        return

    db_name = _db(interaction.guild.id)
    stats = chat_controller.rag_get_status(db_name)
    count = stats.get("document_count", 0)
    if count == 0:
        await interaction.response.send_message("RAGインデックスは空です。削除するものがありません。", ephemeral=True)
        return

    view = _RagClearConfirmView(db_name)
    await interaction.response.send_message(
        f"DB `{db_name}` の RAG インデックスにある **{count}** チャンクを全削除します。よろしいですか？",
        view=view,
        ephemeral=True,
    )


@notion_group.command(name="on", description="Notion連携を有効にする（APIキーも同時に設定）")
@app_commands.describe(api_key="Notion Integration Token（secret_xxx...）")
async def notion_on(interaction: discord.Interaction, api_key: str):
    if not _require_guild(interaction):
        await interaction.response.send_message("このコマンドはDiscordサーバー内でのみ使用できます。", ephemeral=True)
        return
    if not _has_manage_guild(interaction):
        await _send_permission_error(interaction)
        return
    if not api_key.startswith("secret_"):
        await interaction.response.send_message(
            "APIキーは `secret_` で始まるIntegration Tokenを指定してください。",
            ephemeral=True,
        )
        return

    db_name = _db(interaction.guild.id)
    chat_controller.notion_enable(db_name, api_key)
    await interaction.response.send_message(
        f"DB `{db_name}` のNotion連携を有効にしました。\n"
        "次に `/notion db add <データベースID>` でNotionのDBを登録してください。",
        ephemeral=True,
    )


@notion_group.command(name="off", description="Notion連携を無効にする")
async def notion_off(interaction: discord.Interaction):
    if not _require_guild(interaction):
        await interaction.response.send_message("このコマンドはDiscordサーバー内でのみ使用できます。", ephemeral=True)
        return
    if not _has_manage_guild(interaction):
        await _send_permission_error(interaction)
        return

    db_name = _db(interaction.guild.id)
    chat_controller.notion_disable(db_name)
    await interaction.response.send_message(
        f"DB `{db_name}` のNotion連携を無効にしました。",
        ephemeral=True,
    )


notion_db_group = app_commands.Group(name="db", description="連携するNotionデータベースIDを管理する", parent=notion_group)


@notion_db_group.command(name="add", description="連携するNotionデータベースIDを追加する")
@app_commands.describe(database_id="NotionのデータベースID（UUID形式）")
async def notion_db_add(interaction: discord.Interaction, database_id: str):
    if not _require_guild(interaction):
        await interaction.response.send_message("このコマンドはDiscordサーバー内でのみ使用できます。", ephemeral=True)
        return
    if not _has_manage_guild(interaction):
        await _send_permission_error(interaction)
        return

    db_name = _db(interaction.guild.id)
    added = chat_controller.notion_db_add(db_name, database_id.strip())
    if not added:
        await interaction.response.send_message(
            f"`{database_id}` はすでに登録されています。",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        f"NotionデータベースID `{database_id}` を登録しました。\n"
        "`/notion search <キーワード>` で検索できるか確認できます。",
        ephemeral=True,
    )


@notion_db_group.command(name="remove", description="登録済みのNotionデータベースIDを削除する")
async def notion_db_remove(interaction: discord.Interaction):
    if not _require_guild(interaction):
        await interaction.response.send_message("このコマンドはDiscordサーバー内でのみ使用できます。", ephemeral=True)
        return
    if not _has_manage_guild(interaction):
        await _send_permission_error(interaction)
        return

    db_name = _db(interaction.guild.id)
    info = chat_controller.notion_get_status(db_name)
    ids = info["database_ids"]
    if not ids:
        await interaction.response.send_message("登録済みのNotionデータベースIDがありません。", ephemeral=True)
        return

    options = [discord.SelectOption(label=d[:100], value=d[:100]) for d in ids[:25]]
    select = discord.ui.Select(placeholder="削除するデータベースIDを選んでください", options=options)

    async def on_select(select_interaction: discord.Interaction):
        chosen = select_interaction.data["values"][0]
        chat_controller.notion_db_remove(db_name, chosen)
        await select_interaction.response.edit_message(
            content=f"NotionデータベースID `{chosen}` を削除しました。",
            view=None,
        )

    select.callback = on_select
    view = discord.ui.View(timeout=60)
    view.add_item(select)
    await interaction.response.send_message(
        f"DB `{db_name}` から削除するNotionデータベースIDを選んでください。",
        view=view,
        ephemeral=True,
    )


@notion_group.command(name="status", description="現在のDBのNotion連携設定とAPI疎通を確認する")
async def notion_status(interaction: discord.Interaction):
    if not _require_guild(interaction):
        await interaction.response.send_message("このコマンドはDiscordサーバー内でのみ使用できます。", ephemeral=True)
        return

    db_name = _db(interaction.guild.id)
    info = chat_controller.notion_get_status(db_name)

    enabled_label = "有効" if info["enabled"] else "無効"
    key_label = "あり" if info["has_api_key"] else "**なし（未設定）**"
    db_ids = info["database_ids"]
    db_ids_label = "\n".join(f"- `{d}`" for d in db_ids) if db_ids else "（なし）"

    color = 0x57F287 if info["enabled"] and info["has_api_key"] else 0x99AAB5
    embed = discord.Embed(title=f"Notion ステータス — `{db_name}`", color=color)
    embed.add_field(name="連携", value=enabled_label, inline=True)
    embed.add_field(name="APIキー", value=key_label, inline=True)
    embed.add_field(name="PDFプロパティ", value=f"`{info['pdf_property']}`", inline=True)
    embed.add_field(name="最大取得件数", value=str(info["max_results"]), inline=True)
    embed.add_field(name="対象データベースID", value=db_ids_label, inline=False)

    if not info["enabled"]:
        embed.set_footer(text="Notionを有効にするには config.json の notion.enabled を true にしてください。")
    elif not info["has_api_key"]:
        embed.set_footer(text="NOTION_API_KEY 環境変数または config.json の notion.api_key を設定してください。")

    await interaction.response.send_message(embed=embed, ephemeral=True)


@notion_group.command(name="search", description="Notionを実際に検索してボットに何が見えているか確認する")
@app_commands.describe(query="Notionで検索するキーワード")
async def notion_search(interaction: discord.Interaction, query: str):
    if not _require_guild(interaction):
        await interaction.response.send_message("このコマンドはDiscordサーバー内でのみ使用できます。", ephemeral=True)
        return

    db_name = _db(interaction.guild.id)
    info = chat_controller.notion_get_status(db_name)

    if not info["enabled"]:
        await interaction.response.send_message(
            "Notion連携が無効です。`/notion status` で設定を確認してください。",
            ephemeral=True,
        )
        return
    if not info["has_api_key"]:
        await interaction.response.send_message(
            "Notion APIキーが設定されていません。`/notion status` で設定を確認してください。",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    result = await chat_controller.notion_test_search(db_name, query)

    if result["error"]:
        await interaction.followup.send(
            f"Notion検索でエラーが発生しました:\n```\n{result['error']}\n```",
            ephemeral=True,
        )
        return

    pages = result["pages"]
    if not pages:
        await interaction.followup.send(
            f"「{query}」に一致するNotionページが見つかりませんでした。\n"
            "（データベースIDの設定やページの公開設定を確認してください）",
            ephemeral=True,
        )
        return

    lines = [f"`{i + 1}.` {p['title'] or '（タイトルなし）'}  `{p['id']}`" for i, p in enumerate(pages)]
    await interaction.followup.send(
        f"**Notion検索結果** — 「{query}」（DB: `{db_name}`）\n" + "\n".join(lines),
        ephemeral=True,
    )


bot.tree.add_command(db_group)
bot.tree.add_command(memory_group)
bot.tree.add_command(rag_group)
bot.tree.add_command(notion_group)


def run(token: str):
    bot.run(token)

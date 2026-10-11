"""Compact slash-command surface backed by the existing operation handlers."""
from typing import Literal

import discord
from discord import app_commands

from interfaces import archive_commands


async def validate(interaction, allowed, **options):
    unused = [name for name, value in options.items() if value is not None and name not in allowed]
    if unused:
        await interaction.response.send_message(
            'この操作では指定できないオプション: ' + ', '.join(unused), ephemeral=True)
        return False
    return True


async def missing(interaction, *names):
    await interaction.response.send_message('必要なオプションを指定してください: ' + ', '.join(names), ephemeral=True)


def install(tree, handlers):
    """Expose thirteen commands; never register the old aliases."""
    async def call(name, interaction, *args, **kwargs):
        await handlers[name](interaction, *args, **kwargs)

    @tree.command(name='status', description='Bot・DB・知識検索・連携の状態を確認する')
    async def status(interaction: discord.Interaction,
                     target: Literal['bot', 'db', 'rag', 'notion', 'archive'] = 'bot'):
        if target == 'archive':
            await archive_commands.list_registered(interaction)
        else:
            await call({'bot': 'slash_status', 'db': 'db_current', 'rag': 'rag_status',
                        'notion': 'notion_status'}[target], interaction)

    @tree.command(name='archive', description='ログの保存対象を登録・解除・確認する')
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(action='list=確認 / register=登録 / unregister=解除',
                           scope='channel=現在または指定チャンネル / server=このサーバー全体')
    async def archive(interaction: discord.Interaction,
                      action: Literal['list', 'register', 'unregister'] = 'list',
                      scope: Literal['channel', 'server'] = 'channel',
                      channel: discord.TextChannel | discord.Thread | discord.ForumChannel | None = None):
        if action == 'list':
            if channel is not None or scope != 'channel':
                await interaction.response.send_message('一覧表示では scope・channel を指定しないでください。', ephemeral=True)
                return
            await archive_commands.list_registered(interaction)
        else:
            await archive_commands.change(interaction, channel, action == 'register', scope)

    @tree.command(name='db', description='利用可能なDBの確認・作成・切り替えを行う')
    @app_commands.describe(action='list=一覧 / create=作成 / use=切り替え')
    async def db(interaction: discord.Interaction,
                 action: Literal['list', 'create', 'use'] = 'list',
                 db_name: str | None = None, password: str | None = None):
        if not await validate(interaction, set() if action == 'list' else {'db_name', 'password'},
                              db_name=db_name, password=password):
            return
        if action == 'list':
            await call('db_list', interaction)
        elif not db_name or not password:
            await missing(interaction, 'db_name', 'password')
        else:
            await call('db_' + action, interaction, db_name, password)

    @tree.command(name='memory', description='長期記憶の保存・確認・抽出・整理・削除を行う')
    @app_commands.describe(action='save=保存 / list=一覧 / capture=抽出 / optimize=整理 / delete=1件削除 / clear=会話履歴消去',
                           memory_id='deleteで削除する記憶のID', limit='captureで調べる投稿数（省略時40）')
    async def memory(interaction: discord.Interaction,
                     action: Literal['list', 'save', 'capture', 'optimize', 'delete', 'clear'] = 'list',
                     text: str | None = None, memory_id: int | None = None,
                     limit: app_commands.Range[int, 10, 100] | None = None):
        allowed = {'save': {'text'}, 'delete': {'memory_id'}, 'capture': {'limit'}}.get(action, set())
        if not await validate(interaction, allowed, text=text, memory_id=memory_id, limit=limit):
            return
        if action == 'save':
            if text is None:
                await missing(interaction, 'text')
            else:
                await call('memory_save', interaction, text)
        elif action == 'delete':
            if memory_id is None:
                await missing(interaction, 'memory_id')
            else:
                await call('memory_trim', interaction, memory_id)
        elif action == 'capture':
            await call('memory_capture', interaction, 40 if limit is None else limit)
        else:
            await call('memory_' + action, interaction)

    rag = app_commands.Group(name='rag', description='知識検索の設定・追加・同期・削除')

    @rag.command(name='config', description='知識検索の状態確認・有効化・保存方式・自動同期を設定する')
    @app_commands.describe(action='status=確認 / enable=有効 / disable=無効 / backend=保存方式 / auto_sync=自動同期')
    async def rag_config(interaction: discord.Interaction,
                         action: Literal['status', 'enable', 'disable', 'backend', 'auto_sync'] = 'status',
                         backend: Literal['chroma', 'json'] | None = None, enabled: bool | None = None):
        allowed = {'backend': {'backend'}, 'auto_sync': {'enabled'}}.get(action, set())
        if not await validate(interaction, allowed, backend=backend, enabled=enabled):
            return
        if action == 'backend':
            if backend is None:
                await missing(interaction, 'backend')
            else:
                await call('rag_backend', interaction, backend)
        elif action == 'auto_sync':
            if enabled is None:
                await missing(interaction, 'enabled')
            else:
                await call('rag_auto_sync', interaction, enabled)
        else:
            await call({'status': 'rag_status', 'enable': 'rag_on', 'disable': 'rag_off'}[action], interaction)

    @rag.command(name='add', description='ファイル・URL・入力フォームから知識を追加する')
    @app_commands.describe(file='追加するファイル', url='取得するURL', source='ソース名。入力フォームではフォーム内で指定')
    async def rag_add(interaction: discord.Interaction, file: discord.Attachment | None = None,
                      url: str | None = None, source: str | None = None):
        if file is not None and url is not None:
            await interaction.response.send_message('file と url はどちらか1つを指定してください。', ephemeral=True)
        elif file is not None:
            await call('rag_file', interaction, file, source or '')
        elif url is not None:
            await call('rag_url', interaction, url, source or '')
        elif source is not None:
            await interaction.response.send_message('ソース名は入力フォーム内で指定してください。', ephemeral=True)
        else:
            await call('rag_paste', interaction)

    @rag.command(name='sync', description='サーバーの投稿またはチャンネル内の資料を同期する')
    @app_commands.describe(target='server=サーバーの投稿 / channel=チャンネル内のPDF・Docs',
                           mode='サーバー同期: all=全投稿 / pinned=ピン留め / threads=スレッド',
                           limit='取得上限（0=無制限。省略時serverは500、channelは1000）')
    async def rag_sync(interaction: discord.Interaction,
                       target: Literal['server', 'channel'] = 'server',
                       channel: discord.TextChannel | None = None,
                       mode: Literal['all', 'pinned', 'threads'] | None = None,
                       limit: app_commands.Range[int, 0] | None = None, incremental: bool | None = None):
        if target == 'server':
            if not await validate(interaction, set(), channel=channel):
                return
            await call('rag_sync_server', interaction, mode or 'all', 500 if limit is None else limit,
                       True if incremental is None else incremental)
        else:
            if not await validate(interaction, set(), mode=mode, incremental=incremental):
                return
            await call('rag_scan_channel', interaction, channel, 1000 if limit is None else limit)

    @rag.command(name='sources', description='登録済みの知識ソースを表示する')
    async def rag_sources(interaction: discord.Interaction):
        await call('rag_sources', interaction)

    @rag.command(name='delete', description='知識ソースを選んで削除、または全削除する')
    @app_commands.describe(scope='source=ソースを選択 / all=確認画面から全削除')
    async def rag_delete(interaction: discord.Interaction, scope: Literal['source', 'all'] = 'source'):
        await call('rag_clear' if scope == 'all' else 'rag_trim', interaction)

    notion = app_commands.Group(name='notion', description='Notion連携の設定・DB管理・検索')

    @notion.command(name='config', description='Notion連携の状態確認・有効化・無効化')
    async def notion_config(interaction: discord.Interaction,
                            action: Literal['status', 'enable', 'disable'] = 'status',
                            api_key: str | None = None):
        if not await validate(interaction, {'api_key'} if action == 'enable' else set(), api_key=api_key):
            return
        if action == 'enable':
            if not api_key:
                await missing(interaction, 'api_key')
            else:
                await call('notion_on', interaction, api_key)
        else:
            await call('notion_status' if action == 'status' else 'notion_off', interaction)

    @notion.command(name='db', description='連携するNotionデータベースを追加・解除する')
    async def notion_db(interaction: discord.Interaction, action: Literal['add', 'remove'],
                        database_id: str | None = None):
        if not await validate(interaction, {'database_id'} if action == 'add' else set(), database_id=database_id):
            return
        if action == 'add':
            if not database_id:
                await missing(interaction, 'database_id')
            else:
                await call('notion_db_add', interaction, database_id)
        else:
            await call('notion_db_remove', interaction)

    @notion.command(name='search', description='Notionを検索して連携を確認する')
    async def notion_search(interaction: discord.Interaction, query: str):
        await call('notion_search', interaction, query)

    tree.add_command(rag)
    tree.add_command(notion)

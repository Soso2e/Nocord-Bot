# Nocord - Bot

`Server/Nocord`（NotionタスクをDiscordへWebhook通知する別ツール）とは別プロジェクトです。
このリポジトリ／フォルダー `Server/Nocord_bot` が、Discord Q&A・ログ保存・Knowledge Gateway MCPを担当します。
Discord BotのTokenは **このフォルダーの `.env`** の `DISCORD_TOKEN` に設定してください。

Knowledge Gateway / Discord Archive MCP の設定・起動・移行・バックアップは
[運用ガイド](docs/knowledge-gateway.md) を参照してください。
Archiveは初期設定では無効で、明示Allowlistを設定したTrust Zoneだけで収集します。

### Discord Archiveを試す（Windows）

初回だけ `config/personal.example.json` を `config/personal.local.json` にコピーし、
`.env` に `DISCORD_TOKEN` と `NOCORD_CONFIG=config/personal.local.json` を設定します。
依存関係を導入した後、`.venv\Scripts\python.exe main.py` で起動してください。

サーバー管理権限を持つユーザーがDiscordで次のコマンドを実行すると、IDを手入力せず保存対象を設定できます。

| コマンド | 使い方 |
|---|---|
| `/archive action:register` | 今いるチャンネル／threadを保存対象に登録 |
| `/archive action:register channel:対象` | チャンネルを選択して登録 |
| `/archive action:register scope:server` | このサーバーの閲覧・履歴取得可能なテキスト／フォーラムを一括登録 |
| `/archive action:unregister` | 今いる場所の保存許可を外す（既存原文は物理削除しない） |
| `/archive action:unregister scope:server` | このサーバーの保存許可を一括解除（個別登録したthreadを含む） |
| `/archive` | このサーバーの登録済み保存対象を表示 |
| `/rag sync` | 登録対象の過去ログを、設定済みBackfill範囲で同期 |

初期Allowlistは空で、登録前のチャンネルは収集しません。公開threadは親チャンネルの許可を継承します。
private threadはそのthread内で別途登録してください。登録設定はZone別のローカルファイルへ保存し、Git管理しません。
保存対象の管理は `/archive` に統合し、`action` で一覧・登録・解除を選択します。
`scope:server` は実行したサーバーの現在のチャンネルを対象にします。追加チャンネルは再実行して登録してください。
個別解除済みの公開threadは一括登録後も対象外です。`scope:server` と `channel` は同時指定できません。
Bot再起動後にコマンドが登録されます。グローバルコマンドはDiscord側への反映に時間がかかる場合があります。
保存済みログについて質問するには `@Bot 質問` または `/chat text:質問` を使います（回答生成はLM Studioが必要）。

### Discordコマンド一覧（13個）

操作は `action`、対象は `target` / `scope` で選びます。旧サブコマンドは登録しません。
Bot再起動時のコマンド同期後に新しい構成が反映されます。

| コマンド | 操作・オプション |
|---|---|
| `/chat` | `text` を指定して質問 |
| `/status` | `target`: bot（既定）/ db / rag / notion / archive |
| `/archive` | `action`: list（既定）/ register / unregister。`scope`: channel（既定）/ server。`channel` は任意 |
| `/db` | `action`: list（既定）/ create / use。create・use は `db_name` と `password` が必須 |
| `/memory` | `action`: list（既定）/ save / capture / optimize / delete / clear。save は `text`、delete は `memory_id` が必須。capture は `limit` 任意（10〜100、既定40）。clear は現在のチャンネルのBot会話履歴を消去 |
| `/rag config` | `action`: status（既定）/ enable / disable / backend / auto_sync。backend は `backend:chroma` または `json`、auto_sync は `enabled` が必須 |
| `/rag add` | `file` または `url` を指定。省略するとテキスト入力フォーム。file・url は `source` 任意 |
| `/rag sync` | `target`: server（既定）/ channel。server は `mode`（all / pinned / threads）、`incremental` 任意。channel は `channel` 任意。`limit` は両方に使用可能（0=無制限、既定server=500・channel=1000） |
| `/rag sources` | 知識ソースの一覧 |
| `/rag delete` | `scope`: source（既定、選択画面）/ all（全削除の確認画面） |
| `/notion config` | `action`: status（既定）/ enable / disable。enable は `api_key` が必須 |
| `/notion db` | `action`: add / remove。add は `database_id` が必須。remove は選択画面 |
| `/notion search` | `query` を指定して検索 |

例: サーバー全体の登録は `/archive action:register scope:server`、過去ログの同期は `/rag sync`。
現在のDBは `/status target:db`、知識検索の有効化は `/rag config action:enable` です。
管理操作の権限チェックと削除の確認画面は従来どおり適用されます。

### Windows自動起動

以下を一度実行すると、現在のWindowsユーザーのログオン時に非表示で起動するタスク `NocordBot` を登録します。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -StartNow
```

タスクは1分間隔の障害再起動、実行時間制限なし、二重起動防止を設定します。
起動ログは `data/runtime/nocord.log`（1MB × 最大4ファイル、環境変数のToken等を伏せて出力）です。
Token未設定・誤Token・Message Content Intent未設定の場合は接続できないので、設定を修正してください。
PCがスリープ／停止中、ログオン前、ユーザーがログアウトした後は動作しません。

```powershell
Get-ScheduledTask -TaskName NocordBot       # タスクの状態
Get-ScheduledTaskInfo -TaskName NocordBot   # 前回実行結果（0:正常、1:起動失敗）
Start-ScheduledTask -TaskName NocordBot     # 手動開始
Stop-ScheduledTask -TaskName NocordBot      # 現在の実行を停止
Disable-ScheduledTask -TaskName NocordBot   # 自動起動を無効にする
Enable-ScheduledTask -TaskName NocordBot    # 自動起動を再び有効にする
```

起動ファイル・作業ディレクトリは登録時のcheckoutに固定されます。フォルダー移動後は登録スクリプトを再実行してください。
手動起動の `python main.py` にも同じプロセスロックが適用され、自動起動と重複しません。

Discord / Slack / HTTP API に対応したマルチインターフェース AI チャットボット。  
LM Studio の OpenAI 互換 API をバックエンドに、RAG（検索拡張生成）ベースの専属 Q&A エージェントとして利用できます。

---

## 目次

1. [セットアップ](#1-セットアップ)
2. [起動](#2-起動)
3. [データベース（DB）の概念](#3-データベースdbの概念)
4. [RAG — ドキュメントを知識として投入する](#4-rag--ドキュメントを知識として投入する)
5. [Discord コマンド一覧](#5-discord-コマンド一覧)
6. [Slack コマンド一覧](#6-slack-コマンド一覧)
7. [HTTP API](#7-http-api)
8. [設定ファイル](#8-設定ファイル)
9. [Discord Bot の招待設定](#9-discord-bot-の招待設定)

---

## 1. セットアップ

### 必要なもの

- Python 3.11+
- [LM Studio](https://lmstudio.ai/) のローカルサーバーが起動済みで、チャットモデルと埋め込みモデルが利用可能なこと

```bash
# 例:
# - Chat model: qwen3.6:27b
# - Embedding model: bge-m3
# LM Studio でロードし、OpenAI-compatible server を起動する
```

### インストール

```bash
git clone https://github.com/Soso2e/PAI-chatbot.git
cd PAI-chatbot
pip install -r requirements.txt
```

### 環境変数

`.env.example` をコピーして編集します。

```bash
cp .env.example .env
```

```env
DISCORD_TOKEN=your_discord_bot_token_here
SLACK_BOT_TOKEN=xoxb-your-slack-bot-token
SLACK_APP_TOKEN=xapp-your-slack-app-token
HTTP_API_KEY=your_http_api_key_here   # 省略可（省略すると認証なし）
```

---

## 2. 起動

```bash
python main.py
```

`config/app.json` で有効にしたインターフェース（Discord / Slack / HTTP）が同時に起動します。

---

## 3. データベース（DB）の概念

「DB」はボットの**人格・知識・記憶をまとめた単位**です。  
DB ごとにシステムプロンプト・長期記憶・RAG 知識ベースが独立しています。

```
databases/
├── general/          ← デフォルト DB
│   ├── config.json   ← 人格・RAG設定
│   ├── memory.sqlite ← 長期記憶・会話履歴
│   └── vector_db/    ← RAG 知識ベース（ChromaDB）
└── nisolog/          ← 別人格の例
    └── ...
```

Discord では `/db action:create` で新しい DB を作り、`/db action:use` で切り替えます。

---

## 4. RAG — ドキュメントを知識として投入する

RAG を使うと、マニュアルや FAQ などのドキュメントを知識として登録し、  
ボットがその内容をもとに回答するようになります。

### ステップ 1: ドキュメントを投入する

```bash
# 単一ファイル
python scripts/ingest.py --db general --file docs/manual.pdf

# ディレクトリ内を一括投入（txt / md / pdf / json に対応）
python scripts/ingest.py --db general --dir docs/

# 投入状況を確認
python scripts/ingest.py --db general --stats
```

### ステップ 2: DB の RAG を有効にする

`databases/general/config.json` を編集します。

```json
{
  "rag": {
    "enabled": true
  }
}
```

これだけで次の会話から RAG が有効になります。

### RAG が有効なときの動作

- 質問の意味に近いドキュメント片（チャンク）を自動で検索
- システムプロンプトに「知識ベースにない情報は"含まれていません"と答える」制約を自動付加
- プロンプト優先順位: **知識ベース（RAG）> 長期記憶 > 会話履歴**

### RAG 設定パラメータ（config.json）

| キー | デフォルト | 説明 |
|---|---|---|
| `enabled` | `false` | RAG の有効/無効 |
| `embedding_model` | `"bge-m3"` | LM Studio でロードした埋め込みモデル名 |
| `chunk_size` | `500` | チャンク分割サイズ（文字数） |
| `chunk_overlap` | `50` | チャンク間のオーバーラップ（文字数） |
| `retrieval_k` | `4` | 1回の検索で取得するチャンク数 |
| `score_threshold` | `0.3` | 類似度スコアの最低閾値（0〜1） |

### LM Studio 前提の補足

- `config/llm.json` の `provider` は `openai` を使います
- チャットは `{base_url}/v1/chat/completions`、RAG埋め込みは `{base_url}/v1/embeddings` を使います
- `rag.embedding_model` は LM Studio 側でロード済みの埋め込みモデル名と一致させてください

### ドキュメント管理コマンド

```bash
# 統計表示
python scripts/ingest.py --db general --stats

# 全チャンク削除（再投入したいとき）
python scripts/ingest.py --db general --clear

# ソースラベルを指定して投入
python scripts/ingest.py --db general --file manual.pdf --source "製品マニュアル v2.0"
```

---

## 5. Discord コマンド一覧

### チャット

| 方法 | 例 |
|---|---|
| メンション | `@ボット名 こんにちは` |
| スラッシュ | `/chat text:こんにちは` |
| プレフィックス | `!chat こんにちは` |

### DB 管理（Manage Guild 権限が必要）

| コマンド | 説明 |
|---|---|
| `/db` | 利用可能な DB 一覧 |
| `/status target:db` | 現在のサーバーの DB |
| `/db action:create db_name:xxx password:xxx` | 新しい DB を作成 |
| `/db action:use db_name:xxx password:xxx` | 既存の DB に切り替え |

### メモリ管理

| コマンド | 説明 |
|---|---|
| `/memory action:save text:xxx` | 手動でメモリを保存 |
| `/memory` | 最近のメモリを表示 |
| `/memory action:capture` | 会話履歴からメモリを自動抽出 |
| `/memory action:optimize` | メモリを整理・重複排除 |
| `/memory action:clear` | チャンネルの会話履歴をクリア |

---

## 6. Slack コマンド一覧

| コマンド | 説明 |
|---|---|
| `/pai <メッセージ>` | チャット |
| `/pai-db list` | DB 一覧 |
| `/pai-db <name>` | DB を切り替え |
| `/pai-status` | 現在の設定を表示 |
| `@ボット名 <メッセージ>` | スレッド内で返答 |

---

## 7. HTTP API

デフォルトポート: `8000`  
認証: `X-API-Key` ヘッダー（`.env` の `HTTP_API_KEY` が未設定の場合は認証なし）

### エンドポイント

#### `POST /chat`

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_key" \
  -d '{
    "message": "料金はいくらですか？",
    "session_id": "user-123",
    "db_name": "general"
  }'
```

```json
{
  "reply": "...",
  "db_used": "general",
  "session_id": "user-123"
}
```

#### `GET /db/list`

```bash
curl http://localhost:8000/db/list -H "X-API-Key: your_key"
```

#### `POST /db/switch`

```bash
curl -X POST http://localhost:8000/db/switch \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_key" \
  -d '{"session_id": "user-123", "db_name": "nisolog"}'
```

#### `GET /status`

```bash
curl http://localhost:8000/status -H "X-API-Key: your_key"
```

---

## 8. 設定ファイル

### `config/app.json` — インターフェース設定

```json
{
  "default_db": "general",
  "discord": { "enabled": true, "prefix": "!" },
  "http": { "enabled": true, "host": "0.0.0.0", "port": 8000 },
  "slack": { "enabled": true }
}
```

### `config/llm.json` — LLM プロバイダー設定

```json
{
  "provider": "openai",
  "base_url": "http://127.0.0.1:1234",
  "model": "qwen3.6:27b",
  "api_key": "lm-studio",
  "timeout": 120,
  "options": {}
}
```

LM Studio を使う場合は `provider: "openai"` を指定します。

### NVIDIA PAIR 経由で接続する場合

ボットを動かすPCでもPAIRを起動し、推論用PCとペアリングしてください。
PAIRの **Endpoints** に表示されるローカルURLを `base_url` に設定します
（LM Studio用の標準値は `http://127.0.0.1:1234`）。
このボットは `/v1/chat/completions` を付加するので、設定値に `/v1` は含めません。
`query_llm.base_url` も同じ接続先に変更してください。

別PCの `http://IP:1234` へ直接接続すると、PAIRは `403 loopback-only` を返します。
ローカルのPAIRがノード間の認証・転送を担当するので、ボットにmTLS証明書を設定する必要はありません。
設定変更後はボットを再起動してください。

```powershell
curl.exe --noproxy "*" http://127.0.0.1:1234/v1/models
```

一覧のモデルIDを `model` と `query_llm.model` に設定します。
対象ノードでエンジンを起動し、モデルを用意してください。
PAIRの **Overview → Jobs → Ran on / Running on** で実際の推論先を確認できます。
PAIRはリクエスト単位でノードを選び、複数PCのVRAMを合算しません。
詳しくは [NVIDIA公式ガイド](https://docs.nvidia.com/local-ai/nvpair/getting-started/) を参照してください。

### `databases/{db_name}/config.json` — DB（人格）設定

```json
{
  "name": "general",
  "system_prompt": "あなたは親切なアシスタントです。",
  "memory_policy": {
    "auto_save": true,
    "max_context_messages": 20
  },
  "rag": {
    "enabled": false,
    "embedding_model": "bge-m3",
    "chunk_size": 500,
    "chunk_overlap": 50,
    "retrieval_k": 4,
    "score_threshold": 0.3
  }
}
```

---

## 9. Discord Bot の招待設定

### OAuth2 スコープ

[Discord Developer Portal](https://discord.com/developers/applications) → アプリ → **OAuth2 → URL Generator** で以下を選択します。

| スコープ | 理由 |
|---|---|
| `bot` | Bot をサーバーに参加させる |
| `applications.commands` | スラッシュコマンド（`/chat`, `/db`, `/memory` 等）を登録する |

### Bot 権限

| 権限 | 理由 |
|---|---|
| View Channels | チャンネルのメッセージを読む |
| Send Messages | 返答を送信する |
| Embed Links | ステータス表示などの Embed を送る |
| Read Message History | 会話コンテキストを取得する |

パーミッション整数: **`84992`**

### Privileged Gateway Intents（要手動有効化）

Developer Portal → アプリ → **Bot** ページで以下を ON にします。

- **Message Content Intent** — メンション時のメッセージ本文を読むために必須

### 招待 URL テンプレート

```
https://discord.com/api/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=84992&scope=bot%20applications.commands
```

`YOUR_CLIENT_ID` を Developer Portal の **Application ID** に置き換えてください。

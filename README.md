# Nocord - Bot

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

Discord では `/db create` で新しい DB を作り、`/db use` で切り替えます。

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
| `/db list` | 利用可能な DB 一覧 |
| `/db current` | 現在のサーバーの DB |
| `/db create name:xxx password:xxx` | 新しい DB を作成 |
| `/db use name:xxx password:xxx` | 既存の DB に切り替え |

### メモリ管理

| コマンド | 説明 |
|---|---|
| `/memory save text:xxx` | 手動でメモリを保存 |
| `/memory list` | 最近のメモリを表示 |
| `/memory capture` | 会話履歴からメモリを自動抽出 |
| `/memory optimize` | メモリを整理・重複排除 |
| `/memory clear` | チャンネルの会話履歴をクリア |

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

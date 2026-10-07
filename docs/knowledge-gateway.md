# Nocord Knowledge Gateway 運用ガイド

## Architecture

```text
Discord events / startup backfill / daily 09:00 JST repair
    -> consent boundary (guild + channel/thread IDs)
    -> SQLite Discord Archive (message-level source of truth)
        -> FTS5 unicode61 + trigram
        -> optional message-level vectors (derived, revision/model-bound)
        -> generated 30-minute conversation blocks (bounded Q&A context)
    -> Hybrid Search -> read-only MCP / Discord Q&A

PDF / Notion / file RAG -> existing vector backend -> existing Q&A
```

`memory.sqlite` はBotとの会話・長期記憶用のままです。Discord原文は別DBへ保存します。
LLM／Embeddingを保存経路に呼び出しません。Vectorは60秒ごとのbackground workerで最大100件ずつ処理し、
失敗時は未Indexのまま後で再試行します。モデル変更時もrevision/model照合で旧Vectorを検索対象から除外します。
Vectorは初期MVPではSQLiteにJSON配列として置き、既存RAGのEmbedding APIを再利用します。
検索はFTSとVector順位のReciprocal Rank Fusion (`1/(60+rank)`) と原文完全フレーズ一致加点です。
Vectorを無効にした運用でもFTS・保存・MCPは使えます。

## Personal deployment（Windows）

Python 3.11/3.12と、このTrust Zone専用のcheckout・実行ユーザーを使います。

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config/personal.example.json config/personal.local.json
$env:NOCORD_CONFIG = "$PWD\config\personal.local.json"
$env:DISCORD_TOKEN = "<personal bot token>"
.venv\Scripts\python.exe main.py
```

実際のTokenはプロセス環境またはそのマシンだけの `.env` に設定してください。
設定ファイルへTokenを書かないでください。`.local.json` / `.env` / SQLite / `data/` はGit対象外です。
`NOCORD_ARCHIVE_PATH` を指定するとDBパスを上書きできます。デフォルトでは `data/personal/archive.sqlite` です。
空Allowlistは収集・検索とも0件です。Discord Developer PortalでMessage Content Intentを有効化し、
Botに対象チャンネルのView Channel / Read Message History権限を付与します。

Windows Task Schedulerでは、タスクの作業ディレクトリをこのcheckoutへ、プログラムを
`.venv\Scripts\python.exe` の絶対パスへ、引数を `main.py` に設定します。
環境変数はそのマシンの専用ユーザーに設定するか、アクセス権を限定した `.env` を使います。
タスクの多重起動を無効にし、ログオン時起動・失敗時再起動を設定してください。
停止はタスク終了、手動起動ならCtrl+Cです。再起動時にcheckpoint以後を補完します。

## School deployment

学校のマシン・学校用Bot Token・学校用checkoutを使い、`config/school.example.json` を
`config/school.local.json` にコピーして `NOCORD_CONFIG` で選択します。
School側の `.env` とDBをPersonal PCへコピーしません。サンプルの初回Backfillは `none`、
DBパスは `data/school/archive.sqlite` です。管理者の保存方針・同意に従い、必要なら `days` 等を選択します。
Trust Zone名はDB内にも固定され、別Zone名で同一DBを開くと失敗します。
このチェックは設定間違いの防止であり、OSのファイル権限の代替ではありません。
学校と個人はMCPも別プロセス／別接続です。中央集約や外部クラウドへの自動複製は行いません。

## Allowlist

```json
{
  "discord_archive": {
    "enabled": true,
    "trust_zone": "school",
    "db_path": "data/school/archive.sqlite",
    "include_bots": false,
    "qa_enabled": true,
    "allow_guilds": {
      "123456789": {"channels": ["111111111", "222222222"]}
    },
    "backfill": {"mode": "days", "days": 90},
    "repair_days": 7,
    "vector": {"enabled": false, "model": "bge-m3"}
  }
}
```

IDはDiscord開発者モードでコピーします。公開threadは許可済み親チャンネルの同意を継承します。
private threadはそのthread IDも `channels` に明示しなければ保存・返却しません。
親を保存せずthreadだけを明示指定する運用も可能です。DMは対象外です。
`include_bots=false` でBot自身を含むBot投稿を除外します。
Realtime、Backfill、FTS／Vector、context、thread、MCP全toolへ同じ境界を適用します。
Allowlistを削除した既存データも検索対象から外します。設定はイベント／MCPリクエストごとに再読込します。
進行中の同期へ確実に反映するには設定変更後に再起動してください。

## Backfill / reconciliation

初回の各チャンネル／threadに対して、以下の `backfill.mode` を選べます。

| mode | 範囲 |
|---|---|
| `none` | 導入以前をhistoryから取り込まない |
| `latest` | 最新 `limit` 件（既定100、上限10000、Bot除外前の件数） |
| `days` | 過去 `days` 日（既定30） |
| `full` | 全history、明示指定時だけ |

再起動／毎日09:00 JST／`/rag sync_server` でチャンネル別checkpoint以後を取得します。
差分は上限なしで最後まで走査し、全書き込み・repair成功後にだけそのチャンネルのcheckpointを進めます。
境界は1秒重ね、message_id upsertで重複を防ぎます。別チャンネルの失敗は他の同期を止めません。
公開archived threadはページ上限を設けず列挙し、許可されたprivate threadはID指定で取得します。

`repair_days` 内の保存済みメッセージを個別取得して、取りこぼした編集・削除を修復します。
DiscordのUnknown Message（HTTP404、code10008）だけを削除扱いにします。
403・通信失敗・不完全なhistory・thread一覧の欠落を削除の根拠にしません。
古い編集／削除はRealtimeで検出します。停止中のrepair windowより古い変更は自動修復対象外なので、
必要なら `repair_days` を一時的に広げて再同期してください。API rate limitはdiscord.pyへ委ねます。
初回Backfill設定は既存checkpointを巻き戻しません。過去範囲を追加する際は、停止・backup後、
対象の `archive_checkpoints` 行だけを管理者が削除し、設定したBackfillで再起動します。

## Delete / edit policy

最新の編集だけを保存します。古いhistoryから新しい編集を上書きしません。
削除はtombstoneとし、本文を空にし添付metadataを消します。FTSとVectorもtransaction内で無効化します。
削除済みIDはBackfillで復活しません。MCP／Q&Aの新しい検索結果には削除本文を返しません。
既に保持しているIDの削除通知は、Allowlist撤回後でも本文消去だけを行います。
配信済み回答、Bot会話Memory、別途手動登録した資料、古いbackupを遡って消す機能はありません。
SQLite未使用ページの物理消去を保証するものではありません。

## Archive migration / existing RAG

Archiveは新規作成します。旧30分RAGチャンクから原文を推測して移植しません。
必要な過去ログはDiscord historyから明示Backfillします。MemoryやPDF／Notionは変更しません。
schema v1→v2ではprivate-thread属性を追加し、既存threadは安全側でprivate扱いにします。
公開threadも再取得で可視性が確認されるまではthread IDの明示許可が必要です。未知の将来schemaは拒否します。
Zone marker・checkpoint・FTS／Vectorテーブルは必要時に冪等に追加します。起動前にbackupを取ってください。

Archive無効時は既存Discord直接RAG同期が動きます。有効時は `/rag sync_server` がArchiveの同期へ切り替わり、
旧 `discord:guild:channel...` ソースを新たにVector DBへ重複投入しません。コマンドの `mode` / `limit` /
`incremental` の代わりにArchive設定とチャンネル別checkpointが適用されます。
text部分の `total_chunks` は0で、Vector進捗はArchive healthに表示します。既存のPDF同期wrapperは維持し、
Archive有効時の自動PDF走査にもAllowlistを適用します。

`qa_enabled=true` でDiscordのQ&AへArchive evidenceを加えます。既存Guild→DB紐付けを確認し、
他GuildのArchiveを混ぜません。公開済みraw historyの旧 `discord:` ソースは、削除・同意撤回後の漏洩を
避けるためArchive有効中のQ&Aでは除外します。PDF／Notionソースはそのまま使います。
30分ブロックは取得時に原文から再生成し、最大前後10件の文脈から関連ブロックを渡します。
既存HTTP／SlackのQ&AへArchive原文を自動共有しません。これらの既存RAG／Q&Aは継続します。
旧Vector DBの `discord:` ソースはbackup後に既存 `/rag trim` 等で削除できます。勝手に既存DBを消しません。

## MCP connection

SDKは [official MCP Python SDK](https://py.sdk.modelcontextprotocol.io/) のv2を使用しています。
MCPは内部LLMを呼ばず、原文とprovenanceだけを返します。

ローカルstdioクライアントは、実行コマンドをvenv内のPython絶対パス、引数を
`-m interfaces.mcp_server`、作業ディレクトリをcheckout、環境を `NOCORD_CONFIG` で設定します。
Discord Tokenなしでも保存済みArchiveの検索が可能です。

```powershell
$env:NOCORD_CONFIG = "$PWD\config\personal.local.json"
.venv\Scripts\python.exe -m interfaces.mcp_server --transport stdio
```

HTTPは `python -m interfaces.mcp_server --transport streamable-http` で単独起動します。
設定の `mcp.enabled=true` なら `main.py` からDiscord／Slack／HTTP APIと同時起動できます。

```json
{"mcp": {"enabled": true, "host": "127.0.0.1", "port": 8001}}
```

接続URLは `http://127.0.0.1:8001/mcp`。`NOCORD_MCP_TOKEN` を設定した場合は
`Authorization: Bearer <token>` が全HTTPリクエストに必須です。
loopback以外へのbindにはこのTokenが必須です。Discord Tokenと共用しません。
DNS rebinding対策を有効にしており、HTTPS reverse proxyを使う際は `mcp.public_url` に
そのMCPの公開URLを指定します。認可されたZoneだけでTLS・ネットワークアクセス制限を管理します。
この実装は静的Bearer認証です。OAuth自動発見・認可サーバーは提供していません。
接続先がOAuthを要求する場合はZoneの認証gatewayが必要です。
ChatGPT等のリモートクライアントからローカルloopbackへ直接到達はできないため、
管理者が承認したHTTPS経路とクライアントの認証方式を実環境で確認してください。
この開発では外部公開／Token登録は実施していません。

| Tool | 返却 |
|---|---|
| `search_discord` | query + guild/channel/thread/author/date filters、score、match_type |
| `get_message_context` | 同一channel/threadの前後会話（各側最大100件） |
| `get_thread` | thread原文、`after` / `limit` |
| `get_messages` | channel/thread原文、`after` / `before` / `limit` |
| `list_channels` | 保存済み・現在許可された場所、`guild_id` / `limit` |

context以外の上限は100件、queryは2000文字です。message_id / reply relation /
guild/channel/thread/author / UTC timestamps / discord_urlを含みます。DBパス・秘密設定・Tokenは返しません。
本文にはソース利用者自身が投稿した機密が含まれ得るため、接続・呼び出しはZoneの方針に従います。
thread/get_messagesのdate境界は排他的です。大量取得・同時刻メッセージのページングの改善は今後の候補です。

## Health / reindex

```powershell
.venv\Scripts\python.exe scripts/archive.py status
.venv\Scripts\python.exe scripts/archive.py reindex
```

statusは件数、indexed/pending、last event/reconcile、エラー種別、Allowlist数、SQLite quick_checkを返します。
パス・本文・Tokenは表示しません。Vector無効ならpendingは本文がある未生成の原文件数です。
HTTP APIの `/archive/status` は `HTTP_API_KEY` が設定された場合だけ同じstatusを返します。
既存 `/status` はそのままです。reindexはFTS再構築後、Vector有効時に全件を再生成します。
Embedding停止なら失敗を終了コードで通知し、原文・FTSは残ります。

## Backup / restore

```powershell
.venv\Scripts\python.exe scripts/archive.py backup --path data/personal/backups/archive-2026-10-07.sqlite
# 全Nocord/MCPプロセス停止後、新しいDBパスへrestore
$env:NOCORD_ARCHIVE_PATH = "$PWD\data\personal\restored.sqlite"
.venv\Scripts\python.exe scripts/archive.py restore --path data/personal/backups/archive-2026-10-07.sqlite
.venv\Scripts\python.exe scripts/archive.py status
```

SQLite backup APIでWALを含む整合したsnapshotを作ります。既存ファイルは上書きしません。
restoreはZone一致とquick_checkを検証し、新規パスへだけ実行します。
各Zone内の管理者承認済みローカル保存先を使い、OS権限を制限します。School→Personal cloud等への転送は行いません。
保存期間・頻度はZone管理者が決めてください（例:日次、7世代）。削除前のbackupには当時の原文が残るため、
同じZoneの削除・保持ルールで世代を廃棄します。自動外部バックアップは初期MVPの対象外です。

## Troubleshooting / maintenance

- 0件: config選択、enabled、Guild/Channel IDs、private threadの明示ID、Bot権限を確認。
- 編集／削除が残る: repair_days、Unknown Message／403区別、last_reconcile_errorを確認。
- pendingが増える: LM StudioのEmbedding modelとvector.model名、base_urlを確認。FTSは利用可能。
- HTTP401: MCP専用Bearerまたは既存APIのX-API-Keyを確認。HTTP421/403:公開Host/Origin設定を確認。
- Zone mismatch:DBを混用せず正しいマシン・設定を選択。SQLite future schema:旧アプリで起動しない。
- DB locked:多重Bot起動・backup中の長いtransactionを確認。

自動テストは `python -m unittest discover -s tests -v`。CIはWindows/Linux、Python3.11/3.12です。
Discord API、LLM、Notionをmockし、stdio実プロセス／HTTP MCP protocolも検証します。
実サーバーの投稿・編集・削除・停止中補完、モデル有効時のsemantic品質、外部MCP接続は別途手動確認してください。
Dotsは必須にしていません。将来のagentはCI・health・Issueを参照できますが、本文の外部送信やZone間転送は
そのZoneごとの明示許可が必要です。

# Knowledge Gateway 実装・検証報告

作業基点: 最新 `origin/main` (`57295ecc08f9208e635a270a4339c744d4f33f45`)。
作業ブランチ: `codex/knowledge-gateway`。2026-10-07 JST。
親 [#27](https://github.com/Soso2e/Nocord-Bot/issues/27) と子Issue全件を読んで指定順に実装した。
既存checkoutはクリーンで、mainとorigin/mainが一致することを開始時・最終確認時に確認した。

## 対応状況

| Issue | 実装 | 検証・残る確認 |
|---|---|---|
| [#28](https://github.com/Soso2e/Nocord-Bot/issues/28) Archive | SQLite message DTO、upsert、UTC、reply/thread/attachment metadata、permalink、編集・tombstone | 保存・冪等性・再open・stale書き込み・添付・thread文脈の自動テスト済み |
| [#31](https://github.com/Soso2e/Nocord-Bot/issues/31) Trust Zone | Default Deny、共通保存／検索境界、private-thread明示同意、Zone marker、別DB/Token/config、Git除外 | 同意撤回・不正設定・cross-zone open/restore拒否の自動テスト済み。実マシンの権限設定は手動 |
| [#29](https://github.com/Soso2e/Nocord-Bot/issues/29) Realtime | 非メンションCREATE、raw UPDATE/DELETE/bulk、Bot除外、Q&Aと保存障害の隔離 | mockイベントと既存Discordコマンドdispatch回帰を確認。実Discordイベントは手動 |
| [#30](https://github.com/Soso2e/Nocord-Bot/issues/30) Reconciliation | startup/09:00 JST/manual、4種の初回範囲、チャンネル別checkpoint、archived/private thread、repair | 再起動差分・冪等性・失敗分離・成功範囲checkpoint・404と403区別の自動テスト済み |
| [#32](https://github.com/Soso2e/Nocord-Bot/issues/32) Hybrid Search | FTS5 unicode61/trigram、metadata、rank fusion、context、Archive由来Vector、モデル／revision照合 | 日本語・コード・URL・番号・順位・Embedding停止・編集削除・Index再生成の自動テスト済み |
| [#33](https://github.com/Soso2e/Nocord-Bot/issues/33) MCP | 5 Read-only tools、stdio/Streamable HTTP、Bearer、Host/Origin境界、provenance、返却上限、例外秘匿 | 実stdio subprocessでinitialize→tools/list→search→context、HTTP protocol/auth contract済み。ChatGPT等の実接続は手動 |
| [#34](https://github.com/Soso2e/Nocord-Bot/issues/34) Q&A | Guild→DBを確認したArchive検索、原文由来30分ブロック、旧Discord直接RAGの段階移行、PDF/Notion経路維持 | Q&A/Memory、JSON RAG、Notion統合、HTTP、Slack、既存PDFテスト済み。実モデルでの回答品質比較は手動 |
| [#35](https://github.com/Soso2e/Nocord-Bot/issues/35) Operations | health、local CLI、backup/restore、migration、Windows手順、Zone分離、CI | SQLite/WAL snapshot、Zone制約、schema移行、health秘匿の自動テスト済み。CIリモート実行・実運用は未確認 |

GitHub Issueは未close。実サービスでの確認まで完了したとは扱っていない。
既定 `config/app.json` はArchive無効のままで、実Token設定・データ収集・外部公開・自動バックアップは行っていない。

## 主要ファイル

| ファイル | 役割 |
|---|---|
| `core/discord_archive.py` | 原文・添付・tombstone・SQLite schema・Zone marker |
| `core/gateway_config.py` | config選択、Default Deny、共通SQL境界、DBパス |
| `core/discord_archive_sync.py` | Discord DTO、rawイベント保存・消去、障害隔離 |
| `core/archive_reconcile.py` | 初回Backfill・差分・repair・checkpoint |
| `core/archive_search.py` | FTS5・Vector projection・Hybrid・文脈・provenance |
| `core/knowledge_search.py` | 既存文書RAGとArchive evidenceの合成・30分ブロック |
| `core/archive_operations.py`, `scripts/archive.py` | Index worker、health、reindex、backup/restore |
| `interfaces/mcp_server.py` | Read-only MCP、stdio/HTTP、認証、安全な例外 |
| `interfaces/discord_bot.py`, `main.py` | 既存起動への接続、startup/daily/event/index worker |
| `core/context_builder.py`, `core/chat_controller.py` | Archive evidenceを既存Q&Aへ追加、検索のthread offload |
| `core/discord_pdf_sync.py`, `interfaces/http_api.py` | PDFの既存経路維持とArchive時Allowlist、認証health |
| `config/personal.example.json`, `config/school.example.json`, `.env.example`, `.gitignore` | 安全なZone別サンプルと秘密／DB除外 |
| `tests/test_archive_*.py`, `tests/test_discord_archive.py`, `tests/test_gateway_policy.py`, `tests/test_mcp_server.py`, `tests/test_knowledge_integration.py`, `tests/test_boundary_regression.py` | 新機能・境界・既存機能の回帰 |
| `.github/workflows/tests.yml` | Windows/Linux × Python3.11/3.12 CI |
| `docs/knowledge-gateway.md`, `README.md` | Architecture・運用・接続・移行・backup手順 |

## テスト結果

- `python -m unittest discover -s tests -q`: **42件成功**（既存PDFテスト4件を含む）。
- `python -m compileall -q core interfaces scripts main.py`: 成功。
- `python -m pip check`: `No broken requirements found`。
- `git diff --check`: 成功。
- Discord/HTTPモジュールimport: 成功。
- stdio実プロセスとHTTPのMCP protocol contract: 成功。
- このPCでの実行はWindows／Python3.12。Linux/Python3.11のCI設定は追加したがリモート実行は未確認。

テスト中のArchive失敗警告はmockで障害を起こした隔離テストの想定出力。
discord.pyの `audioop` DeprecationWarningは既存依存由来。
実Discord／Slack／Notionアカウント、LM Studio、外部ChatGPTには接続していない。
追加のDB・Token・ローカル環境設定はコミット対象外。

## 手動確認チェックリスト

- Personal/Schoolそれぞれのマシンで専用Token、DB、MCP、OS権限が分離されていること。
- 実Allowlistで非メンション投稿、編集、単独／bulk削除、公開／private／archived threadを確認。
- Bot停止中に投稿し、再起動後の補完と再同期後の重複なしを確認。
- Bot権限を外したチャンネルが他のcheckpointを止めず、削除誤検出を起こさないこと。
- LM Studio停止中のArchive保存／FTSを確認し、再開後のVector追いつきと意味検索品質を比較。
- 既存Discord Q&A、PDF、Google Docs、Notion、Slack、HTTPを実環境で確認。
- 接続先クライアントのMCP transport／認証方式、TLS公開経路、検索→context→permalinkを確認。
- Task Schedulerの自動起動・再起動、Zone内backup世代管理・restoreを確認。
- PR作成後のGitHub Actions結果を確認。

## 未解決事項・今後の改善候補

1. 実サービスのE2E・外部MCP接続・モデルによる検索／回答品質評価は未実施。
2. 停止中の編集／削除修復はrepair window内の保存済み原文が対象。古い変更はwindow拡大による再修復が必要。
3. HTTP MCPは静的Bearer。OAuth必須クライアント用の認証gatewayは各Zoneで別途必要。
4. Vector検索はSQLite内の配列を候補走査するMVP構成。大規模ログでは専用Vector backendやバッチ処理の測定・最適化が候補。
5. date境界だけのページングでは同時刻hitの完全走査に限界がある。message_id付きcursorと大規模thread取得の改善が候補。
6. Allowlistを外した原文は検索から除外するが物理消去はしない。保存期間・撤回時purge・backup世代保持はZone管理者の方針策定が必要。
7. 過去回答・Memory・手動登録資料・削除前backupを遡って消す機能は非対象。削除通知を受けた原文／派生Indexは消去する。
8. Dots等による監視・依存更新は今後の候補。本文外部送信・Zone集約は自動化していない。

## PR作成時の記載案

Title: `Add consent-aware Discord Archive and read-only Knowledge Gateway MCP`

Discord原文をRAGから分離してSQLiteのSource of Truthに保存し、明示AllowlistとTrust Zone境界の下で
Realtime／再起動補完／FTS・Hybrid検索／Read-only MCP／既存Discord Q&Aを提供する。
削除本文・添付はtombstone化しIndexから除外、Embedding障害時も保存とFTSを継続する。
Archiveは既定無効。PDF／Notion／Slack／HTTPの既存経路を保持する。

Refs #27, #28, #31, #29, #30, #32, #33, #34, #35。
Validation: Windows Python3.12で42 tests、compileall、pip check、diff check成功。
Manual pending: 実Discord、モデル品質、ChatGPT等の外部MCP認証／接続、リモートCI。
運用・移行手順: `docs/knowledge-gateway.md`。

## 2026-10-08: Discordからの保存対象登録

ユーザー要望に基づき `/archive register`・`/archive unregister`・`/archive list` を追加。
サーバー管理権限をruntimeでも検証し、現在の場所またはchannel pickerで選んだ場所を登録できる。
Zone別ローカル `*.allowlist.json` に永続化し、configのID指定と合成する。
解除は設定済みの許可や公開threadの親からの継承より優先し、MCP／保存の共通境界へ反映する。
進行中の収集も保存直前に最新の同意を再確認する。秘密情報やtracked configを変更しない。

新規主要ファイル: `core/archive_allowlist.py`、`interfaces/archive_commands.py`、
`tests/test_archive_registration.py`。README・運用ガイドも更新。
追加後の検証: **50 tests成功**、compileall、pip check、diff check成功。
実Discordへのコマンド同期はToken設定後に手動確認する。

## 2026-10-11: Windows起動・自動起動

`scripts/run_bot.py` と `scripts/install_autostart.ps1` を追加し、現在のユーザーのログオン時に
非表示で起動するTask Scheduler構成を用意した。実行時間制限なし、障害時1分ごとに再起動、
同一checkoutの手動／タスク二重起動をOSロックで防ぐ。停止したinterface threadをmainが検出する。
Token等を伏せたrotateログをローカル `data/runtime/nocord.log` に保存する。
起動・ログ秘匿・プロセスロックの追加後、**54 tests成功**、compileall、pip check、diff check成功。
Task Schedulerの登録と起動経路を実機で確認した。実Discordへの接続完了は、正しいBot Token設定後に確認する。
READMEに別プロジェクト `Server/Nocord`（Notion→Discord Webhook通知）との違いも記載した。

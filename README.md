# Discord Hardcore Admin

以前の [discord-mc-admin](https://github.com/yukugura/discord-mc-admin) の MySQL・Minecraft VM 構成を使い、Discord のコマンドツリーでハードコアサーバーを提供します。

- `/create` — EULA 同意（はい/いいえ）後に近接 VC の有無を選択。`default` ユーザーは名前なしで 1 台まで作成。admin は `name` を必ず入力して複数台作成可能
- 近接 VC ありは Paper を選び、DB の `server_versions` に登録された対応バージョンからプルダウン選択してSimple Voice Chatを導入。なしは選択なしでDB登録済みの最新Paperをプラグインなしで作成
- `/admin key:<ADMIN_KEY>` — `admin` 権限に変更。admin は複数台作成可能
- `/reset` — 自分のサーバーを「はい / いいえ」で確認してリセット。admin が複数台持つ場合は対象選択を表示
- `/reset code:<8桁コード>` — 他ユーザーのサーバーをリセットする場合だけコードを入力
- `/delete` — 「はい / いいえ」で確認してサーバーを完全に削除し、作成枠を解放
- `/status` — 自分のサーバーの実際の起動状態、接続先、リセットコード（spoiler）、自動削除予定を表示
- バックグラウンド処理が、最後のリセット（作成直後を含む）から `RESET_RETENTION_DAYS` を過ぎたサーバーを約 6 時間ごとに自動削除
- `server_events` テーブルに作成要求・成功/失敗・リセット成功/失敗・自動削除成功/失敗・admin 権限の付与を記録

応答はすべて実行者だけに見えます。内部ポートは公開せず、接続先として `hc01.DOMAIN_NAME` から `hc10.DOMAIN_NAME` を返します。割当は `25401 → hc01`、…、`25410 → hc10` です。BungeeCord の `forced_hosts`・転送先設定は VM 側で行ってください。

## 設定

`.env.example` を `.env` として埋めてください。既存リポジトリと同じ環境変数名を保ち、追加は削除期限の `RESET_RETENTION_DAYS` のみです。`DOMAIN_NAME` には親ドメインを設定します。ポート範囲は `SV_MIN_PORT=25401` と `SV_MAX_PORT=25410` にしてください。10 台すべて使用中の場合は作成せず、その旨をユーザーへ返します。SSH の known-hosts 用変数などは追加していません。SSH 接続では実行ホストの既存 `known_hosts` を使い、未知のホスト鍵は拒否します。

MySQL 初期スキーマは既存リポジトリの `assets/create-table.sql` を使い、その後に一度だけ [sql/migration-hardcore.sql](C:/Users/makot/Documents/ChatGPT/discord-hardcore-admin/sql/migration-hardcore.sql) を実行してください。

## VM の準備

Ubuntu VM では、同梱の [setup-hardcore-vm.sh](C:/Users/makot/Documents/ChatGPT/discord-hardcore-admin/setup-script/setup-hardcore-vm.sh) を root で一度実行するだけです。Java、`minecraft` ユーザー、`/minecraft/scripts`、作成・リセット・削除用スクリプト、systemd、sudoers、バックアップ先を構成します。

```bash
sudo bash setup-hardcore-vm.sh
```

最初に次の選択肢が出ます。

1. **既存 discord-mc-admin 環境へ追加** — 既存の `/minecraft/scripts`、ユーザー、Java、サーバー設定を変更しません。ハードコア専用の `/minecraft/scripts/hardcore/`、ラッパー、sudoers、バックアップ先だけを追加します。
2. **新しい Minecraft VM を構築** — Minecraft ユーザー、Java、`/minecraft/scripts/hardcore/` 以下の作成・削除スクリプトまで新規作成します。SSH 公開鍵を貼り付けます。

ハードコアサーバーは既存 `/minecraft/config` の `server.properties`、`eula.txt`、`spigot.yml`、`stop.sh`、`TEST-25565.service` をそのままテンプレートにします。`TEST-25565` だけを `HC-25401` のような名前へ置換します。start.sh は既存と同じ `screen -AdmSU` 形式、JAR は `<選択バージョン>.jar`（例: `26.2.0.jar`）で権限 `754` です。

既存環境のスクリプトの場所が異なる場合だけ、`SCRIPTS_DIR=/minecraft/実際の場所 sudo bash setup-hardcore-vm.sh` のように指定します。ダウンロード URL は Discord から渡さず、DB の `server_versions` に登録済みの値だけを使用します。

セットアップは既存 `dc-mc-admin` を変更せず、`/etc/ufw/applications.d/hc-mc-admin` に `25401:25410/tcp` と Simple Voice Chat 用の `24401:24410/udp` の UFW プロファイルを追加して許可します。UFW 自体の有効化は行いません。VCありで作成したPaperサーバーだけにSimple Voice Chatを導入し、UDPポートはMinecraftポートから1000を引いた値になります。VCなしではDB登録済みのPaper最新版を、プラグインなしで起動します。

ワールドリセット時の旧ワールドは `/minecraft/backups/hardcore/HC-<port>/<UTC時刻>/` に退避されます。`RESET_RETENTION_DAYS`（既定30日）を過ぎたバックアップは6時間ごとのクリーンアップで削除され、サーバーを削除するとそのサーバーのバックアップ一式も削除されます。

## 起動

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

Discord Developer Portal で `bot` と `applications.commands` スコープを指定して招待してください。`ADMIN_KEY` は Discord に入力されるため、十分長いランダム値にしてください。

# セットアップガイド（主催者がやることリスト）

所要時間の目安: 必須部分のみで30分、全部やって1時間ほど。

---

## ✅ ステップ1: Discord Bot を作る【必須・10分】

1. https://discord.com/developers/applications → **New Application** → 名前を入力（例: YeG Tourney）
2. 左メニュー **Bot** → **Reset Token** → トークンをコピー（後で `.env` に貼る）
3. 同じページの **Privileged Gateway Intents** で
   **MESSAGE CONTENT INTENT** を ON
4. 左メニュー **OAuth2 → URL Generator**
   - SCOPES: `bot` と `applications.commands` にチェック
   - BOT PERMISSIONS: `Administrator` にチェック
5. 生成されたURLをブラウザで開いて自分のサーバーに招待

> 主催者＝サーバー管理者なら追加のロール設定は不要。
> 管理者権限を持たない手伝いスタッフに運営コマンドを使わせたい場合だけ、
> サーバーに「運営」ロールを作ってその人に付与すればOK。

## ✅ ステップ2: Bot を動かす【必須・10分】

```bash
cd yeg-tourney-bot
pip install -r requirements.txt
copy .env.example .env
```

`.env` をメモ帳で開いて記入:

```
DISCORD_TOKEN=ステップ1でコピーしたトークン
GUILD_ID=自分のサーバーID
```

サーバーIDの調べ方: Discordの「ユーザー設定 → 詳細設定 → 開発者モード」をON
→ サーバーアイコンを右クリック → 「サーバーIDをコピー」

起動:

```bash
python bot.py
```

`✅ ログイン成功` と出れば完了。Discord で `/tourney-create` が出てくるか確認。

## ✅ ステップ3: スプレッドシート連携【推奨・15分】

> ※ sushida-bot で使った `credentials.json` があれば、それをこのフォルダに
> コピーするだけでステップ3-1〜3-4はスキップできます（3-5から）。

### 3-1. Google Cloud プロジェクト作成
https://console.cloud.google.com/ → プロジェクトを新規作成

### 3-2. API有効化
「APIとサービス → ライブラリ」で以下2つを有効化:
- Google Sheets API
- Google Drive API

### 3-3. サービスアカウント作成
「APIとサービス → 認証情報 → 認証情報を作成 → サービスアカウント」
名前は何でもOK（例: yeg-tourney）。ロールは不要。

### 3-4. 鍵をダウンロード
作成したサービスアカウントをクリック → 「キー」タブ → 「鍵を追加 → 新しい鍵を作成 → JSON」
→ ダウンロードしたファイルを `credentials.json` にリネームしてこのフォルダに置く

### 3-5. スプレッドシート作成と共有
1. https://sheets.google.com で新規スプレッドシートを作成（名前例: YeGタイピング大会）
2. `credentials.json` の中の `"client_email"`（…@….iam.gserviceaccount.com）をコピー
3. スプレッドシートの「共有」でそのメールアドレスを **編集者** として追加
4. スプレッドシートURLの `/d/` と `/edit` の間がID。`.env` に記入:

```
SPREADSHEET_ID=ここにID
```

Bot を再起動 → 起動ログに「スプレッドシート連携: 有効」と出ればOK。
以後、エントリーや結果入力のたびに「参加者」「対戦表」「運営ログ」シートが自動更新されます。

## ✅ ステップ4: ブラケットのWeb公開【推奨・15分】

参加者がスマホで対戦表を見られるようになります。

1. GitHub に新規リポジトリを作成（公開リポジトリ）
2. このフォルダを push:

```bash
git init
git add .
git commit -m "initial commit"
git branch -M main
git remote add origin https://github.com/あなたのID/リポジトリ名.git
git push -u origin main
```

3. GitHub のリポジトリ設定 → **Pages** → Source を **GitHub Actions** に変更
4. `.env` に追記して Bot を再起動:

```
GIT_AUTO_PUSH=true
```

以後、結果入力のたびに自動 push → 数十秒でWebページに反映されます。
URL（`https://あなたのID.github.io/リポジトリ名/`）を参加者に共有してください。

> ⚠️ `.gitignore` で `.env` と `credentials.json` は push されない設定済み。
> この2ファイルは絶対にGitHubに上げないこと。

## ✅ ステップ5: リマインド機能【任意・5分】

試合結果の入力忘れを防ぎたい場合:

```
REMINDER_MINUTES=10
REMINDER_CHANNEL_ID=運営チャンネルのID
```

同じ試合が10分以上動かないと運営チャンネルに通知が来ます。

---

## 🏁 当日の運営フロー

```
事前       /tourney-create 第1回タイピング大会
           /tourney-open          ← 参加者がボタンでエントリー
受付終了   /tourney-close         ← 名簿を確認
開始       /tourney-seed ランダム  ← 対戦表が自動生成・Web公開
試合ごと   /tourney-next          ← 対戦者を自動メンション
           （試合）
           /tourney-result R1M1 プレイヤー1 100-80
           ↑ 勝者が自動進出、次の試合を自動案内
ミス時     /tourney-undo          ← 直前の入力を取り消し
欠席者     /tourney-dq 名前        ← 相手が不戦勝で自動進出
終了後     /tourney-export        ← 結果一覧をファイル出力
```

参加者側は「エントリーボタン」「/tourney-myresult」「Webページ」だけ案内すれば十分です。

## 🧪 本番前にやっておくテスト

- [ ] 自分含む2〜3人（またはサブ垢）でエントリー → seed → result → 優勝まで一周
- [ ] `/tourney-undo` で結果を取り消して再入力できるか
- [ ] `/tourney-dq` で不戦勝が正しく進むか
- [ ] スプレッドシートに3シートが自動生成されているか
- [ ] GitHub Pages のURLをスマホで開いて対戦表が見えるか
- [ ] テストが終わったら `/tourney-reset` で初期化

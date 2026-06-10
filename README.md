# YeG Tourney Bot

横須賀市eスポーツ大会向けの大会運営自動化ツール。
エントリー受付 → 対戦表生成 → 試合進行 → 結果公開までを Discord 上でほぼ自動化します。

- **Discord**: スラッシュコマンド＋エントリーボタンで運営・参加
- **Googleスプレッドシート**: 参加者名簿・対戦表・運営ログを自動同期
- **GitHub Pages**: `bracket.html` でブラケットをWeb公開（60秒ごと自動更新）
- **安全網**: 全保存前に自動バックアップ、`/tourney-undo` で取り消し可能

## コマンド一覧

### 運営用（サーバー管理者 or「運営」ロール）
| コマンド | 説明 |
|---|---|
| `/tourney-create <大会名>` | 大会を新規作成 |
| `/tourney-open` | エントリー受付開始（ボタン付き告知を投稿） |
| `/tourney-close` | エントリー締切（参加者一覧を表示） |
| `/tourney-seed <ランダム/申込順>` | 対戦表を生成。不戦勝(BYE)は自動処理 |
| `/tourney-result <試合ID> <勝者> [スコア]` | 結果入力。勝者が自動進出、次の試合を案内 |
| `/tourney-undo` | 直前の操作（結果・棄権・対戦表生成）を取り消し |
| `/tourney-dq <参加者名>` | 棄権処理。進行中なら相手が不戦勝に |
| `/tourney-export` | 結果一覧をテキストファイルで出力 |
| `/tourney-reset` | 大会データを完全リセット（確認ボタン付き） |

### 参加者用（誰でも）
| コマンド | 説明 |
|---|---|
| 🎮 エントリーボタン | モーダルで参加登録（二重エントリー自動防止） |
| `/tourney-next` | 次の対戦カードを表示・対戦者にメンション |
| `/tourney-myresult` | 自分の戦績・次の試合を確認（本人にのみ表示） |
| `/tourney-status` | 大会全体の進行状況を表示 |

## セットアップ

```bash
pip install -r requirements.txt
copy .env.example .env   # .envにトークン等を記入
python bot.py
```

詳しい手順は SETUP.md を参照。

## スプレッドシート連携

`.env` に `SPREADSHEET_ID` と `GOOGLE_CREDENTIALS_FILE` を設定すると、
保存のたびに以下の3シートへ自動同期されます:

- **参加者** — エントリー名簿（棄権状況・エントリー日時つき）
- **対戦表** — 全試合の状況（対戦可能/終了/相手待ち）
- **運営ログ** — 日時・操作者・操作内容の記録

連携未設定でも Bot 本体は問題なく動きます（JSONのみで運用）。

## GitHub Pages 連携

`GIT_AUTO_PUSH=true` にすると `tournament.json` 更新時に自動 push し、
`.github/workflows/deploy.yml` が `bracket.html` を GitHub Pages に公開します。
参加者はURLを開くだけで最新のブラケットを確認できます。

## リマインド機能

`.env` で `REMINDER_MINUTES` と `REMINDER_CHANNEL_ID` を設定すると、
結果が未入力のまま放置されている試合を指定チャンネルに定期通知します。

## データ保護

- 保存のたびに `backups/` へタイムスタンプ付きJSONを退避（最新50件）
- `/tourney-undo` で直近10操作まで巻き戻し可能
- `/tourney-reset` は確認ボタンを押すまで実行されない

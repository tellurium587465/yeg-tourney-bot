"""Googleスプレッドシート同期モジュール。

tournament.json の内容を3つのワークシートにミラーする:
  - 参加者: エントリー一覧（運営が名簿として印刷・確認できる形）
  - 対戦表: 全試合の状況
  - 運営ログ: いつ誰が何をしたかの記録
"""

import os
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")

_client = None


def sheets_enabled() -> bool:
    return bool(SPREADSHEET_ID) and os.path.exists(GOOGLE_CREDENTIALS_FILE)


def _get_spreadsheet():
    global _client
    if _client is None:
        creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=SCOPES)
        _client = gspread.authorize(creds)
    return _client.open_by_key(SPREADSHEET_ID)


def _get_or_create_worksheet(spreadsheet, title: str, rows: int = 100, cols: int = 10):
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


def _format_header(ws, cell_range: str):
    ws.format(cell_range, {
        "textFormat": {"bold": True},
        "backgroundColor": {"red": 0.15, "green": 0.4, "blue": 0.92},
    })


def player_name(p) -> str:
    if p is None:
        return "BYE"
    return p.get("name", "?")


def sync_participants(spreadsheet, data: dict):
    ws = _get_or_create_worksheet(spreadsheet, "参加者")
    rows = [["No.", "参加者名", "Discord表示名", "ユーザーID", "状態", "エントリー日時"]]
    for i, p in enumerate(data.get("participants", []), start=1):
        status = "棄権" if p.get("status") == "dq" else "参加"
        rows.append([
            str(i), p.get("name", ""), p.get("discord_name", ""),
            p.get("user_id", ""), status, p.get("entered_at", ""),
        ])
    ws.clear()
    ws.update(f"A1:F{len(rows)}", rows)
    _format_header(ws, "A1:F1")


def sync_matches(spreadsheet, data: dict):
    ws = _get_or_create_worksheet(spreadsheet, "対戦表")
    rows = [["試合ID", "Round", "プレイヤー1", "プレイヤー2", "勝者", "スコア", "状態"]]
    for m in sorted(data.get("matches", []), key=lambda x: (x["round"], x["match_number"])):
        if m["winner"] == 1:
            winner = player_name(m["player1"])
        elif m["winner"] == 2:
            winner = player_name(m["player2"])
        else:
            winner = ""
        if m["winner"] is not None:
            state = "終了"
        elif m["player1"] and m["player2"]:
            state = "対戦可能"
        else:
            state = "対戦相手待ち"
        rows.append([
            m["id"], str(m["round"]),
            player_name(m["player1"]), player_name(m["player2"]),
            winner, m.get("score") or "", state,
        ])
    ws.clear()
    ws.update(f"A1:G{len(rows)}", rows)
    _format_header(ws, "A1:G1")


def append_log(spreadsheet, action: str, operator: str = ""):
    ws = _get_or_create_worksheet(spreadsheet, "運営ログ")
    if not ws.row_values(1):
        ws.update("A1:C1", [["日時", "操作者", "操作内容"]])
        _format_header(ws, "A1:C1")
    ws.append_row([datetime.now().strftime("%Y/%m/%d %H:%M:%S"), operator, action])


def sync_all(data: dict, action: str = "", operator: str = ""):
    """tournament.json の状態を全シートに反映する。失敗してもBot本体は止めない。"""
    if not sheets_enabled():
        return False, "スプレッドシート連携が未設定です"
    try:
        spreadsheet = _get_spreadsheet()
        sync_participants(spreadsheet, data)
        sync_matches(spreadsheet, data)
        if action:
            append_log(spreadsheet, action, operator)
        return True, None
    except Exception as e:
        return False, str(e)

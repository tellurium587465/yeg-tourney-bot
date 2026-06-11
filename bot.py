import os
import io
import json
import random
import shutil
import subprocess
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

import sheets  # noqa: E402  (load_dotenv後に読み込む必要がある)

# =====================================
# 設定
# =====================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # 開発用：指定するとそのサーバーに即時コマンド反映
ADMIN_ROLE_NAME = os.getenv("ADMIN_ROLE_NAME", "運営")
TOURNAMENT_FILE = os.getenv("TOURNAMENT_FILE", "tournament.json")
GIT_AUTO_PUSH = os.getenv("GIT_AUTO_PUSH", "false").lower() == "true"
# テスト用: trueにすると同じ人が複数エントリーできる（本番ではfalseに戻すこと）
ALLOW_MULTI_ENTRY = os.getenv("ALLOW_MULTI_ENTRY", "false").lower() == "true"
GIT_REPO_DIR = os.getenv("GIT_REPO_DIR") or os.path.dirname(os.path.abspath(__file__))

# 結果未報告リマインド（0で無効）
REMINDER_MINUTES = int(os.getenv("REMINDER_MINUTES", "0"))
REMINDER_CHANNEL_ID = os.getenv("REMINDER_CHANNEL_ID")

BACKUP_DIR = os.path.join(GIT_REPO_DIR, "backups")
MAX_BACKUPS = 50
MAX_HISTORY = 10


# =====================================
# tournament.json 読み書き
# =====================================
def empty_tournament():
    return {
        "tournament_name": "",
        "status": "draft",  # draft / entry_open / entry_closed / in_progress / finished
        "participants": [],
        "matches": [],
        "updated_at": None,
        "_history": [],
    }


def tournament_path() -> str:
    return os.path.join(GIT_REPO_DIR, TOURNAMENT_FILE)


def load_tournament() -> dict:
    if not os.path.exists(tournament_path()):
        return empty_tournament()
    with open(tournament_path(), "r", encoding="utf-8") as f:
        return json.load(f)


def backup_tournament():
    """保存前に現在のJSONをbackups/へ退避する"""
    if not os.path.exists(tournament_path()):
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(tournament_path(), os.path.join(BACKUP_DIR, f"tournament_{stamp}.json"))

    backups = sorted(os.listdir(BACKUP_DIR))
    for old in backups[:-MAX_BACKUPS]:
        os.remove(os.path.join(BACKUP_DIR, old))


def save_tournament(data: dict, action: str = "update", operator: str = ""):
    """保存＋バックアップ＋git push＋スプレッドシート同期。

    戻り値: (sheets_ok, sheets_error) — 運営向けフィードバックに使う
    """
    backup_tournament()
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    with open(tournament_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    if GIT_AUTO_PUSH:
        try:
            subprocess.run(["git", "add", TOURNAMENT_FILE], cwd=GIT_REPO_DIR, check=True)
            # tournament.json のみコミット（他のステージ済みファイルを巻き込まない）
            subprocess.run(["git", "commit", "-m", action, "--", TOURNAMENT_FILE], cwd=GIT_REPO_DIR, check=False)
            subprocess.run(["git", "push"], cwd=GIT_REPO_DIR, check=True)
        except subprocess.CalledProcessError as e:
            print(f"git push に失敗しました: {e}")

    sheets_ok, sheets_err = sheets.sync_all(data, action=action, operator=operator)
    if not sheets_ok and sheets.sheets_enabled():
        print(f"スプレッドシート同期に失敗: {sheets_err}")
    return sheets_ok, sheets_err


def push_history(data: dict):
    snapshot = {
        "matches": json.loads(json.dumps(data["matches"])),
        "participants": json.loads(json.dumps(data["participants"])),
        "status": data["status"],
    }
    data["_history"].append(snapshot)
    if len(data["_history"]) > MAX_HISTORY:
        data["_history"] = data["_history"][-MAX_HISTORY:]


def sync_note(sheets_ok: bool) -> str:
    """運営向け：スプレッドシート同期結果のひとこと"""
    if not sheets.sheets_enabled():
        return ""
    return "\nスプレッドシートに同期しました。" if sheets_ok else "\nスプレッドシート同期に失敗しました（データ自体は保存済み）。"


# =====================================
# ブラケット生成・進行ロジック
# =====================================
def active_participants(data: dict) -> list:
    return [p for p in data["participants"] if p.get("status") != "dq"]


def generate_bracket(participants: list, mode: str) -> list:
    entrants = list(participants)
    if mode == "random":
        random.shuffle(entrants)

    n = len(entrants)
    bracket_size = 1 << (n - 1).bit_length() if n > 1 else 2
    slots = entrants + [None] * (bracket_size - n)

    matches = []
    round_size = bracket_size // 2
    for i in range(round_size):
        matches.append({
            "id": f"R1M{i + 1}",
            "round": 1,
            "match_number": i + 1,
            "player1": slots[2 * i],
            "player2": slots[2 * i + 1],
            "winner": None,
            "score": None,
            "next_match_id": None,
            "next_slot": None,
        })

    round_num = 2
    round_size //= 2
    prev_round_matches = matches
    while round_size >= 1:
        current = []
        for i in range(round_size):
            current.append({
                "id": f"R{round_num}M{i + 1}",
                "round": round_num,
                "match_number": i + 1,
                "player1": None,
                "player2": None,
                "winner": None,
                "score": None,
                "next_match_id": None,
                "next_slot": None,
            })
        for i, m in enumerate(prev_round_matches):
            m["next_match_id"] = current[i // 2]["id"]
            m["next_slot"] = 1 if i % 2 == 0 else 2
        matches.extend(current)
        prev_round_matches = current
        round_num += 1
        round_size //= 2

    resolve_byes(matches)
    return matches


def find_match(matches: list, match_id: str):
    for m in matches:
        if m["id"] == match_id:
            return m
    return None


def _feeder_match(matches: list, match: dict, slot: int):
    """このスロットに勝者を送り込む前段の試合を探す"""
    for f in matches:
        if f["next_match_id"] == match["id"] and f["next_slot"] == slot:
            return f
    return None


def _slot_can_fill(matches: list, match: dict, slot: int) -> bool:
    """このスロットに将来プレイヤーが入る可能性があるか。
    False = 真のBYE（前段が存在しないか、前段が誰も送り込めない）"""
    player = match["player1"] if slot == 1 else match["player2"]
    if player is not None:
        return True
    feeder = _feeder_match(matches, match, slot)
    if feeder is None:
        return False  # Round1の空きスロット = BYE
    if feeder["winner"] is not None:
        return False  # 前段は決着済みなのに誰も来ていない（BYE同士など）
    return _slot_can_fill(matches, feeder, 1) or _slot_can_fill(matches, feeder, 2)


def resolve_byes(matches: list):
    """不戦勝を自動処理する。相手が「前の試合待ち」の場合は進めない。"""
    changed = True
    while changed:
        changed = False
        for m in matches:
            if m["winner"] is not None:
                continue
            p1, p2 = m["player1"], m["player2"]
            if p1 is not None and p2 is None and not _slot_can_fill(matches, m, 2):
                advance_winner(matches, m, 1, score="不戦勝")
                changed = True
            elif p2 is not None and p1 is None and not _slot_can_fill(matches, m, 1):
                advance_winner(matches, m, 2, score="不戦勝")
                changed = True


def advance_winner(matches: list, match: dict, winner_slot: int, score: str = None):
    match["winner"] = winner_slot
    match["score"] = score
    winner_player = match["player1"] if winner_slot == 1 else match["player2"]
    if match["next_match_id"]:
        nxt = find_match(matches, match["next_match_id"])
        if nxt:
            if match["next_slot"] == 1:
                nxt["player1"] = winner_player
            else:
                nxt["player2"] = winner_player


def get_next_match(matches: list):
    for m in sorted(matches, key=lambda m: (m["round"], m["match_number"])):
        if m["winner"] is None and m["player1"] and m["player2"]:
            return m
    return None


def get_pending_matches(matches: list) -> list:
    return [
        m for m in sorted(matches, key=lambda m: (m["round"], m["match_number"]))
        if m["winner"] is None and m["player1"] and m["player2"]
    ]


def is_finished(matches: list) -> bool:
    final_round = max(m["round"] for m in matches)
    return all(m["winner"] is not None for m in matches if m["round"] == final_round)


def get_champion(matches: list):
    final_round = max(m["round"] for m in matches)
    final = [m for m in matches if m["round"] == final_round][0]
    if final["winner"] == 1:
        return final["player1"]
    if final["winner"] == 2:
        return final["player2"]
    return None


def player_label(player: dict | None) -> str:
    if player is None:
        return "BYE"
    return player["name"]


# =====================================
# 権限チェック（サーバー管理者ならOK）
# =====================================
def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
        role = discord.utils.get(interaction.user.roles, name=ADMIN_ROLE_NAME)
        if role is None:
            await interaction.response.send_message(
                f"この操作はサーバー管理者、または「{ADMIN_ROLE_NAME}」ロールのみ実行できます。",
                ephemeral=True,
            )
            return False
        return True

    return app_commands.check(predicate)


# =====================================
# エントリーモーダル & ビュー
# =====================================
class EntryModal(discord.ui.Modal, title="大会エントリー"):
    game_name = discord.ui.TextInput(
        label="参加者名（ゲーム内表示名など）",
        placeholder="例: たろう#1234",
        max_length=50,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        data = load_tournament()

        if data["status"] != "entry_open":
            await interaction.response.send_message("現在エントリー受付中ではありません。", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        for p in data["participants"]:
            if not ALLOW_MULTI_ENTRY and p["user_id"] == user_id:
                await interaction.response.send_message(
                    f"すでに「{p['name']}」としてエントリー済みです。\n"
                    "名前を変更したい場合は運営にお声がけください。",
                    ephemeral=True,
                )
                return
            if p["name"] == str(self.game_name.value):
                await interaction.response.send_message(
                    f"「{p['name']}」は既に使われている名前です。別の名前で登録してください。",
                    ephemeral=True,
                )
                return

        # 保存（シート同期・git push）は数秒かかるため、先にdeferして3秒制限を回避
        await interaction.response.defer(ephemeral=True)

        data["participants"].append({
            "user_id": user_id,
            "name": str(self.game_name.value),
            "discord_name": interaction.user.display_name,
            "status": "active",
            "entered_at": datetime.now().strftime("%Y/%m/%d %H:%M"),
        })
        save_tournament(data, f"エントリー: {self.game_name.value}", operator=interaction.user.display_name)

        await interaction.followup.send(
            f"エントリーを受け付けました（{self.game_name.value}）。\n"
            f"現在の参加者数: {len(data['participants'])}人\n"
            "対戦表が公開されたら /tourney-myresult で自分の試合を確認できます。",
            ephemeral=True,
        )


class EntryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="エントリーする", style=discord.ButtonStyle.primary, custom_id="yeg_tourney_entry")
    async def entry_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EntryModal())


class ConfirmResetView(discord.ui.View):
    """リセットの誤爆防止用 確認ボタン"""

    def __init__(self, author_id: int):
        super().__init__(timeout=1800)  # 30分。放置されたボタンが押せなくなるまでの時間
        self.author_id = author_id

    @discord.ui.button(label="本当にリセットする", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("コマンドを実行した本人のみ操作できます。", ephemeral=True)
            return
        # 保存（シート同期・git push）は数秒かかるため、先に応答してDiscordの3秒制限を回避する
        await interaction.response.edit_message(content="リセット処理中...", view=None)
        data = empty_tournament()
        save_tournament(data, "大会リセット", operator=interaction.user.display_name)
        await interaction.edit_original_response(
            content="大会データをリセットしました。`/tourney-create` から再度作成できます。\n"
                    "（直前までのデータは `backups/` フォルダに残っています）",
        )

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="キャンセルしました。", view=None)


# =====================================
# Discord Bot
# =====================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

_last_reminded_match_id = None


@bot.event
async def on_ready():
    bot.add_view(EntryView())
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    else:
        await bot.tree.sync()
    if REMINDER_MINUTES > 0 and REMINDER_CHANNEL_ID and not reminder_loop.is_running():
        reminder_loop.start()
    print(f"ログイン成功: {bot.user}")
    print(f"  スプレッドシート連携: {'有効' if sheets.sheets_enabled() else '無効（SPREADSHEET_ID未設定）'}")
    print(f"  git自動push: {'有効' if GIT_AUTO_PUSH else '無効'}")


@tasks.loop(minutes=max(REMINDER_MINUTES, 1))
async def reminder_loop():
    """進行中の試合が一定時間動かないとき、運営チャンネルにリマインドする"""
    global _last_reminded_match_id
    data = load_tournament()
    if data["status"] != "in_progress":
        return
    match = get_next_match(data["matches"])
    if match is None:
        return
    if match["id"] == _last_reminded_match_id:
        channel = bot.get_channel(int(REMINDER_CHANNEL_ID))
        if channel:
            await channel.send(
                f"結果未入力の試合があります: `{match['id']}` "
                f"**{player_label(match['player1'])} vs {player_label(match['player2'])}** の結果が未入力です。\n"
                f"終了していたら `/tourney-result` で入力してください。"
            )
    _last_reminded_match_id = match["id"]


# --- /tourney-create ---
@bot.tree.command(name="tourney-create", description="新しい大会を作成します")
@app_commands.describe(name="大会名")
@is_admin()
async def tourney_create(interaction: discord.Interaction, name: str):
    await interaction.response.defer()
    data = empty_tournament()
    data["tournament_name"] = name
    ok, _ = save_tournament(data, f"大会作成: {name}", operator=interaction.user.display_name)

    embed = discord.Embed(
        title=name,
        description="エントリー受付準備中です。",
        color=discord.Color.blue(),
    )
    embed.add_field(
        name="次のステップ",
        value="`/tourney-open` を実行するとエントリー受付が始まります。" + sync_note(ok),
        inline=False,
    )
    await interaction.followup.send(embed=embed)


# --- /tourney-open ---
@bot.tree.command(name="tourney-open", description="エントリー受付を開始します")
@is_admin()
async def tourney_open(interaction: discord.Interaction):
    data = load_tournament()
    if not data["tournament_name"]:
        await interaction.response.send_message(
            "大会がまだありません。先に `/tourney-create` で作成してください。", ephemeral=True
        )
        return

    await interaction.response.defer()
    data["status"] = "entry_open"
    save_tournament(data, "エントリー受付開始", operator=interaction.user.display_name)

    embed = discord.Embed(
        title=f"{data['tournament_name']} エントリー受付中",
        description="下の「エントリーする」ボタンを押して、表示名を入力してください。\n"
                    "（1人1回まで。重複エントリーは自動でブロックされます）",
        color=discord.Color.green(),
    )
    embed.add_field(
        name="運営の次のステップ",
        value="参加者が集まったら `/tourney-close` で受付を締め切ります。\n"
              "現在の参加状況は `/tourney-status` で確認できます。",
        inline=False,
    )
    await interaction.followup.send(embed=embed, view=EntryView())


# --- /tourney-close ---
@bot.tree.command(name="tourney-close", description="エントリー受付を締め切ります")
@is_admin()
async def tourney_close(interaction: discord.Interaction):
    data = load_tournament()
    if data["status"] != "entry_open":
        await interaction.response.send_message("エントリー受付中ではありません。", ephemeral=True)
        return

    await interaction.response.defer()
    data["status"] = "entry_closed"
    ok, _ = save_tournament(data, "エントリー締切", operator=interaction.user.display_name)

    names = "\n".join(f"{i}. {p['name']}" for i, p in enumerate(data["participants"], 1)) or "（参加者なし）"
    embed = discord.Embed(
        title=f"{data['tournament_name']} エントリー締切",
        description=f"参加者数: {len(data['participants'])}人\n\n{names}",
        color=discord.Color.orange(),
    )
    embed.add_field(
        name="次のステップ",
        value="`/tourney-seed` で対戦表を生成します（ランダム or 申込順）。" + sync_note(ok),
        inline=False,
    )
    await interaction.followup.send(embed=embed)


# --- /tourney-seed ---
@bot.tree.command(name="tourney-seed", description="ブラケット（対戦表）を生成します")
@app_commands.describe(mode="シード方法")
@app_commands.describe(
    mode="シード方法",
    order="「指定順」のときに使用。参加者名をカンマ区切りで並べる（例: A,B,C,D → A vs B / C vs D）",
)
@app_commands.choices(mode=[
    app_commands.Choice(name="ランダム", value="random"),
    app_commands.Choice(name="申込順", value="order"),
    app_commands.Choice(name="指定順（スキル別など運営が並びを決める）", value="manual"),
])
@is_admin()
async def tourney_seed(interaction: discord.Interaction, mode: app_commands.Choice[str], order: str = None):
    data = load_tournament()
    if data["status"] not in ("entry_closed", "in_progress"):
        await interaction.response.send_message(
            "先に `/tourney-close` でエントリーを締め切ってください。", ephemeral=True
        )
        return

    entrants = active_participants(data)
    if len(entrants) < 2:
        await interaction.response.send_message("参加者が2人未満のため対戦表を作れません。", ephemeral=True)
        return

    if mode.value == "manual":
        if not order:
            names = ", ".join(p["name"] for p in entrants)
            await interaction.response.send_message(
                "「指定順」では order に参加者名をカンマ区切りで入力してください。\n"
                "並べた順に 1-2番目、3-4番目…が対戦カードになります。\n"
                f"現在の参加者: {names}",
                ephemeral=True,
            )
            return
        wanted = [x.strip() for x in order.replace("、", ",").split(",") if x.strip()]
        by_name = {p["name"]: p for p in entrants}
        unknown = [n for n in wanted if n not in by_name]
        dup = [n for n in set(wanted) if wanted.count(n) > 1]
        missing = [p["name"] for p in entrants if p["name"] not in wanted]
        if unknown or dup or missing:
            problems = []
            if unknown:
                problems.append(f"参加者にいない名前: {', '.join(unknown)}")
            if dup:
                problems.append(f"重複している名前: {', '.join(dup)}")
            if missing:
                problems.append(f"並びに入っていない参加者: {', '.join(missing)}")
            await interaction.response.send_message(
                "指定順に問題があります。\n" + "\n".join(problems), ephemeral=True
            )
            return
        entrants = [by_name[n] for n in wanted]

    await interaction.response.defer()
    push_history(data)
    data["matches"] = generate_bracket(entrants, mode.value)
    data["status"] = "in_progress"
    ok, _ = save_tournament(data, f"対戦表生成（{mode.name}）", operator=interaction.user.display_name)

    rounds = sorted(set(m["round"] for m in data["matches"]))
    lines = []
    for r in rounds:
        lines.append(f"**Round {r}**" if r != rounds[-1] else "**決勝**")
        for m in sorted([x for x in data["matches"] if x["round"] == r], key=lambda x: x["match_number"]):
            lines.append(f"`{m['id']}` {player_label(m['player1'])} vs {player_label(m['player2'])}")
        lines.append("")

    embed = discord.Embed(
        title=f"対戦表（{mode.name}）",
        description="\n".join(lines),
        color=discord.Color.purple(),
    )
    embed.add_field(
        name="次のステップ",
        value="`/tourney-next` で最初の試合を呼び出し、終わったら `/tourney-result` で結果入力。" + sync_note(ok),
        inline=False,
    )
    await interaction.followup.send(embed=embed)


# --- /tourney-result ---
@bot.tree.command(name="tourney-result", description="試合結果を入力します")
@app_commands.describe(match_id="試合ID（例: R1M1）", winner="勝者", score="スコア（任意、例: 2-1）")
@app_commands.choices(winner=[
    app_commands.Choice(name="プレイヤー1", value=1),
    app_commands.Choice(name="プレイヤー2", value=2),
])
@is_admin()
async def tourney_result(interaction: discord.Interaction, match_id: str, winner: app_commands.Choice[int], score: str = None):
    data = load_tournament()
    match = find_match(data["matches"], match_id.upper())
    if match is None:
        valid = ", ".join(m["id"] for m in get_pending_matches(data["matches"])) or "なし"
        await interaction.response.send_message(
            f"試合 `{match_id}` が見つかりません。\n現在入力可能な試合: {valid}", ephemeral=True
        )
        return

    if not match["player1"] or not match["player2"]:
        await interaction.response.send_message("まだ両者が確定していない試合です。", ephemeral=True)
        return

    if match["winner"] is not None:
        await interaction.response.send_message(
            "すでに結果が登録されています。間違えた場合は `/tourney-undo` で取り消せます。", ephemeral=True
        )
        return

    await interaction.response.defer()
    push_history(data)
    advance_winner(data["matches"], match, winner.value, score=score)

    finished = is_finished(data["matches"])
    if finished:
        data["status"] = "finished"

    ok, _ = save_tournament(
        data, f"結果入力: {match['id']} 勝者={player_label(match['player1'] if winner.value == 1 else match['player2'])}",
        operator=interaction.user.display_name,
    )

    winner_name = player_label(match["player1"] if winner.value == 1 else match["player2"])
    embed = discord.Embed(
        title=f"{match['id']} 結果登録",
        description=f"勝者: **{winner_name}**" + (f"\nスコア: {score}" if score else ""),
        color=discord.Color.green(),
    )

    if finished:
        champion = get_champion(data["matches"])
        embed.add_field(
            name="大会終了",
            value=f"優勝: **{player_label(champion)}**\n"
                  f"`/tourney-export` で結果一覧を出力できます。" + sync_note(ok),
            inline=False,
        )
    else:
        nxt = get_next_match(data["matches"])
        if nxt:
            embed.add_field(
                name="次の試合",
                value=f"`{nxt['id']}` {player_label(nxt['player1'])} vs {player_label(nxt['player2'])}\n"
                      f"`/tourney-next` で対戦者を呼び出せます。" + sync_note(ok),
                inline=False,
            )
    await interaction.followup.send(embed=embed)


# --- /tourney-undo ---
@bot.tree.command(name="tourney-undo", description="直前の操作（結果入力・棄権・対戦表生成）を取り消します")
@is_admin()
async def tourney_undo(interaction: discord.Interaction):
    data = load_tournament()
    if not data["_history"]:
        await interaction.response.send_message("取り消せる操作がありません。", ephemeral=True)
        return

    await interaction.response.defer()
    snapshot = data["_history"].pop()
    data["matches"] = snapshot["matches"]
    data["participants"] = snapshot["participants"]
    data["status"] = snapshot["status"]
    ok, _ = save_tournament(data, "操作の取り消し", operator=interaction.user.display_name)

    await interaction.followup.send("直前の操作を取り消しました。`/tourney-status` で現状を確認してください。" + sync_note(ok))


# --- /tourney-dq ---
@bot.tree.command(name="tourney-dq", description="参加者を棄権扱いにします（不戦勝で相手が進出）")
@app_commands.describe(name="棄権する参加者名（エントリー名）")
@is_admin()
async def tourney_dq(interaction: discord.Interaction, name: str):
    data = load_tournament()
    target = None
    for p in data["participants"]:
        if p["name"] == name and p.get("status") != "dq":
            target = p
            break
    if target is None:
        names = ", ".join(p["name"] for p in active_participants(data)) or "なし"
        await interaction.response.send_message(
            f"「{name}」が見つかりません。\n現在の参加者: {names}", ephemeral=True
        )
        return

    await interaction.response.defer()
    push_history(data)
    target["status"] = "dq"

    msg_extra = ""
    if data["status"] == "in_progress":
        # 未消化の試合で相手を不戦勝にする
        for m in data["matches"]:
            if m["winner"] is not None:
                continue
            p1, p2 = m["player1"], m["player2"]
            if p1 and p1["user_id"] == target["user_id"] and p2:
                advance_winner(data["matches"], m, 2, score="不戦勝（棄権）")
                msg_extra = f"\n`{m['id']}` は **{player_label(p2)}** の不戦勝になりました。"
                break
            if p2 and p2["user_id"] == target["user_id"] and p1:
                advance_winner(data["matches"], m, 1, score="不戦勝（棄権）")
                msg_extra = f"\n`{m['id']}` は **{player_label(p1)}** の不戦勝になりました。"
                break
        resolve_byes(data["matches"])
        if is_finished(data["matches"]):
            data["status"] = "finished"

    ok, _ = save_tournament(data, f"棄権処理: {name}", operator=interaction.user.display_name)
    await interaction.followup.send(
        f"**{name}** さんを棄権扱いにしました。{msg_extra}\n間違えた場合は `/tourney-undo` で戻せます。" + sync_note(ok)
    )


# --- /tourney-next ---
@bot.tree.command(name="tourney-next", description="次の試合を表示し、対戦者にメンションします")
async def tourney_next(interaction: discord.Interaction):
    data = load_tournament()
    if data["status"] != "in_progress":
        await interaction.response.send_message("大会は進行中ではありません。", ephemeral=True)
        return

    match = get_next_match(data["matches"])
    if match is None:
        await interaction.response.send_message("現在対戦可能な試合はありません（結果入力待ち、または全試合終了）。")
        return

    p1, p2 = match["player1"], match["player2"]
    mention1 = f"<@{p1['user_id']}>" if p1.get("user_id") else p1["name"]
    mention2 = f"<@{p2['user_id']}>" if p2.get("user_id") else p2["name"]

    embed = discord.Embed(
        title=f"次の試合: {match['id']} (Round {match['round']})",
        description=f"**{p1['name']}** vs **{p2['name']}**\n\n準備ができたら試合を開始してください。",
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="運営の次のステップ",
        value=f"試合が終わったら `/tourney-result {match['id']} <勝者>` で結果を入力します。\n"
              "入力すると勝者が自動で次の試合に進み、続く試合が案内されます。",
        inline=False,
    )
    await interaction.response.send_message(content=f"{mention1} {mention2}", embed=embed)


# --- /tourney-myresult ---
@bot.tree.command(name="tourney-myresult", description="自分の試合状況を確認します")
async def tourney_myresult(interaction: discord.Interaction):
    data = load_tournament()
    user_id = str(interaction.user.id)

    me = None
    for p in data["participants"]:
        if p["user_id"] == user_id:
            me = p
            break
    if me is None:
        await interaction.response.send_message("あなたはこの大会にエントリーしていません。", ephemeral=True)
        return

    if me.get("status") == "dq":
        await interaction.response.send_message("あなたは棄権扱いになっています。詳細は運営にお問い合わせください。", ephemeral=True)
        return

    lines = []
    upcoming = None
    for m in sorted(data["matches"], key=lambda m: (m["round"], m["match_number"])):
        p1, p2 = m["player1"], m["player2"]
        in_match = (p1 and p1["user_id"] == user_id) or (p2 and p2["user_id"] == user_id)
        if not in_match:
            continue
        opponent = p2 if (p1 and p1["user_id"] == user_id) else p1
        if m["winner"] is None:
            if p1 and p2:
                upcoming = m
                lines.append(f"`{m['id']}` vs **{player_label(opponent)}** — 対戦待ち")
            else:
                lines.append(f"`{m['id']}` — 対戦相手が決まり次第お知らせします")
        else:
            won = (m["winner"] == 1 and p1["user_id"] == user_id) or (m["winner"] == 2 and p2 and p2["user_id"] == user_id)
            mark = "勝利" if won else "敗北"
            score = f"（{m['score']}）" if m.get("score") else ""
            lines.append(f"{mark} `{m['id']}` vs {player_label(opponent)} {score}")

    if not lines:
        lines.append("まだ対戦表が生成されていません。しばらくお待ちください。")

    embed = discord.Embed(
        title=f"{me['name']} さんの戦績",
        description="\n".join(lines),
        color=discord.Color.gold() if upcoming else discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# --- /tourney-status ---
@bot.tree.command(name="tourney-status", description="大会の進行状況を表示します")
async def tourney_status(interaction: discord.Interaction):
    data = load_tournament()
    if not data["tournament_name"]:
        await interaction.response.send_message("大会が作成されていません。", ephemeral=True)
        return

    status_label = {
        "draft": "準備中",
        "entry_open": "エントリー受付中",
        "entry_closed": "エントリー締切",
        "in_progress": "進行中",
        "finished": "終了",
    }.get(data["status"], data["status"])

    done = sum(1 for m in data["matches"] if m["winner"] is not None)
    total = len(data["matches"])
    progress = f"\n試合進行: **{done} / {total}**" if total else ""

    embed = discord.Embed(
        title=data['tournament_name'],
        description=f"状態: **{status_label}**\n参加者数: {len(active_participants(data))}人{progress}",
        color=discord.Color.blurple(),
    )

    if data["matches"]:
        rounds = sorted(set(m["round"] for m in data["matches"]))
        for r in rounds:
            lines = []
            for m in sorted([x for x in data["matches"] if x["round"] == r], key=lambda x: x["match_number"]):
                p1, p2 = player_label(m["player1"]), player_label(m["player2"])
                if m["winner"] == 1:
                    p1 = f"**{p1}**（勝）"
                elif m["winner"] == 2:
                    p2 = f"**{p2}**（勝）"
                lines.append(f"`{m['id']}` {p1} vs {p2}")
            title = "決勝" if r == rounds[-1] else f"Round {r}"
            embed.add_field(name=title, value="\n".join(lines) or "-", inline=False)

    if data["status"] == "draft":
        guide = "`/tourney-open` でエントリー受付を開始します。"
    elif data["status"] == "entry_open":
        guide = "参加者が集まったら `/tourney-close` で受付を締め切ります。"
    elif data["status"] == "entry_closed":
        guide = "`/tourney-seed ランダム` または `/tourney-seed 申込順` で対戦表を作成します。"
    elif data["status"] == "in_progress":
        nxt = get_next_match(data["matches"])
        if nxt:
            guide = (f"次の試合: `{nxt['id']}` {player_label(nxt['player1'])} vs {player_label(nxt['player2'])}\n"
                     f"`/tourney-next` で対戦者を呼び出し、終了後 `/tourney-result` で結果を入力します。")
        else:
            guide = "結果入力待ちの試合があります。`/tourney-result` で入力してください。"
    elif data["status"] == "finished":
        guide = "全試合が終了しました。`/tourney-export` で結果一覧を出力できます。"
    else:
        guide = ""
    if guide:
        embed.add_field(name="次の操作", value=guide, inline=False)

    await interaction.response.send_message(embed=embed)


# --- /tourney-export ---
@bot.tree.command(name="tourney-export", description="大会結果をテキストファイルで出力します")
@is_admin()
async def tourney_export(interaction: discord.Interaction):
    data = load_tournament()
    if not data["tournament_name"]:
        await interaction.response.send_message("大会が作成されていません。", ephemeral=True)
        return

    lines = [
        f"大会名: {data['tournament_name']}",
        f"出力日時: {datetime.now().strftime('%Y/%m/%d %H:%M')}",
        "",
        "== 参加者 ==",
    ]
    for i, p in enumerate(data["participants"], 1):
        dq = "（棄権）" if p.get("status") == "dq" else ""
        lines.append(f"{i}. {p['name']}{dq}  Discord: {p.get('discord_name', '')}")

    if data["matches"]:
        lines += ["", "== 試合結果 =="]
        for m in sorted(data["matches"], key=lambda m: (m["round"], m["match_number"])):
            if m["winner"] == 1:
                w = player_label(m["player1"])
            elif m["winner"] == 2:
                w = player_label(m["player2"])
            else:
                w = "未消化"
            score = f"（{m['score']}）" if m.get("score") else ""
            lines.append(f"{m['id']}: {player_label(m['player1'])} vs {player_label(m['player2'])} → {w}{score}")

        champion = get_champion(data["matches"])
        if champion:
            lines += ["", f"優勝: {champion['name']}"]

    buf = io.BytesIO("\n".join(lines).encode("utf-8"))
    fname = f"{data['tournament_name']}_結果_{datetime.now().strftime('%Y%m%d')}.txt"
    await interaction.response.send_message("結果を出力しました。", file=discord.File(buf, filename=fname))


# --- /tourney-reset ---
@bot.tree.command(name="tourney-reset", description="大会データを完全リセットします（要確認）")
@is_admin()
async def tourney_reset(interaction: discord.Interaction):
    data = load_tournament()
    await interaction.response.send_message(
        f"「{data['tournament_name'] or '（無題）'}」のデータを**すべて削除**します。\n"
        "参加者・対戦表・結果が消えます（バックアップは `backups/` に残ります）。よろしいですか？",
        view=ConfirmResetView(interaction.user.id),
        ephemeral=True,
    )


bot.run(DISCORD_TOKEN)

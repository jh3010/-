import asyncio
import datetime as dt
import sqlite3
from pathlib import Path
from typing import Optional, Iterable

import discord
from discord.ui import Button, View

try:
    from schedule_data import get_today_games, get_games_by_date, get_next_available_games
except ImportError:
    from schedule_data import get_today_games
    get_games_by_date = None
    get_next_available_games = None

KST = dt.timezone(dt.timedelta(hours=9))
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "prediction.db"
PANEL_RECORD_PATH = BASE_DIR / "prediction_panel.json"
ACTIVE_MANAGER = None


def now_kst() -> dt.datetime:
    return dt.datetime.now(KST)


def today_iso() -> str:
    return now_kst().date().isoformat()


def parse_dt(game: dict) -> Optional[dt.datetime]:
    raw_date = game.get("gdate") or game.get("game_date") or game.get("date")
    raw_time = game.get("gtime") or game.get("game_time") or game.get("time")
    if not raw_date:
        return None
    if raw_time and len(str(raw_time).split(":")) >= 2:
        s = f"{str(raw_date)[:10]} {str(raw_time)[:5]}:00"
    else:
        raw_dt = game.get("gameDateTime") or game.get("startTime")
        if raw_dt:
            s = str(raw_dt).replace("T", " ")[:19]
        else:
            s = f"{str(raw_date)[:10]} 23:59:59"
    try:
        return dt.datetime.fromisoformat(s).replace(tzinfo=KST)
    except ValueError:
        return None


def status(game: dict) -> str:
    if game.get("cancel") is True:
        return "CANCEL"
    s = str(game.get("gameStatusNormalized") or game.get("statusCode") or game.get("status") or "UNKNOWN").upper()
    if s in {"BEFORE", "SCHEDULED", "UPCOMING", "WAIT", "READY"}:
        return "BEFORE"
    if s in {"LIVE", "PLAYING", "IN_PROGRESS", "STARTED"}:
        return "LIVE"
    if s in {"END", "ENDED", "FINAL", "FINISHED", "GAME_END"}:
        return "END"
    if s in {"CANCEL", "CANCELED", "CANCELLED"}:
        return "CANCEL"
    return s


def team_names(game: dict):
    away = game.get("team_a") or game.get("awayTeamName") or game.get("aName") or game.get("away") or "원정팀"
    home = game.get("team_b") or game.get("homeTeamName") or game.get("hName") or game.get("home") or "홈팀"
    return str(away), str(home)


def game_id(game: dict) -> str:
    return str(game.get("gameId") or game.get("id") or f"{game.get('gdate')}_{game.get('gtime')}_{team_names(game)[0]}_{team_names(game)[1]}")


def normalize_result(game: dict) -> Optional[str]:
    s = status(game)
    if s != "END":
        return None
    winner = str(game.get("winner") or game.get("winningTeam") or "").upper()
    away, home = team_names(game)
    if winner in {"AWAY", "A", away.upper()}:
        return "AWAY"
    if winner in {"HOME", "H", home.upper()}:
        return "HOME"
    try:
        a = float(game.get("aScore"))
        h = float(game.get("hScore"))
        if a > h:
            return "AWAY"
        if h > a:
            return "HOME"
        return "DRAW"
    except (TypeError, ValueError):
        return None


def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


class PredictionStore:
    def __init__(self):
        self._init_db()

    def _init_db(self):
        with db_conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS games (
                game_id TEXT PRIMARY KEY,
                game_date TEXT NOT NULL,
                start_ts TEXT,
                away TEXT NOT NULL,
                home TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                result TEXT,
                updated_at TEXT NOT NULL
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS votes (
                game_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                choice TEXT NOT NULL,
                voted_at TEXT NOT NULL,
                PRIMARY KEY (game_id, user_id)
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                total_votes INTEGER NOT NULL DEFAULT 0,
                correct INTEGER NOT NULL DEFAULT 0,
                wrong INTEGER NOT NULL DEFAULT 0,
                streak INTEGER NOT NULL DEFAULT 0,
                best_streak INTEGER NOT NULL DEFAULT 0
            )""")

    def upsert_game(self, game: dict, state: Optional[str] = None):
        gid = game_id(game)
        away, home = team_names(game)
        gdate = str(game.get("gdate") or game.get("game_date") or game.get("date") or today_iso())[:10]
        start = parse_dt(game)
        st = state or ("CANCEL" if status(game) == "CANCEL" else "OPEN")
        existing = self.get_game(gid)
        if existing and existing["status"] in {"FINAL", "CANCEL"}:
            st = existing["status"]
        with db_conn() as c:
            c.execute("""INSERT INTO games(game_id,game_date,start_ts,away,home,status,result,updated_at)
                        VALUES(?,?,?,?,?,?,?,?)
                        ON CONFLICT(game_id) DO UPDATE SET game_date=excluded.game_date,start_ts=excluded.start_ts,
                        away=excluded.away,home=excluded.home,status=excluded.status,result=COALESCE(excluded.result,games.result),
                        updated_at=excluded.updated_at""",
                      (gid, gdate, start.isoformat() if start else None, away, home, st, normalize_result(game), now_kst().isoformat()))
        return gid

    def get_game(self, gid: str):
        with db_conn() as c:
            return c.execute("SELECT * FROM games WHERE game_id=?", (gid,)).fetchone()

    def set_status(self, gid: str, st: str, result: Optional[str] = None):
        with db_conn() as c:
            c.execute("UPDATE games SET status=?, result=COALESCE(?,result), updated_at=? WHERE game_id=?", (st, result, now_kst().isoformat(), gid))

    def vote(self, gid: str, user_id: int, choice: str):
        if choice not in {"AWAY", "HOME"}:
            raise ValueError("잘못된 선택입니다.")
        g = self.get_game(gid)
        if not g or g["status"] != "OPEN":
            raise ValueError("예측이 마감된 경기입니다.")
        with db_conn() as c:
            c.execute("""INSERT INTO users(user_id) VALUES(?) ON CONFLICT(user_id) DO NOTHING""", (str(user_id),))
            c.execute("""INSERT INTO votes(game_id,user_id,choice,voted_at) VALUES(?,?,?,?)
                     ON CONFLICT(game_id,user_id) DO UPDATE SET choice=excluded.choice,voted_at=excluded.voted_at""",
                      (gid, str(user_id), choice, now_kst().isoformat()))
        return self.tally(gid)

    def tally(self, gid: str):
        with db_conn() as c:
            rows = c.execute("SELECT choice, COUNT(*) n FROM votes WHERE game_id=? GROUP BY choice", (gid,)).fetchall()
        a = next((r["n"] for r in rows if r["choice"] == "AWAY"), 0)
        h = next((r["n"] for r in rows if r["choice"] == "HOME"), 0)
        total = a + h
        return {"AWAY": a, "HOME": h, "TOTAL": total, "AWAY_PCT": round(a * 100 / total, 1) if total else 0.0, "HOME_PCT": round(h * 100 / total, 1) if total else 0.0}

    def user_vote(self, gid: str, user_id: int):
        with db_conn() as c:
            row = c.execute("SELECT choice FROM votes WHERE game_id=? AND user_id=?", (gid, str(user_id))).fetchone()
            return row["choice"] if row else None

    def voters(self, gid: str, choice: str):
        with db_conn() as c:
            return [r["user_id"] for r in c.execute("SELECT user_id FROM votes WHERE game_id=? AND choice=? ORDER BY voted_at", (gid, choice)).fetchall()]

    def finalize(self, gid: str, result: str):
        if result not in {"AWAY", "HOME", "DRAW"}:
            return
        with db_conn() as c:
            rows = c.execute("SELECT user_id, choice FROM votes WHERE game_id=?", (gid,)).fetchall()
            for r in rows:
                uid = r["user_id"]
                c.execute("INSERT INTO users(user_id) VALUES(?) ON CONFLICT(user_id) DO NOTHING", (uid,))
                if result == "DRAW" or r["choice"] == result:
                    c.execute("UPDATE users SET total_votes=total_votes+1, correct=correct+1, streak=streak+1, best_streak=MAX(best_streak,streak+1) WHERE user_id=?", (uid,))
                else:
                    c.execute("UPDATE users SET total_votes=total_votes+1, wrong=wrong+1, streak=0 WHERE user_id=?", (uid,))
            c.execute("UPDATE games SET status='FINAL', result=?, updated_at=? WHERE game_id=?", (result, now_kst().isoformat(), gid))

    def ranking(self, metric="correct", limit=10):
        with db_conn() as c:
            if metric == "accuracy":
                rows = c.execute("SELECT * FROM users WHERE total_votes>=10 ORDER BY CAST(correct AS REAL)/total_votes DESC, correct DESC LIMIT ?", (limit,)).fetchall()
            elif metric == "streak":
                rows = c.execute("SELECT * FROM users ORDER BY best_streak DESC, correct DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM users ORDER BY correct DESC, CAST(correct AS REAL)/CASE WHEN total_votes=0 THEN 1 ELSE total_votes END DESC LIMIT ?", (limit,)).fetchall()
            return rows

    def stats(self, user_id: int):
        with db_conn() as c:
            row = c.execute("SELECT * FROM users WHERE user_id=?", (str(user_id),)).fetchone()
            recent = c.execute("SELECT v.game_id,v.choice,g.away,g.home,g.status,g.result FROM votes v JOIN games g ON g.game_id=v.game_id WHERE v.user_id=? ORDER BY v.voted_at DESC LIMIT 10", (str(user_id),)).fetchall()
        return row, recent


STORE = PredictionStore()


def bar(pct: float, width: int = 14):
    filled = max(0, min(width, round(pct / 100 * width)))
    return "█" * filled + "░" * (width - filled)


class VoteView(View):
    def __init__(self, gid: str, away: str, home: str):
        super().__init__(timeout=None)
        self.gid = gid
        self.away = away
        self.home = home
        self.add_item(VoteButton("AWAY", away, gid, 0))
        self.add_item(VoteButton("HOME", home, gid, 0))
        self.add_item(VoterListButton("AWAY", away, gid, 1))
        self.add_item(VoterListButton("HOME", home, gid, 1))


class VoteButton(Button):
    def __init__(self, choice: str, label: str, gid: str, pos: int):
        super().__init__(label=f"{label} 승", style=discord.ButtonStyle.secondary, custom_id=f"kbo_pred_vote_{choice.lower()}_{gid}", row=0)
        self.choice = choice
        self.gid = gid
        self.pos = pos

    async def callback(self, interaction: discord.Interaction):
        try:
            result = STORE.vote(self.gid, interaction.user.id, self.choice)
            await interaction.response.send_message(
                f"✅ 예측을 저장했습니다.\n현재 {result['AWAY']}명 : {result['HOME']}명 ({result['AWAY_PCT']}% : {result['HOME_PCT']}%)",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.response.send_message(str(e), ephemeral=True)


class PredictionPanelView(View):
    def __init__(self, games: Iterable[dict]):
        super().__init__(timeout=None)
        self.games = [g for g in games if status(g) != "CANCEL"][:25]
        for idx, game in enumerate(self.games):
            away, home = team_names(game)
            self.add_item(GameVoteButton(game_id(game), away, home, idx))
        self.add_item(RankingButton())
        self.add_item(MyPredictionButton())


class GameVoteButton(Button):
    def __init__(self, gid: str, away: str, home: str, idx: int):
        super().__init__(label=f"{away} @ {home}"[:80], style=discord.ButtonStyle.secondary, custom_id=f"kbo_pred_game_{gid}", row=min(idx // 5, 4))
        self.gid, self.away, self.home = gid, away, home

    async def callback(self, interaction: discord.Interaction):
        g = STORE.get_game(self.gid)
        if not g:
            await interaction.response.send_message("경기 정보를 찾을 수 없습니다.", ephemeral=True)
            return
        if g["status"] != "OPEN":
            await interaction.response.send_message("이 경기는 이미 예측이 마감되었습니다.", ephemeral=True)
            return
        tally = STORE.tally(self.gid)
        user_vote = STORE.user_vote(self.gid, interaction.user.id)
        embed = discord.Embed(title=f"⚾ {self.away} @ {self.home}", description="경기 시작 전까지 예측을 변경할 수 있습니다.", color=discord.Color.dark_gray())
        embed.add_field(name="현재 투표", value=f"{self.away} {tally['AWAY_PCT']:.1f}% {bar(tally['AWAY_PCT'])}\n{self.home} {tally['HOME_PCT']:.1f}% {bar(tally['HOME_PCT'])}", inline=False)
        embed.add_field(name="참여 인원", value=f"총 {tally['TOTAL']}명", inline=True)
        embed.add_field(name="내 예측", value=(self.away if user_vote == "AWAY" else self.home if user_vote == "HOME" else "아직 없음"), inline=True)
        embed.set_footer(text="버튼을 다시 누르면 예측을 변경할 수 있습니다.")
        await interaction.response.send_message(embed=embed, view=VoteView(self.gid, self.away, self.home), ephemeral=True)


class RankingButton(Button):
    def __init__(self):
        super().__init__(label="적중 랭킹 보기", style=discord.ButtonStyle.secondary, custom_id="kbo_pred_ranking", row=4)

    async def callback(self, interaction: discord.Interaction):
        rows = STORE.ranking("correct", 10)
        if not rows:
            await interaction.response.send_message("아직 예측 기록이 없습니다.", ephemeral=True)
            return
        lines = []
        for i, r in enumerate(rows, 1):
            total = r["total_votes"]
            acc = (r["correct"] / total * 100) if total else 0
            lines.append(f"**{i}위** <@{r['user_id']}> · {r['correct']}적중 / {total}경기 · {acc:.1f}%")
        embed = discord.Embed(title="🏆 적중 랭킹", description="\n".join(lines), color=discord.Color.gold())
        embed.set_footer(text="적중률 랭킹은 최소 10경기 참여자부터 집계할 수 있습니다.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class MyPredictionButton(Button):
    def __init__(self):
        super().__init__(label="내 예측 정보", style=discord.ButtonStyle.secondary, custom_id="kbo_pred_mine", row=4)

    async def callback(self, interaction: discord.Interaction):
        row, recent = STORE.stats(interaction.user.id)
        total = row["total_votes"] if row else 0
        correct = row["correct"] if row else 0
        wrong = row["wrong"] if row else 0
        streak = row["streak"] if row else 0
        best = row["best_streak"] if row else 0
        acc = correct / total * 100 if total else 0
        lines = [f"총 참여: {total}경기", f"적중: {correct}경기", f"실패: {wrong}경기", f"적중률: {acc:.1f}%", f"현재 연속 적중: {streak}경기", f"최고 연속 적중: {best}경기", "", "최근 예측"]
        for r in recent:
            predicted = r["away"] if r["choice"] == "AWAY" else r["home"]
            if r["status"] == "FINAL":
                icon = "✅" if r["result"] == r["choice"] else "❌" if r["result"] in {"AWAY", "HOME"} else "➖"
            else:
                icon = "⚾"
            lines.append(f"{icon} {r['away']} @ {r['home']} · {predicted}")
        embed = discord.Embed(title=f"👤 {interaction.user.display_name} · 내 예측 정보", description="\n".join(lines), color=discord.Color.dark_gray())
        await interaction.response.send_message(embed=embed, ephemeral=True)


def build_panel_embed(games: list[dict], target_date: str) -> discord.Embed:
    lines = []
    for g in games:
        gid = STORE.upsert_game(g, "OPEN")
        away, home = team_names(g)
        start = parse_dt(g)
        tm = start.strftime("%H:%M") if start else str(g.get("gtime") or "시간 미정")
        tally = STORE.tally(gid)
        lines.append(
            f"**{away} @ {home}** · {tm}\n"
            f"{away} {tally['AWAY_PCT']:.1f}% {bar(tally['AWAY_PCT'], 10)} · "
            f"{home} {tally['HOME_PCT']:.1f}% {bar(tally['HOME_PCT'], 10)} · "
            f"{tally['TOTAL']}명"
        )
    desc = "\n\n".join(lines) if lines else "예측 가능한 경기가 없습니다."
    return discord.Embed(
        title=f"⚾ {target_date} KBO 승부예측",
        description=desc + "\n\n경기 시작 전까지 예측을 변경할 수 있습니다.",
        color=discord.Color.dark_gray(),
    )


async def resolve_prediction_games(max_days=7):
    today = get_today_games()
    if today:
        return [g for g in today if status(g) in {"BEFORE", "OPEN"}], today_iso()
    if get_next_available_games:
        games, d = get_next_available_games(max_days=max_days)
        return [g for g in games if status(g) in {"BEFORE", "OPEN"}], d
    return [], today_iso()


class PredictionManager:
    def __init__(self, bot: discord.Client, channel_id: int):
        self.bot = bot
        self.channel_id = int(channel_id)
        self.message_id: Optional[int] = None
        self._stop = asyncio.Event()
        global ACTIVE_MANAGER
        ACTIVE_MANAGER = self
        if PANEL_RECORD_PATH.exists():
            try:
                self.message_id = int(PANEL_RECORD_PATH.read_text(encoding="utf-8").strip())
            except Exception:
                self.message_id = None

    async def _get_channel(self):
        ch = self.bot.get_channel(self.channel_id)
        if ch is None:
            ch = await self.bot.fetch_channel(self.channel_id)
        return ch

    async def publish_panel(self):
        games, target_date = await asyncio.to_thread(asyncio.run, resolve_prediction_games()) if False else await resolve_prediction_games()
        for g in games:
            STORE.upsert_game(g, "OPEN")
        channel = await self._get_channel()
        embed = build_panel_embed(games, target_date)
        view = PredictionPanelView(games)
        if self.message_id:
            try:
                msg = await channel.fetch_message(self.message_id)
                await msg.edit(embed=embed, view=view)
                return msg
            except Exception:
                self.message_id = None
        msg = await channel.send(embed=embed, view=view)
        self.message_id = msg.id
        PANEL_RECORD_PATH.write_text(str(msg.id), encoding="utf-8")
        return msg

    async def refresh_current_panel(self):
        try:
            games, target_date = await resolve_prediction_games()
            if not games:
                return
            channel = await self._get_channel()
            embed = build_panel_embed(games, target_date)
            view = PredictionPanelView(games)
            if self.message_id:
                try:
                    msg = await channel.fetch_message(self.message_id)
                    await msg.edit(embed=embed, view=view)
                    return msg
                except Exception:
                    pass
            msg = await channel.send(embed=embed, view=view)
            self.message_id = msg.id
            PANEL_RECORD_PATH.write_text(str(msg.id), encoding="utf-8")
            return msg
        except Exception as e:
            print(f"[승부예측] 패널 갱신 오류: {type(e).__name__}: {e}")

    async def sync_games(self):
        games = get_today_games()
        if not games:
            return
        for g in games:
            gid = STORE.upsert_game(g)
            st = status(g)
            if st == "CANCEL":
                STORE.set_status(gid, "CANCEL")
                continue
            start = parse_dt(g)
            if st == "END":
                result = normalize_result(g)
                if result and STORE.get_game(gid) and STORE.get_game(gid)["status"] != "FINAL":
                    STORE.finalize(gid, result)
            elif start and now_kst() >= start:
                row = STORE.get_game(gid)
                if row and row["status"] == "OPEN":
                    STORE.set_status(gid, "LOCKED")

    async def run(self, interval_seconds=60):
        while not self._stop.is_set():
            try:
                await self.sync_games()
                if now_kst().hour == 0 and now_kst().minute < 2:
                    await self.publish_panel()
            except Exception as e:
                print(f"[승부예측] 동기화 오류: {type(e).__name__}: {e}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                pass

    def stop(self):
        self._stop.set()


async def add_prediction_panel_view(bot: discord.Client, channel_id: int):
    games, target_date = await resolve_prediction_games()
    for g in games:
        STORE.upsert_game(g, "OPEN")
    ch = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
    embed = build_panel_embed(games, target_date)
    msg = await ch.send(embed=embed, view=PredictionPanelView(games))
    return msg.id
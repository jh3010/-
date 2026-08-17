"""KBO 분석 디스코드 봇 - 통합 분석 UI 개선판 (Railway 한글 폰트 대응 + 중복 분석 방지)"""

import asyncio
import datetime
import io
import json
import os
import tempfile
import time
import threading
import hashlib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import discord
from discord.ext import commands
from discord.ui import Button, Select, View
from dotenv import load_dotenv

from player_data import get_player_data
from matchup_report import build_matchup_report, MatchupReportError
from credits_db import add_credits, use_credit
try:
    from credits_db import get_status
except ImportError:
    get_status = None
from tickets_db import get_open_ticket, record_ticket, remove_ticket_by_channel
from schedule_data import get_today_games
from venue_weather import get_venue_weather, get_stadium_info, format_weather_line
from advanced_analysis import (
    build_core_matchup_embed,
    _analysis_embed,
    _hitters,
    _pitchers,
    _season_ops,
    _recent_ops,
    _season_whip,
    _recent_whip,
)
from manual_analysis import (
    ManualGameSelectView,
    get_manual_analysis,
    is_published,
    build_published_manual_embed,
)


# -----------------------------------------------------------------------------
# 기본 설정
# -----------------------------------------------------------------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

KST = datetime.timezone(datetime.timedelta(hours=9))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PLAYERS_FILE = os.path.join(BASE_DIR, "players.json")
PANEL_RECORD_FILE = "panel_message.json"
USAGE_LOG_FILE = "usage_logs.json"
AI_COMMENT_CACHE_FILE = "ai_comment_cache.json"
AI_COMMENT_CACHE_MAX_ENTRIES = 200
AI_COMMENT_TIMEOUT_SECONDS = 45
_AI_COMMENT_CACHE_LOCK = asyncio.Lock()
_AI_COMMENT_GENERATION_LOCKS = {}
_cached_players = None
_USAGE_FILE_LOCK = threading.Lock()
_ANALYSIS_LOCKS: dict[str, asyncio.Lock] = {}
_LAST_ANALYSIS_REQUEST: dict[str, float] = {}
_ANALYSIS_COOLDOWN_SECONDS = 2.0
MAX_CREDIT_CHARGE = 1000

# ★ 중복 분석 방지용 상태
_ANALYSIS_IN_PROGRESS: dict[str, bool] = {}


# -----------------------------------------------------------------------------
# 공통 유틸
# -----------------------------------------------------------------------------
def now_kst() -> datetime.datetime:
    return datetime.datetime.now(KST)


def today_kst() -> datetime.date:
    return now_kst().date()


def today_iso() -> str:
    return today_kst().isoformat()


def weekday_ko(d: datetime.date) -> str:
    return "월화수목금토일"[d.weekday()]


def text(v: Any, default: str = "-") -> str:
    if v is None or v == "":
        return default
    return str(v)


def num(v: Any) -> Optional[float]:
    if v is None or v == "" or v == "-":
        return None
    try:
        return float(str(v).replace("%", ""))
    except (TypeError, ValueError):
        return None


def int_num(v: Any) -> Optional[int]:
    n = num(v)
    return int(n) if n is not None else None


def first_value(*values: Any, default: Any = None) -> Any:
    for v in values:
        if v is not None and v != "" and v != "-":
            return v
    return default


def get_nested(obj: Any, keys: Iterable[str], default: Any = None) -> Any:
    if not isinstance(obj, dict):
        return default
    for key in keys:
        if key in obj and obj[key] not in (None, "", "-"):
            return obj[key]
    return default


def find_value_recursive(obj: Any, keys: Iterable[str]) -> Any:
    """report 구조가 조금 달라도 주요 지표를 최대한 찾아준다."""
    wanted = set(keys)
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in wanted and v not in (None, "", "-"):
                return v
        for v in obj.values():
            r = find_value_recursive(v, wanted)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_value_recursive(v, wanted)
            if r is not None:
                return r
    return None


def safe_side_name(side: dict) -> str:
    return text(side.get("standings", {}).get("name"), "팀")


def report_game_date(report: dict) -> str:
    info = report.get("gameInfo", {})
    return text(info.get("gdate"), today_iso())


def session_date_is_today(created_date: datetime.date) -> bool:
    return created_date == today_kst()


async def reset_to_today_games(interaction: discord.Interaction) -> bool:
    """자정이 지나면 기존 화면을 오늘 경기 선택 화면으로 되돌린다."""
    try:
        games = get_today_games()
        if not games:
            await interaction.response.edit_message(
                content="오늘 예정된 경기가 없습니다. 자정 기준으로 분석 세션이 초기화되었습니다.",
                embed=None,
                view=None,
            )
            return True
        await interaction.response.edit_message(
            content="날짜가 변경되어 분석 화면을 초기화했습니다. 오늘 경기를 선택하세요.",
            embed=None,
            view=GameSelectView(games, created_date=today_kst()),
        )
        return True
    except Exception as e:
        await interaction.response.edit_message(
            content=f"자정 초기화 중 경기 정보를 불러오지 못했습니다: {e}",
            embed=None,
            view=None,
        )
        return True


def format_game_datetime(game: dict) -> str:
    date_value = first_value(game.get("gdate"), game.get("date"), game.get("game_date"), default=today_iso())
    time_value = first_value(game.get("gtime"), game.get("time"), game.get("game_time"), default="시간 미정")
    try:
        d = datetime.date.fromisoformat(str(date_value)[:10])
        date_label = f"{d.year}.{d.month:02d}.{d.day:02d} ({weekday_ko(d)})"
    except ValueError:
        date_label = str(date_value)
    return f"{date_label} · {time_value}"


def game_status_text(game: dict) -> str:
    if not isinstance(game, dict):
        return "UNKNOWN"
    if game.get("cancel") is True:
        return "CANCEL"
    status = str(first_value(game.get("gameStatusNormalized"), game.get("statusCode"), game.get("status"), default="UNKNOWN")).upper()
    if status in {"CANCEL", "CANCELED", "CANCELLED"}: return "CANCEL"
    if status in {"LIVE", "PLAYING", "IN_PROGRESS", "STARTED"}: return "LIVE"
    if status in {"END", "ENDED", "FINAL", "FINISHED", "GAME_END"}: return "END"
    if status in {"BEFORE", "SCHEDULED", "UPCOMING", "WAIT", "READY"}: return "BEFORE"
    return status


def game_team_names(game: dict) -> Tuple[str, str]:
    away = first_value(game.get("team_a"), game.get("away"), game.get("aName"), game.get("awayTeam"), default="원정팀")
    home = first_value(game.get("team_b"), game.get("home"), game.get("hName"), game.get("homeTeam"), default="홈팀")
    return str(away), str(home)


def player_identifier(player: dict) -> Optional[str]:
    value = first_value(
        player.get("playerId"), player.get("playerID"), player.get("playerNo"),
        player.get("pId"), player.get("id"),
    )
    return str(value) if value is not None else None


def line_up_players(side: dict) -> List[dict]:
    lineup = side.get("lineup", {}) if isinstance(side, dict) else {}
    candidates = lineup.get("fullLineUp", []) or lineup.get("lineup", []) or []
    return [p for p in candidates if isinstance(p, dict) and (p.get("batorder") or p.get("playerName"))]


def sort_batters(players: List[dict]) -> List[dict]:
    def key(p: dict):
        v = int_num(p.get("batorder"))
        return (v if v is not None else 99, text(p.get("playerName"), ""))
    return sorted(players, key=key)


def collect_candidate_dicts(report: dict) -> Iterable[dict]:
    yield report
    for side_key in ("away", "home"):
        side = report.get(side_key, {})
        if isinstance(side, dict):
            yield side
            yield side.get("lineup", {})
            yield side.get("standings", {})
            yield side.get("starter", {})
            yield side.get("topPlayer", {})


# -----------------------------------------------------------------------------
# 사용 로그 / 내 정보
# -----------------------------------------------------------------------------
def load_usage_logs() -> dict:
    try:
        with open(USAGE_LOG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_usage_logs(data: dict):
    """사용 로그를 원자적으로 저장해 JSON 손상을 줄인다."""
    directory = os.path.dirname(os.path.abspath(USAGE_LOG_FILE)) or "."
    fd, temp_path = tempfile.mkstemp(prefix="usage_logs_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, USAGE_LOG_FILE)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def record_usage_log(user_id: str, away: str, home: str, game_date: str, game_time: str):
    """사용자별 누적 횟수와 최근 5회 로그를 안전하게 기록한다."""
    user_key = str(user_id)
    entry = {
        "away": str(away)[:50],
        "home": str(home)[:50],
        "date": str(game_date)[:10],
        "time": str(game_time)[:20],
        "timestamp": now_kst().isoformat(),
    }
    with _USAGE_FILE_LOCK:
        data = load_usage_logs()
        bucket = data.setdefault(user_key, {"total": 0, "logs": []})
        if isinstance(bucket, list):
            bucket = {"total": len(bucket), "logs": bucket}
            data[user_key] = bucket
        try:
            total = int(bucket.get("total", 0))
        except (TypeError, ValueError):
            total = 0
        bucket["total"] = total + 1
        logs = bucket.get("logs", [])
        if not isinstance(logs, list):
            logs = []
        logs.insert(0, entry)
        bucket["logs"] = logs[:5]
        save_usage_logs(data)


def usage_log_count(user_id: str) -> int:
    data = load_usage_logs()
    bucket = data.get(str(user_id), {})
    if isinstance(bucket, dict):
        return int(bucket.get("total", len(bucket.get("logs", []))))
    return len(bucket or [])


def get_user_credit_info(user_id: str):
    """credits_db의 반환 형태가 달라도 최대한 안전하게 읽는다."""
    try:
        status = get_status(str(user_id))
    except Exception:
        status = None

    remaining = None
    total_used = None
    if isinstance(status, dict):
        remaining = first_value(status.get("credits"), status.get("balance"), status.get("remaining"), status.get("count"))
        total_used = first_value(status.get("total_used"), status.get("used"), status.get("usage_count"), status.get("totalUsage"))
    elif isinstance(status, (tuple, list)):
        if len(status) >= 1:
            remaining = status[0]
        if len(status) >= 2:
            total_used = status[1]
    elif status is not None:
        remaining = status

    if total_used in (None, "", "-"):
        total_used = usage_log_count(user_id)
    try:
        total_used = int(total_used)
    except (TypeError, ValueError):
        total_used = usage_log_count(user_id)
    return remaining, total_used


def build_my_info_embed(user: discord.abc.User) -> discord.Embed:
    remaining, total_used = get_user_credit_info(str(user.id))
    data = load_usage_logs()
    bucket = data.get(str(user.id), {}) or {}
    logs = bucket.get("logs", []) if isinstance(bucket, dict) else bucket

    lines = []
    for log in reversed((logs or [])[-5:]):
        away = log.get("away", "원정")
        home = log.get("home", "홈")
        date = str(log.get("date", ""))
        short_date = date[5:] if len(date) >= 10 else date
        tm = log.get("time", "시간 미정")
        lines.append(f"{away} {home} {short_date} {tm}")

    embed = discord.Embed(
        title=f" {user.display_name} · 내 정보",
        description="현재 사용권과 최근 분석 사용 기록을 확인합니다.",
        color=discord.Color.from_rgb(24, 36, 52),
    )
    embed.add_field(name="남은 사용 횟수", value=text(remaining, "확인 불가"), inline=True)
    embed.add_field(name="누적 사용 횟수", value=text(total_used, "0"), inline=True)
    embed.add_field(
        name="최근 사용 로그 · 최대 5개",
        value="\n".join(lines) if lines else "사용 기록이 없습니다.",
        inline=False,
    )
    return embed


# -----------------------------------------------------------------------------
# 선수 검색
# -----------------------------------------------------------------------------
def load_players():
    global _cached_players
    if _cached_players is None:
        try:
            with open(PLAYERS_FILE, "r", encoding="utf-8") as f:
                _cached_players = json.load(f)
        except FileNotFoundError:
            _cached_players = []
    return _cached_players


def find_player_candidates(name: str, team_id: str = None, back_number=None, player_type: str = None):
    name = str(name or "").strip()
    team_id = str(team_id or "").strip()
    target_back = str(back_number).strip() if back_number not in (None, "") else ""
    target_type = str(player_type or "").strip().upper()

    players = load_players()

    def team_match(p):
        return not team_id or str(p.get("teamId", "")).strip().upper() == team_id.upper()

    def type_match(p):
        return not target_type or str(p.get("playerType", "")).strip().upper() == target_type

    exact = [p for p in players if str(p.get("playerName", "")).strip() == name and team_match(p) and type_match(p)]

    if target_back:
        numbered = [p for p in exact if str(p.get("backNumber", "")).strip() == target_back]
        if len(numbered) == 1:
            return numbered
        if numbered:
            exact = numbered

    if exact:
        return exact

    partial = [
        p for p in players
        if team_match(p) and type_match(p)
        and (name in str(p.get("playerName", "")) or str(p.get("playerName", "")) in name)
    ]
    return partial


def find_player_by_name(name: str):
    candidates = find_player_candidates(name)
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        return candidates
    return None


# ================================================================
# 한글 폰트 설정 함수 (bot.py에서도 사용)
# ================================================================
def _configure_korean_matplotlib():
    """실행 환경에 맞는 한글 폰트를 찾고, FontProperties를 반환한다."""
    import matplotlib
    from matplotlib import font_manager

    local_font = os.path.join(os.path.dirname(__file__), 'NanumGothic.ttf')
    candidates = [
        local_font,
        r"C:\Windows\Fonts\malgun.ttf",
        r"C:\Windows\Fonts\malgunsl.ttf",
        r"C:\Windows\Fonts\NanumGothic.ttf",
        r"C:\Windows\Fonts\NanumGothicBold.ttf",
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf",
        "/usr/share/fonts/truetype/unfonts-core/UnDotum.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    ]

    try:
        system_fonts = font_manager.findSystemFonts(fontext="ttf") + font_manager.findSystemFonts(fontext="ttc")
        candidates.extend(system_fonts)
    except Exception:
        pass

    seen = set()
    for path in candidates:
        if not path or path in seen or not os.path.exists(path):
            continue
        seen.add(path)
        try:
            font_manager.fontManager.addfont(path)
            prop = font_manager.FontProperties(fname=path)
            matplotlib.rcParams["font.family"] = [prop.get_name()]
            matplotlib.rcParams["font.sans-serif"] = [prop.get_name()]
            matplotlib.rcParams["axes.unicode_minus"] = False
            return prop
        except Exception:
            continue

    matplotlib.rcParams["axes.unicode_minus"] = False
    return None


def _apply_font_to_axis(ax, prop, legend=False):
    if prop is None:
        return
    ax.title.set_fontproperties(prop)
    ax.xaxis.label.set_fontproperties(prop)
    ax.yaxis.label.set_fontproperties(prop)
    for label in ax.get_xticklabels() + ax.get_yticklabels():
        label.set_fontproperties(prop)
    if legend and ax.get_legend() is not None:
        for label in ax.get_legend().get_texts():
            label.set_fontproperties(prop)


def _save_korean_chart(fig, buf, prop):
    if prop is not None:
        for ax in fig.axes:
            _apply_font_to_axis(ax, prop, legend=True)
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=160)
    return buf


def _player_context(player: dict) -> tuple[str, str, str]:
    team_id = str(player.get("_teamCode") or player.get("teamId") or player.get("teamCode") or "").strip()
    back_number = str(first_value(player.get("backNumber"), player.get("backNo"), player.get("uniformNumber"), default="") or "").strip()
    ptype = str(player.get("playerType") or "").strip().upper()
    if not ptype:
        detail_kind = str(player.get("_detail_kind") or "").strip().lower()
        if detail_kind == "bullpen" or player.get("_isBullpen"):
            ptype = "PITCHER"
        else:
            pos = str(player.get("positionName") or "").strip()
            ptype = "PITCHER" if ("투수" in pos or pos.upper() in {"P", "SP", "RP"}) else "HITTER"
    return team_id, back_number, ptype


def _resolve_player_id(player: dict) -> Optional[str]:
    pid = player_identifier(player)
    if pid:
        return pid

    name = str(player.get("playerName") or player.get("name") or "").strip()
    if not name:
        return None

    team_id, back_number, ptype = _player_context(player)
    candidates = find_player_candidates(name, team_id=team_id, back_number=back_number, player_type=ptype)
    if len(candidates) == 1:
        value = first_value(candidates[0].get("playerId"), candidates[0].get("playerID"), candidates[0].get("playerNo"), candidates[0].get("id"))
        return str(value) if value is not None else None

    candidates = find_player_candidates(name, team_id=team_id, player_type=ptype)
    if len(candidates) == 1:
        value = first_value(candidates[0].get("playerId"), candidates[0].get("playerID"), candidates[0].get("playerNo"), candidates[0].get("id"))
        return str(value) if value is not None else None
    return None

_PLAYER_SELECT_CACHE = {}

def _player_select_value(player: dict) -> str:
    pid = _resolve_player_id(player)
    if pid:
        return f"id:{pid}"
    team_id, back_number, ptype = _player_context(player)
    name = str(player.get("playerName") or player.get("name") or "").strip()
    import hashlib
    key = hashlib.sha1(f"{team_id}|{back_number}|{ptype}|{name}".encode("utf-8")).hexdigest()[:20]
    _PLAYER_SELECT_CACHE[key] = dict(player)
    return f"cache:{key}"


def _resolve_select_player(value: str) -> Optional[str]:
    if value.startswith("id:"):
        return value[3:] or None
    if value.startswith("cache:"):
        player = _PLAYER_SELECT_CACHE.get(value[6:])
        if player:
            return _resolve_player_id(player)
        return None
    return None


# -----------------------------------------------------------------------------
# 그래프 생성
# -----------------------------------------------------------------------------
def _chart_buffer(title: str, labels: List[str], values: List[float], ylabel: str) -> io.BytesIO:
    prop = _configure_korean_matplotlib()
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9, 5), dpi=160)
    bars = ax.bar(labels, values)
    ax.set_title(title, fontsize=14, fontproperties=prop)
    ax.set_ylabel(ylabel, fontproperties=prop)
    ax.grid(axis="y", alpha=0.2)
    for bar, value in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{value:.2f}",
                ha="center", va="bottom", fontsize=9, fontproperties=prop)
    for label in ax.get_xticklabels() + ax.get_yticklabels():
        label.set_fontproperties(prop)
    fig.tight_layout()
    buf = io.BytesIO()
    _save_korean_chart(fig, buf, prop)
    plt.close(fig)
    buf.seek(0)
    return buf


def _simple_metric(side: dict, keys: Iterable[str]) -> Optional[float]:
    return num(find_value_recursive(side, keys))


def make_team_metrics_chart(report: dict) -> io.BytesIO:
    prop = _configure_korean_matplotlib()
    import matplotlib.pyplot as plt
    away, home = report["away"], report["home"]
    labels = [safe_side_name(away), safe_side_name(home)]

    def team_metric(side: dict, key: str, fallback_keys: Iterable[str]):
        metrics = side.get("teamMetrics", {}) or {}
        value = metrics.get(key)
        if value not in (None, "", "-"):
            return num(value)
        return _simple_metric(side, fallback_keys)

    era = [team_metric(s, "era", ("era", "teamEra", "team_era")) for s in (away, home)]
    avg = [team_metric(s, "battingAverage", ("hra", "avg", "battingAverage", "batAvg", "teamBattingAverage")) for s in (away, home)]
    avg = [v / 100 if v is not None and v > 1 else v for v in avg]
    values_era = [v if v is not None else 0 for v in era]
    values_avg = [v * 1000 if v is not None else 0 for v in avg]

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.8), dpi=160)
    axes[0].bar(labels, values_era)
    axes[0].set_title("팀 평균자책점 (ERA)", fontproperties=prop)
    axes[0].set_ylabel("ERA", fontproperties=prop)
    axes[0].grid(axis="y", alpha=0.2)
    axes[1].bar(labels, values_avg)
    axes[1].set_title("팀 타율", fontproperties=prop)
    axes[1].set_ylabel("타율 × 1000", fontproperties=prop)
    axes[1].grid(axis="y", alpha=0.2)
    for ax in axes:
        for container in ax.containers:
            ax.bar_label(container, fmt="%.1f", padding=3, fontproperties=prop)
        for label in ax.get_xticklabels() + ax.get_yticklabels():
            label.set_fontproperties(prop)
    fig.tight_layout()
    buf = io.BytesIO()
    _save_korean_chart(fig, buf, prop)
    plt.close(fig)
    buf.seek(0)
    return buf


def recent_wdl(side: dict) -> Tuple[int, int, int]:
    w = d = l = 0
    team_name = safe_side_name(side)
    for g in side.get("previousGames", []) or []:
        result = str(g.get("result", "")).upper()
        if result.startswith("W") or "승" in result:
            w += 1
        elif result.startswith("D") or result.startswith("T") or "무" in result:
            d += 1
        elif result.startswith("L") or "패" in result:
            l += 1
        else:
            try:
                h = float(g.get("hScore"))
                a = float(g.get("aScore"))
                home_name = str(g.get("hName", ""))
                team_is_home = team_name == home_name
                team_score, opp_score = (h, a) if team_is_home else (a, h)
                if team_score > opp_score:
                    w += 1
                elif team_score == opp_score:
                    d += 1
                else:
                    l += 1
            except (TypeError, ValueError):
                pass
    return w, d, l


def make_wdl_graph(report: dict) -> io.BytesIO:
    prop = _configure_korean_matplotlib()
    import matplotlib.pyplot as plt
    sides = [report["away"], report["home"]]
    labels = [safe_side_name(s) for s in sides]
    w = [recent_wdl(s)[0] for s in sides]
    d = [recent_wdl(s)[1] for s in sides]
    l = [recent_wdl(s)[2] for s in sides]
    x = list(range(len(labels)))
    width = 0.25
    fig, ax = plt.subplots(figsize=(9, 5), dpi=160)
    ax.bar([i - width for i in x], w, width, label="승")
    ax.bar(x, d, width, label="무")
    ax.bar([i + width for i in x], l, width, label="패")
    ax.set_xticks(x, labels)
    ax.set_title("최근 경기 승 · 무 · 패", fontproperties=prop)
    ax.legend(prop=prop)
    ax.grid(axis="y", alpha=0.2)
    for label in ax.get_xticklabels() + ax.get_yticklabels():
        label.set_fontproperties(prop)
    fig.tight_layout()
    buf = io.BytesIO()
    _save_korean_chart(fig, buf, prop)
    plt.close(fig)
    buf.seek(0)
    return buf


def make_home_away_winrate_graph(report: dict) -> io.BytesIO:
    prop = _configure_korean_matplotlib()
    import matplotlib.pyplot as plt
    away, home = report["away"], report["home"]
    labels = [f"{safe_side_name(away)}\n원정", f"{safe_side_name(home)}\n홈"]
    records = [away.get("awayRecord", {}) or {}, home.get("homeRecord", {}) or {}]
    rates = []
    for record in records:
        rate = record.get("winrate")
        if rate is None:
            w = record.get("w", 0)
            total = record.get("total", 0)
            rate = (w / total * 100) if total else 0
        rates.append(float(rate))
    fig, ax = plt.subplots(figsize=(8, 4.8), dpi=160)
    bars = ax.bar(labels, rates)
    ax.set_ylim(0, 100)
    ax.set_ylabel("승률 (%)", fontproperties=prop)
    ax.set_title("홈팀 · 원정팀 승률", fontproperties=prop)
    ax.grid(axis="y", alpha=0.2)
    ax.bar_label(bars, fmt="%.1f%%", padding=3, fontproperties=prop)
    for label in ax.get_xticklabels() + ax.get_yticklabels():
        label.set_fontproperties(prop)
    fig.tight_layout()
    buf = io.BytesIO()
    _save_korean_chart(fig, buf, prop)
    plt.close(fig)
    buf.seek(0)
    return buf


# -----------------------------------------------------------------------------
# 분석 Embed
# -----------------------------------------------------------------------------
def build_matchup_summary_embed(report: dict) -> discord.Embed:
    info = report.get("gameInfo", {})
    away, home = report.get("away", {}), report.get("home", {})
    gdate = text(info.get("gdate"), today_iso())
    gtime = text(info.get("gtime"), "시간 미정")
    stadium = text(info.get("stadium"), "구장 정보 없음")

    try:
        d = datetime.date.fromisoformat(gdate[:10])
        date_label = f"{d.year}.{d.month:02d}.{d.day:02d} ({weekday_ko(d)})"
    except ValueError:
        date_label = gdate

    embed = discord.Embed(
        title=f" {text(info.get('aName'))} @ {text(info.get('hName'))}",
        description=f"**{date_label} · {gtime}**\n{stadium}\n\n경기 전 데이터 기반 종합 매치업 분석",
        color=discord.Color.from_rgb(24, 36, 52),
    )

    for side in (away, home):
        st = side.get("standings", {})
        starter = side.get("starter", {})
        s_basic = starter.get("currentSeasonStats", {})
        s_info = starter.get("playerInfo", {})
        team_name = safe_side_name(side)
        team_line = f"순위 {text(st.get('rank'))}위 · 승률 {text(st.get('wra'))} · {text(st.get('w'))}승 {text(st.get('l'))}패"
        starter_line = (
            f"**{text(s_info.get('name'))}** · ERA {text(s_basic.get('era'))} · WHIP {text(s_basic.get('whip'))} · "
            f"{text(s_basic.get('w'))}승 {text(s_basic.get('l'))}패 · K {text(s_basic.get('kk'))}"
        )
        embed.add_field(name=team_name, value=f"{team_line}\n선발 {starter_line}", inline=False)

    vs = report.get("seasonVsResult")
    if vs:
        embed.add_field(
            name="상대전적",
            value=f"{text(vs.get('hCode'))} · {text(vs.get('hw'))}승 {text(vs.get('hd'))}무 {text(vs.get('hl'))}패",
            inline=True,
        )

    w1, d1, l1 = recent_wdl(away)
    w2, d2, l2 = recent_wdl(home)
    embed.add_field(
        name="최근 흐름",
        value=f"{safe_side_name(away)} {w1}승 {d1}무 {l1}패\n{safe_side_name(home)} {w2}승 {d2}무 {l2}패",
        inline=True,
    )

    confidence = report.get("dataConfidence", {}) or {}
    if confidence:
        embed.add_field(name="데이터 신뢰도", value=f"{confidence.get('score', 0)}% · {confidence.get('grade', '확인 불가')}", inline=False)
    game_id = get_game_id_from_report(report)
    manual_item = get_manual_analysis(game_id) if game_id else None
    if manual_item and manual_item.get("status") == "published":
        embed.add_field(name="관리자 검수 분석", value="✅ 검수 완료 · 전체 리포트에서 확인할 수 있습니다.", inline=False)
    else:
        embed.add_field(name="관리자 검수 분석", value="⏳ 아직 공개된 수동 분석이 없습니다.", inline=False)
    return embed


def build_starter_detail_embed(report: dict) -> discord.Embed:
    embed = discord.Embed(title=" 선발투수 분석", description="시즌 지표 + 상대전적 + 구종 정보", color=discord.Color.green())
    for side in (report.get("away", {}), report.get("home", {})):
        starter = side.get("starter", {})
        info = starter.get("playerInfo", {})
        basic = starter.get("currentSeasonStats", {})
        vs_opp = starter.get("currentSeasonStatsOnOpponents", {})
        pit_kinds = starter.get("currentPitKindStats", []) or []
        pitch_lines = []
        for p in pit_kinds[:6]:
            speed = text(p.get("speed"), "-")
            rate = text(p.get("pit_rt"), "-")
            pitch_lines.append(f"{text(p.get('type'))} {speed}km/h · {rate}%")
        value = (
            f"ERA **{text(basic.get('era'))}** · WHIP **{text(basic.get('whip'))}**\n"
            f"{text(basic.get('w'))}승 {text(basic.get('l'))}패 · K {text(basic.get('kk'))} · BB {text(basic.get('bb'))}\n"
            f"구종: {', '.join(pitch_lines) if pitch_lines else '데이터 없음'}"
        )
        if vs_opp and vs_opp.get("gameCount"):
            value += (
                f"\n상대전: {text(vs_opp.get('gameCount'))}경기 · {text(vs_opp.get('inn'))}이닝 · "
                f"ERA {text(vs_opp.get('era'))} · {text(vs_opp.get('w'))}승 {text(vs_opp.get('l'))}패"
            )
        embed.add_field(name=f"{safe_side_name(side)} · {text(info.get('name'))}", value=value, inline=False)
    return embed


def build_lineup_embed(report: dict) -> discord.Embed:
    embed = discord.Embed(
        title=" 선발 라인업 분석",
        description="1~9번 타순 중심. 아래 선수 선택 메뉴에서 개별 상세 기록을 확인할 수 있습니다.",
        color=discord.Color.teal(),
    )
    for side in (report.get("away", {}), report.get("home", {})):
        batters = sort_batters(line_up_players(side))
        is_today = bool(side.get("lineupIsToday"))
        label = "확정 라인업" if is_today else f"참고 라인업 · {text(side.get('lineupDate'))}"
        lines = []
        for i, b in enumerate(batters[:9], start=1):
            order = int_num(b.get("batorder")) or i
            pos = text(b.get("positionName"), "-")
            name = text(b.get("playerName"), "선수")
            avg = first_value(b.get("hra"), b.get("avg"), b.get("battingAverage"))
            ops = first_value(b.get("ops"), b.get("OPS"))
            extra = ""
            if avg not in (None, "", "-"):
                extra += f" · 타율 {avg}"
            if ops not in (None, "", "-"):
                extra += f" · OPS {ops}"
            lines.append(f"**{order}번** {pos} {name}{extra}")
        embed.add_field(
            name=f"{safe_side_name(side)} · {label}",
            value="\n".join(lines) if lines else "라인업 정보 없음",
            inline=True,
        )
    return embed


def _percent_value(value):
    n = num(value)
    if n is None:
        return None
    return n * 100 if n <= 1 else n

def _stat(v, default="정보 없음"):
    return text(v, default)

def _build_comment_payload(report: dict) -> str:
    info = report.get("gameInfo", {}) or {}
    away, home = report.get("away", {}), report.get("home", {})
    lines = []
    lines.append(f"경기: {text(info.get('aName'))} @ {text(info.get('hName'))}, {text(info.get('gdate'))} {text(info.get('gtime'))}")
    for side in (away, home):
        team = safe_side_name(side)
        st = side.get("standings", {}) or {}
        starter = side.get("starter", {}) or {}
        sinfo = starter.get("playerInfo", {}) or {}
        sb = starter.get("currentSeasonStats", {}) or {}
        vs = starter.get("currentSeasonStatsOnOpponents", {}) or {}
        batters = sort_batters(line_up_players(side))[:9]
        batter_text = ", ".join(text(p.get("playerName"), "선수") for p in batters)
        metrics = side.get("teamMetrics", {}) or {}
        lines.append(
            f"[{team}] 순위 {text(st.get('rank'))}, 승률 {text(st.get('wra'))}, "
            f"팀 ERA {text(metrics.get('era'))}, 팀 타율 {text(metrics.get('battingAverage'))}, "
            f"선발 {text(sinfo.get('name'))} ERA {text(sb.get('era'))} WHIP {text(sb.get('whip'))}, "
            f"선발 상대전 ERA {text(vs.get('era'))}, 라인업 {batter_text or '정보 없음'}"
        )
        recent = side.get("recentRecord", {}) or {}
        lines.append(f"[{team}] 최근전적 {recent.get('w', 0)}승 {recent.get('d', 0)}무 {recent.get('l', 0)}패")
    absences = report.get("analysis", {}).get("absences", []) or []
    if absences:
        lines.append("결장 자료: " + " / ".join(str(x) for x in absences[:10]))
    return "\n".join(lines)

def _rule_based_ai_comment(report: dict) -> str:
    away, home = report.get("away", {}), report.get("home", {})
    paragraphs=[]
    paragraphs.append("경기 전 데이터를 기준으로 선발투수, 예상 타선, 불펜 운용, 팀 성적 지표를 종합한 분석입니다.")
    for side, other in ((away, home), (home, away)):
        team=safe_side_name(side); st=side.get("standings", {}) or {}; starter=side.get("starter", {}) or {}; sinfo=starter.get("playerInfo", {}) or {}; sb=starter.get("currentSeasonStats", {}) or {}
        recent=side.get("recentRecord", {}) or {}; metrics=side.get("teamMetrics", {}) or {}
        strengths=[]
        era=num(metrics.get("era")); avg=num(metrics.get("battingAverage")); s_era=num(sb.get("era")); opp_era=num((starter.get("currentSeasonStatsOnOpponents", {}) or {}).get("era"))
        if era is not None: strengths.append(f"팀 ERA {era:.2f}")
        if avg is not None: strengths.append(f"팀 타율 {avg:.3f}")
        if s_era is not None: strengths.append(f"선발 ERA {s_era:.2f}")
        if opp_era is not None: strengths.append(f"해당 상대전 선발 ERA {opp_era:.2f}")
        rec=f"최근 {recent.get('w',0)}승 {recent.get('d',0)}무 {recent.get('l',0)}패"
        batter_names=', '.join(text(p.get('playerName'),'선수') for p in sort_batters(line_up_players(side))[:5])
        paragraphs.append(f"{team}은 {text(sinfo.get('name'),'선발 미확인')}을 선발로 내세우며 {rec}의 최근 흐름을 보입니다. " + ("주요 지표는 " + ", ".join(strengths) + "이며, " if strengths else "현재 확인 가능한 주요 지표가 제한적이며, ") + f"상위 타선은 {batter_names or '확인되지 않음'} 중심으로 구성됩니다.")
    wA=(away.get("recentRecord",{}) or {}).get("w",0); wH=(home.get("recentRecord",{}) or {}).get("w",0)
    if wA>wH:
        form=f"최근 흐름만 놓고 보면 {safe_side_name(away)}가 상대적으로 우위입니다."
    elif wH>wA:
        form=f"최근 흐름만 놓고 보면 {safe_side_name(home)}가 상대적으로 우위입니다."
    else:
        form="최근 승수 기준으로 양 팀의 흐름은 비슷합니다."
    paragraphs.append(form + " 실제 경기에서는 선발의 이닝 소화력과 초반 득점 생산, 불펜의 연투 부담 여부가 승부에 직접적인 영향을 줄 가능성이 큽니다.")
    return "\n\n".join(paragraphs)

def build_team_recent_games_embed(report: dict) -> discord.Embed:
    embed = discord.Embed(title="최근 5경기", description="최근 경기 결과 및 흐름", color=discord.Color.dark_teal())
    for side in (report.get("away", {}), report.get("home", {})):
        games = side.get("previousGames", []) or []
        if not games:
            embed.add_field(name=safe_side_name(side), value="데이터 없음", inline=False)
            continue
        lines = []
        for g in games[:5]:
            h_name, a_name = text(g.get("hName"), "홈"), text(g.get("aName"), "원정")
            score_line = f"{h_name} {text(g.get('hScore'))} : {text(g.get('aScore'))} {a_name}"
            lines.append(f"{text(g.get('gdate'))} · {text(g.get('result'))} · {score_line}")
        embed.add_field(name=safe_side_name(side), value="\n".join(lines), inline=False)
    return embed


def build_team_form_embed(report: dict) -> discord.Embed:
    embed = discord.Embed(title="팀 지표 비교", description="승패 흐름 + ERA + 타율 등 리포트가 제공하는 지표를 한 화면에 정리", color=discord.Color.blue())
    for side in (report.get("away", {}), report.get("home", {})):
        st = side.get("standings", {})
        w, d, l = recent_wdl(side)
        era = first_value(st.get("era"), st.get("teamEra"), find_value_recursive(side, ("teamEra", "era")))
        avg = first_value(st.get("hra"), st.get("avg"), st.get("battingAverage"), find_value_recursive(side, ("battingAverage", "batAvg", "hra")))
        parts = [f"최근 {w}승 {d}무 {l}패"]
        if era not in (None, "", "-"):
            parts.append(f"ERA {era}")
        if avg not in (None, "", "-"):
            parts.append(f"타율 {avg}")
        embed.add_field(name=safe_side_name(side), value=" · ".join(parts), inline=True)
    return embed


# -----------------------------------------------------------------------------
# 선수 상세 Embed / View
# -----------------------------------------------------------------------------
def _basic_record(data: dict) -> dict:
    return (data.get("basicRecord", {}) or {}).get("basic", {}) or {}


def _player_info(data: dict) -> dict:
    return data.get("playerInfo", {}) or {}


def _is_pitcher_data(data: dict) -> bool:
    basic = _basic_record(data)
    return bool(any(k in basic for k in ("era", "whip", "inn", "kk", "bb"))) or bool(data.get("chart", {}).get("pit_kind"))


def build_basic_embed(data: dict) -> discord.Embed:
    basic = _basic_record(data)
    pinfo = _player_info(data)
    name = first_value(pinfo.get("name"), data.get("name"), default="선수 상세")
    team = first_value(data.get("teamCode"), pinfo.get("teamCode"), default="-")
    embed = discord.Embed(
        title=str(name),
        description=f"{text(data.get('year'))}시즌 · {team}",
        color=discord.Color.dark_gray(),
    )
    if _is_pitcher_data(data):
        metrics = [
            ("ERA", basic.get("era")), ("WHIP", basic.get("whip")),
            ("승-패", f"{text(basic.get('w'))}-{text(basic.get('l'))}"),
            ("이닝", basic.get("inn")), ("탈삼진", basic.get("kk")), ("볼넷", basic.get("bb")),
        ]
    else:
        metrics = [
            ("타율", first_value(basic.get("hra"), basic.get("avg"), basic.get("battingAverage"))),
            ("출루율", first_value(basic.get("obp"), basic.get("onbase"))),
            ("장타율", first_value(basic.get("slg"), basic.get("slugging"))),
            ("OPS", first_value(basic.get("ops"), basic.get("OPS"))),
            ("안타", first_value(basic.get("hit"), basic.get("hits"))),
            ("홈런", first_value(basic.get("hr"), basic.get("homeRun"))),
            ("타점", first_value(basic.get("rbi"), basic.get("RBI"))),
            ("타수", first_value(basic.get("ab"), basic.get("atBat"))),
        ]
    for label, value in metrics:
        embed.add_field(name=label, value=text(value), inline=True)
    return embed


def build_pitch_embed(data: dict) -> discord.Embed:
    embed = discord.Embed(title="구종 분석", description=f"{text(data.get('year'))}시즌 구속 및 구사율", color=discord.Color.dark_gray())
    pit_kind = data.get("chart", {}).get("pit_kind", {}).get("player", {})
    if not pit_kind:
        embed.description = "구종 데이터가 없습니다."
        return embed
    for info in pit_kind.values():
        speed = info.get("speed")
        rate = info.get("pit_rt")
        if speed and speed != "-":
            embed.add_field(name=text(info.get("pit"), "구종"), value=f"{speed}km/h · 구사율 {text(rate)}%", inline=False)
    return embed


def build_recent_games_embed(data: dict) -> discord.Embed:
    embed = discord.Embed(title="최근 경기 기록", description="최근 5경기", color=discord.Color.dark_gray())
    games = data.get("record", {}).get("game", [])[:5]
    if not games:
        embed.description = "경기 기록이 없습니다."
        return embed
    for g in games:
        if _is_pitcher_data(data):
            value = f"{text(g.get('inn'))}이닝 · 실점 {text(g.get('r'))} · 자책 {text(g.get('er'))} · K {text(g.get('kk'))} · ERA {text(g.get('era'))}"
        else:
            value = f"안타 {text(g.get('hit'))} · 타수 {text(g.get('ab'))} · 타율 {text(g.get('hra'))} · HR {text(g.get('hr'))} · 타점 {text(g.get('rbi'))}"
        embed.add_field(name=f"{text(g.get('gday'))} vs {text(g.get('opponent'))}", value=value, inline=False)
    return embed


def build_vs_team_embed(data: dict) -> discord.Embed:
    embed = discord.Embed(title="팀별 상대전적", color=discord.Color.dark_gray())
    vs_teams = data.get("vsTeam", {}).get("vsteam", [])
    if not vs_teams:
        embed.description = "상대전적 데이터가 없습니다."
        return embed
    for v in vs_teams:
        if _is_pitcher_data(data):
            value = f"ERA {text(v.get('era'))} · {text(v.get('w'))}승 {text(v.get('l'))}패 · K {text(v.get('kk'))}"
        else:
            value = f"타율 {text(v.get('hra'))} · HR {text(v.get('hr'))} · 안타 {text(v.get('hit'))}"
        embed.add_field(name=f"vs {text(v.get('name'))}", value=value, inline=True)
    return embed


class PlayerMenuView(View):
    def __init__(self, player_data: dict):
        super().__init__(timeout=None)
        self.player_data = player_data

    @discord.ui.button(label="기본 기록", style=discord.ButtonStyle.secondary, custom_id="kbo_player_basic")
    async def basic_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.edit_message(embed=build_basic_embed(self.player_data), view=self)

    @discord.ui.button(label="구종 분석", style=discord.ButtonStyle.secondary, custom_id="kbo_player_pitch")
    async def pitch_button(self, interaction: discord.Interaction, button: Button):
        if not _is_pitcher_data(self.player_data):
            await interaction.response.send_message("해당 선수는 투수 구종 데이터가 없습니다.", ephemeral=True)
            return
        await interaction.response.edit_message(embed=build_pitch_embed(self.player_data), view=self)

    @discord.ui.button(label="최근 경기", style=discord.ButtonStyle.secondary, custom_id="kbo_player_recent")
    async def recent_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.edit_message(embed=build_recent_games_embed(self.player_data), view=self)

    @discord.ui.button(label="상대전적", style=discord.ButtonStyle.secondary, custom_id="kbo_player_vs")
    async def vsteam_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.edit_message(embed=build_vs_team_embed(self.player_data), view=self)


class PlayerSelect(Select):
    def __init__(self, players: List[dict]):
        options=[]
        for p in players[:25]:
            pid=first_value(p.get("playerId"), p.get("playerID"), p.get("playerNo"), p.get("id"))
            if pid is not None:
                options.append(discord.SelectOption(label=text(p.get("playerName"), "선수"), value=str(pid)))
        super().__init__(placeholder="선수를 선택하세요.", min_values=1, max_values=1, options=options, custom_id="kbo_player_search_select")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            data = get_player_data(str(self.values[0]))
            await interaction.followup.send(embed=build_basic_embed(data), view=PlayerMenuView(data), ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"선수 데이터를 불러오지 못했습니다: {e}", ephemeral=True)


class PlayerSelectView(View):
    def __init__(self, players: List[dict]):
        super().__init__(timeout=None)
        self.add_item(PlayerSelect(players))


# -----------------------------------------------------------------------------
# 경기 분석 View
# -----------------------------------------------------------------------------
def _starter_identity(side: dict) -> tuple[str, str]:
    starter = side.get("starter", {}) or {}
    info = starter.get("playerInfo", {}) or {}
    return str(first_value(info.get("name"), starter.get("name"), default="")).strip(), str(first_value(info.get("playerId"), info.get("playerID"), info.get("id"), default="")).strip()


class PlayerDetailButton(Button):
    def __init__(self, player: dict, label: str, detail_kind: str, row: int = 0):
        name = str(player.get("playerName") or player.get("name") or "선수").strip()
        digest = hashlib.sha1(f"{detail_kind}:{name}:{player.get('playerId')}:{player.get('playerNo')}".encode("utf-8")).hexdigest()[:16]
        super().__init__(label=label[:80], style=discord.ButtonStyle.secondary, custom_id=f"kbo_detail_{detail_kind}_{digest}", row=row)
        self.player = dict(player)
        self.player["_detail_kind"] = detail_kind
        if detail_kind == "bullpen":
            self.player["_isBullpen"] = True
            self.player["playerType"] = "PITCHER"
        self.detail_kind = detail_kind

    async def callback(self, interaction: discord.Interaction):
        pid = _resolve_player_id(self.player)
        if not pid:
            team_id, back_number, ptype = _player_context(self.player)
            name = str(self.player.get("playerName") or self.player.get("name") or "").strip()
            candidates = find_player_candidates(name, team_id=team_id, back_number=back_number, player_type=ptype)
            if len(candidates) == 1:
                pid = str(first_value(candidates[0].get("playerId"), candidates[0].get("playerID"), candidates[0].get("playerNo"), candidates[0].get("id")) or "")
            if not pid:
                candidates = find_player_candidates(name, team_id=team_id, player_type=ptype)
                if len(candidates) == 1:
                    pid = str(first_value(candidates[0].get("playerId"), candidates[0].get("playerID"), candidates[0].get("playerNo"), candidates[0].get("id")) or "")
        if not pid:
            await interaction.response.send_message(
                f"선수 ID를 확인하지 못했습니다. ({self.player.get('playerName', '선수')})",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            data = get_player_data(pid)
            await interaction.followup.send(embed=build_basic_embed(data), view=PlayerMenuView(data), ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"선수 데이터를 불러오지 못했습니다: {e}", ephemeral=True)


class PlayerButtonView(View):
    def __init__(self, players: List[dict], detail_kind: str, title: str):
        super().__init__(timeout=None)
        self.title = title
        for index, player in enumerate(players[:24]):
            team = str(player.get("_teamName") or "")
            order = player.get("batorder")
            name = str(player.get("playerName") or player.get("name") or "선수")
            suffix = ""
            if detail_kind == "lineup" and order:
                suffix = f" {order}번"
            if detail_kind == "bullpen" and player.get("isStarter"):
                suffix = " (선발)"
            label = f"{team} · {name}{suffix}" if team else f"{name}{suffix}"
            self.add_item(PlayerDetailButton(player, label, detail_kind, row=min(index // 5, 4)))


class PlayerButtonInfoView(View):
    def __init__(self, report: dict, detail_kind: str):
        super().__init__(timeout=None)
        self.report = report
        self.detail_kind = detail_kind
        players=[]
        if detail_kind == "lineup":
            for side in (report.get("away", {}), report.get("home", {})):
                for p in sort_batters(line_up_players(side))[:9]:
                    item=dict(p); item["_teamName"]=safe_side_name(side); players.append(item)
            title="타자 상세 선택"
        else:
            for side in (report.get("away", {}), report.get("home", {})):
                for p in (side.get("bullpenAnalysis", []) or [])[:12]:
                    item=dict(p); item["_teamName"]=safe_side_name(side); item["_detail_kind"]="bullpen"; item["_isBullpen"]=True; item["playerType"]="PITCHER"; players.append(item)
            title="불펜 투수 상세 선택"
        for index, player in enumerate(players[:24]):
            team = str(player.get("_teamName") or "")
            order = player.get("batorder")
            name = str(player.get("playerName") or player.get("name") or "선수")
            suffix = f" {order}번" if detail_kind == "lineup" and order else ""
            if detail_kind == "bullpen" and player.get("isStarter"):
                suffix = " (선발)"
            label = f"{team} · {name}{suffix}" if team else f"{name}{suffix}"
            self.add_item(PlayerDetailButton(player, label, detail_kind, row=min(index // 5,4)))


class BatterDetailSelect(Select):
    def __init__(self, report: dict):
        self.report = report
        options = []
        for side in (report.get("away", {}), report.get("home", {})):
            for p in sort_batters(line_up_players(side))[:9]:
                name = text(p.get("playerName"), "선수")
                value = _player_select_value(p)
                options.append(
                    discord.SelectOption(
                        label=f"{safe_side_name(side)} · {text(p.get('batorder'))}번 {name}"[:100],
                        value=value[:100],
                        description="타자 상세 정보",
                    )
                )
        seen = set(); unique = []
        for option in options:
            if option.value not in seen:
                seen.add(option.value); unique.append(option)
        super().__init__(
            placeholder="타자를 선택하면 상세 정보가 표시됩니다.",
            min_values=1, max_values=1, options=unique[:25],
            custom_id="kbo_batter_detail_select",
        )

    async def callback(self, interaction: discord.Interaction):
        pid = _resolve_select_player(self.values[0])
        if not pid:
            await interaction.response.send_message("선수 ID를 찾을 수 없어 상세 정보를 불러올 수 없습니다.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            data = get_player_data(pid)
            await interaction.followup.send(embed=build_basic_embed(data), view=PlayerMenuView(data), ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"타자 데이터를 불러오지 못했습니다: {e}", ephemeral=True)


class BullpenDetailSelect(Select):
    def __init__(self, report: dict):
        options = []
        for side in (report.get("away", {}), report.get("home", {})):
            for raw in (side.get("bullpenAnalysis", []) or [])[:12]:
                p = dict(raw)
                p["_detail_kind"] = "bullpen"; p["_isBullpen"] = True; p["playerType"] = "PITCHER"
                name = text(p.get("playerName"), "투수")
                value = _player_select_value(p)
                suffix = " (선발)" if p.get("isStarter") else ""
                details = []
                if p.get("_appearanceCount") is not None:
                    try: details.append(f"{int(float(p.get('_appearanceCount')))}경기")
                    except (TypeError, ValueError): pass
                if p.get("_innings") is not None:
                    try: details.append(f"{float(p.get('_innings')):.1f}이닝")
                    except (TypeError, ValueError): pass
                era = first_value(p.get("era"), p.get("ERA"))
                if era not in (None, "", "-"): details.append(f"ERA {era}")
                options.append(discord.SelectOption(label=f"{safe_side_name(side)} · {name}{suffix}"[:100], value=value[:100], description=(" · ".join(details) if details else "상세 정보 보기")[:100]))
        seen=set(); unique=[]
        for option in options:
            if option.value not in seen:
                seen.add(option.value); unique.append(option)
        super().__init__(placeholder="불펜 투수를 선택하세요.", min_values=1, max_values=1, options=unique[:25], custom_id="kbo_bullpen_detail_select")

    async def callback(self, interaction: discord.Interaction):
        pid = _resolve_select_player(self.values[0])
        if not pid:
            await interaction.response.send_message("투수 ID를 찾을 수 없어 상세 정보를 불러올 수 없습니다.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True)
        try:
            data = get_player_data(pid)
            await interaction.followup.send(embed=build_basic_embed(data), view=PlayerMenuView(data), ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"불펜 투수 데이터를 불러오지 못했습니다: {e}", ephemeral=True)


def _add_select_to_matchup_view(view: "MatchupMenuView", select: Select):
    if select.options:
        view.add_item(select)


def build_lineup_view(report: dict, created_date: datetime.date):
    view = MatchupMenuView(report, created_date=created_date)
    _add_select_to_matchup_view(view, BatterDetailSelect(report))
    return view


def build_bullpen_view(report: dict, created_date: datetime.date):
    view = MatchupMenuView(report, created_date=created_date)
    _add_select_to_matchup_view(view, BullpenDetailSelect(report))
    return view


def build_ai_comment_text(report: dict) -> str:
    away, home = report.get("away", {}), report.get("home", {})
    def side_block(side):
        st=side.get("standings", {}) or {}
        starter=side.get("starter", {}) or {}
        si=starter.get("playerInfo", {}) or {}
        ss=starter.get("currentSeasonStats", {}) or {}
        batters=sort_batters(line_up_players(side))[:9]
        names=", ".join(text(x.get("playerName")) for x in batters)
        metrics=side.get("teamMetrics", {}) or {}
        w,d,l=recent_wdl(side)
        return {
            "team": safe_side_name(side),
            "starter": text(si.get("name")),
            "era": text(ss.get("era")),
            "whip": text(ss.get("whip")),
            "team_era": text(metrics.get("era")),
            "avg": text(metrics.get("battingAverage")),
            "recent": f"{w}승 {d}무 {l}패",
            "batters": names,
        }
    a,h=side_block(away),side_block(home)
    absences=report.get("analysis",{}).get("absences",[]) or []
    comments=report.get("analysis",{}).get("comments",[]) or []
    points=[]
    points.append(f"{a['team']}는 선발 {a['starter']} (ERA {a['era']}, WHIP {a['whip']})를 내세운다. 타선은 {a['batters'] or '확인 가능한 선발 타선 없음'}으로 구성된다.")
    points.append(f"{h['team']}는 선발 {h['starter']} (ERA {h['era']}, WHIP {h['whip']})를 내세운다. 타선은 {h['batters'] or '확인 가능한 선발 타선 없음'}으로 구성된다.")
    points.append(f"최근 흐름은 {a['team']} {a['recent']}, {h['team']} {h['recent']}이다.")
    if a['team_era'] != '-' or h['team_era'] != '-':
        points.append(f"팀 투수력 지표는 {a['team']} ERA {a['team_era']}, {h['team']} ERA {h['team_era']}이며, 팀 타율은 각각 {a['avg']}, {h['avg']}이다.")
    if absences:
        points.append("확인된 결장/부상 정보: " + "; ".join(str(x) for x in absences[:5]) + ".")
    if comments:
        points.append("네이버 프리뷰 코멘트 참고사항: " + "; ".join(str(x) for x in comments[:3]) + ".")
    points.append("종합적으로 선발 안정성, 상위 타선의 생산력, 최근 흐름을 함께 비교하는 것이 핵심이다. 단순 승패 예측보다 실제 라인업 확정 여부와 당일 투수 운용 변수를 우선 확인해야 한다.")
    return "\n\n".join(points)


def build_comment_source_text(report: dict) -> str:
    away, home = report.get("away", {}), report.get("home", {})
    confidence = report.get("dataConfidence", {}) or {}
    weather = report.get("weather") or {}
    game_info = report.get("gameInfo", {}) or {}

    def side_block(side: dict, label: str) -> str:
        st = side.get("standings", {}) or {}
        starter = side.get("starter", {}) or {}
        si = starter.get("playerInfo", {}) or {}
        ss = starter.get("currentSeasonStats", {}) or {}
        vs_opp = starter.get("currentSeasonStatsOnOpponents", {}) or {}
        metrics = side.get("teamMetrics", {}) or {}
        batters = sort_batters(line_up_players(side))[:9]
        bullpen = side.get("bullpenAnalysis", []) or []
        w, d, l = recent_wdl(side)

        batting_lines = []
        for b in batters:
            hand = b.get("_battingHand") or b.get("battingHand") or "미확인"
            batting_lines.append(
                f"{b.get('batorder')}번 {text(b.get('playerName'))} "
                f"타격방향={hand} 포지션={text(b.get('positionName'))}"
            )

        bullpen_lines = []
        for p in bullpen[:8]:
            workload = p.get("_recentWorkload") or {}
            bullpen_lines.append(
                f"{text(p.get('playerName'))}"
                f"{'(선발)' if p.get('isStarter') else ''} "
                f"등판={p.get('_appearanceCount', 0)}경기 "
                f"이닝={p.get('_innings', 0)} "
                f"ERA={text(p.get('era'), '-')}"
                f" 피로도={p.get('_fatigueGrade', '판정 보류')} "
                f"최근3일={workload.get('recent3DayInnings', 0)}이닝 "
                f"최근7일={workload.get('recent7DayInnings', 0)}이닝"
            )

        hand_match = side.get("handednessMatchup", {}) or {}

        return (
            f"[{label}]\n"
            f"팀={safe_side_name(side)}\n"
            f"순위={text(st.get('rank'))}; 승률={text(st.get('wra'))}; "
            f"시즌전적={text(st.get('w'))}승 {text(st.get('l'))}패\n"
            f"최근={w}승 {d}무 {l}패\n"
            f"팀ERA={text(metrics.get('era'), '-')}; 팀타율={text(metrics.get('battingAverage'), '-')}\n"
            f"선발={text(si.get('name'))}; 선발ERA={text(ss.get('era'), '-')}; "
            f"선발WHIP={text(ss.get('whip'), '-')}; 선발승패={text(ss.get('w'))}-{text(ss.get('l'))}; "
            f"선발K={text(ss.get('kk'), '-')}; 선발BB={text(ss.get('bb'), '-')}\n"
            f"선발상대전적={text(vs_opp.get('gameCount'), 0)}경기 "
            f"ERA={text(vs_opp.get('era'), '-')}; "
            f"승패={text(vs_opp.get('w'), '-')}-{text(vs_opp.get('l'), '-')}\n"
            f"1~9번 타선:\n- " + "\n- ".join(batting_lines) + "\n"
            f"불펜:\n- " + ("\n- ".join(bullpen_lines) if bullpen_lines else "정보 없음") + "\n"
            f"좌우타 상성: 상대 선발 투구손={text(hand_match.get('pitcherHand'), '미확인')}; "
            f"좌타={hand_match.get('leftCount', 0)}명; 우타={hand_match.get('rightCount', 0)}명; "
            f"미확인={hand_match.get('unknownCount', 0)}명"
        )

    absences = report.get("analysis", {}).get("absences", []) or []
    preview_comments = report.get("analysis", {}).get("comments", []) or []
    vs = report.get("seasonVsResult", {}) or {}

    return (
        f"[경기]\n"
        f"{text(game_info.get('gdate'))} {text(game_info.get('gtime'))} · "
        f"{text(game_info.get('aName'))} @ {text(game_info.get('hName'))}\n"
        f"구장={text(game_info.get('stadium'), '미확인')}\n"
        f"홈/원정 구분: 홈={text(game_info.get('hName'))}, 원정={text(game_info.get('aName'))}\n\n"
        + side_block(away, "원정팀")
        + "\n\n"
        + side_block(home, "홈팀")
        + "\n\n"
        f"[시즌 상대전적]\n"
        f"{text(vs.get('hCode'))} {text(vs.get('hw'))}승 {text(vs.get('hd'))}무 {text(vs.get('hl'))}패\n\n"
        f"[결장/부상]\n"
        f"{'; '.join(map(str, absences[:15])) if absences else '확인된 결장 데이터 없음'}\n\n"
        f"[네이버 프리뷰 코멘트]\n"
        f"{'; '.join(map(str, preview_comments[:10])) if preview_comments else '없음'}\n\n"
        f"[날씨]\n"
        f"{weather if weather else '확인되지 않음'}\n\n"
        f"[데이터 신뢰도]\n"
        f"전체={confidence.get('score', '미확인')}% · 등급={confidence.get('grade', '미확인')}\n"
        f"항목={confidence.get('checks', {})}"
    )

def build_fallback_comment(report: dict) -> str:
    away, home = report.get("away", {}), report.get("home", {})
    a_start = text((away.get("starter", {}).get("playerInfo", {}) or {}).get("name"))
    h_start = text((home.get("starter", {}).get("playerInfo", {}) or {}).get("name"))

    def team_summary(side: dict) -> str:
        m = side.get("teamMetrics", {}) or {}
        bp = side.get("bullpenAnalysis", []) or []
        high = sum(1 for p in bp if p.get("_fatigueGrade") == "높음")
        w, d, l = recent_wdl(side)
        return (
            f"{safe_side_name(side)}는 최근 {w}승 {d}무 {l}패 흐름이며 "
            f"팀 ERA {text(m.get('era'), '-')}, 팀 타율 {text(m.get('battingAverage'), '-')}. "
            f"최근 피로도 높음 불펜 투수가 {high}명 확인된다."
        )

    return (
        f"선발 매치업에서는 {safe_side_name(away)}가 {a_start}, "
        f"{safe_side_name(home)}가 {h_start}를 선발로 내세운다.\n\n"
        f"{team_summary(away)}\n{team_summary(home)}\n\n"
        "타선은 현재 확인 가능한 1~9번 라인업과 팀 타율을 기준으로 비교하며, "
        "라인업 미확정 또는 결장 데이터가 부족한 경우 해당 변수의 영향은 보수적으로 판단한다.\n\n"
        "경기 후반에는 최근 등판 부담이 높은 불펜의 가용성이 중요한 변수다. "
        "최종 판단에서는 선발 안정성, 상위 타선 생산력, 불펜 피로도와 당일 결장 변수를 함께 고려해야 한다."
    )

def _load_ai_comment_cache() -> dict:
    try:
        with open(AI_COMMENT_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_ai_comment_cache(data: dict):
    directory = os.path.dirname(os.path.abspath(AI_COMMENT_CACHE_FILE)) or "."
    fd, temp_path = tempfile.mkstemp(prefix="ai_comment_cache_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, AI_COMMENT_CACHE_FILE)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _ai_comment_cache_key(report: dict, source: str) -> str:
    info = report.get("gameInfo", {}) or {}
    game_id = str(report.get("gameId") or info.get("gameId") or "")
    source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return f"v12:{game_id}:{source_hash}"


async def _get_ai_generation_lock(cache_key: str) -> asyncio.Lock:
    async with _AI_COMMENT_CACHE_LOCK:
        lock = _AI_COMMENT_GENERATION_LOCKS.get(cache_key)
        if lock is None:
            lock = asyncio.Lock()
            _AI_COMMENT_GENERATION_LOCKS[cache_key] = lock
        return lock


async def generate_ai_comment(report: dict) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return build_fallback_comment(report)

    source = build_comment_source_text(report)
    cache_key = _ai_comment_cache_key(report, source)

    async with _AI_COMMENT_CACHE_LOCK:
        cache = _load_ai_comment_cache()
        cached = cache.get(cache_key)
        if isinstance(cached, dict) and cached.get("text"):
            return str(cached["text"])

    generation_lock = await _get_ai_generation_lock(cache_key)
    async with generation_lock:
        async with _AI_COMMENT_CACHE_LOCK:
            cache = _load_ai_comment_cache()
            cached = cache.get(cache_key)
            if isinstance(cached, dict) and cached.get("text"):
                return str(cached["text"])

        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key)
            prompt = (
                "너는 KBO 경기 프리뷰를 작성하는 분석 담당자다. 다음 제공 데이터만 근거로 전문적인 한국어 경기 분석을 작성하라.\n"
                "중요 원칙:\n"
                "1) 데이터에 없는 선수 성적, 부상, 날씨, 승률, 확률을 만들지 말 것.\n"
                "2) 참고 라인업은 확정 라인업처럼 표현하지 말 것.\n"
                "3) 미확인 항목은 '확인되지 않음' 또는 '판정 보류'로 명시할 것.\n"
                "4) 승리확률 숫자를 임의로 생성하지 말 것.\n"
                "5) 단순 수치 나열보다 경기에서 실제로 영향을 줄 연결고리를 설명할 것.\n\n"
                "분석 형식:\n"
                "## 1. 경기 개요\n"
                "홈/원정과 데이터 신뢰도를 간단히 정리한다.\n"
                "## 2. 선발 매치업\n"
                "ERA, WHIP, 승패, K/BB, 상대전적과 구종 정보를 비교하고 이닝 소화 및 실점 억제 차이를 설명한다.\n"
                "## 3. 타선 매치업\n"
                "1~9번 라인업, 팀 타율, 핵심 타자와 좌우타 정보를 사용한다. 실제 확인된 좌우타 정보만 사용한다.\n"
                "## 4. 불펜 운용\n"
                "등판 빈도, 최근 3일/7일 이닝, 피로도와 선발의 이닝 소화 가능성을 연결한다.\n"
                "## 5. 결장·구장·날씨\n"
                "확인된 정보가 실제 경기 양상에 영향을 줄 때만 설명한다.\n"
                "## 6. 최근 흐름·상대전적\n"
                "최근 승무패와 시즌 상대전적을 현재 매치업과 연결한다.\n"
                "## 7. 핵심 변수 3개\n"
                "데이터로 근거가 있는 변수 3개만 제시한다.\n"
                "마지막에는 종합 의견을 2~3문장으로 작성하되 확정적인 승부 예측 대신 어느 요소가 어느 팀에 유리한지를 설명한다.\n"
                "전체 분량은 800~1400자 정도로 작성한다.\n\n"
                + source
            )
            response = await asyncio.wait_for(
                asyncio.to_thread(client.responses.create, model="gpt-5.6", input=prompt),
                timeout=AI_COMMENT_TIMEOUT_SECONDS,
            )
            output = getattr(response, "output_text", None)
            if output and str(output).strip():
                result = str(output).strip()
                async with _AI_COMMENT_CACHE_LOCK:
                    cache = _load_ai_comment_cache()
                    cache[cache_key] = {
                        "text": result,
                        "gameId": str(report.get("gameId") or ""),
                        "createdAt": now_kst().isoformat(),
                    }
                    items = sorted(
                        cache.items(),
                        key=lambda item: str((item[1] or {}).get("createdAt", "")),
                        reverse=True,
                    )[:AI_COMMENT_CACHE_MAX_ENTRIES]
                    _save_ai_comment_cache(dict(items))
                return result
        except Exception as e:
            print(f"[AI 코멘트] 호출 실패: {e}")

    return build_fallback_comment(report)


async def build_comment_embed(report: dict) -> discord.Embed:
    info = report.get("gameInfo", {}) or {}
    embed = discord.Embed(title="경기 코멘트", color=discord.Color.dark_gray())
    embed.description = await generate_ai_comment(report)
    embed.set_footer(text=f"{text(info.get('gdate'))} {text(info.get('gtime'))} · 데이터 기반 분석")
    return embed

async def build_absence_stadium_embed(report: dict) -> discord.Embed:
    info = report.get("gameInfo", {}) or {}
    embed = discord.Embed(title="결장 · 구장", color=discord.Color.dark_gray())
    absences = report.get("analysis", {}).get("absences", []) or []
    away = text(info.get("aName"), "원정팀")
    home = text(info.get("hName"), "홈팀")
    stadium_name = text(info.get("stadium"), "구장 정보 없음")
    stadium = get_stadium_info(stadium_name)

    weather = await get_venue_weather(stadium_name, info.get("gdate"), info.get("gtime"))
    venue_lines = [
        f"경기: {away} @ {home}",
        f"홈팀: {home}",
        f"원정팀: {away}",
        f"구장: {stadium.get('name', stadium_name)}",
        f"홈구단: {stadium.get('home_team', home)}",
        f"도시: {stadium.get('city', '정보 없음')}",
        f"일정: {text(info.get('gdate'))} {text(info.get('gtime'))}",
        format_weather_line(weather),
    ]
    embed.add_field(name="경기장 및 환경", value="\n".join(venue_lines), inline=False)
    embed.add_field(name="결장 및 부상", value="\n".join(f"- {x}" for x in absences) if absences else "확인된 결장자 정보 없음", inline=False)
    return embed

def build_bullpen_embed(report: dict) -> discord.Embed:
    embed = discord.Embed(title="불펜 운용", description="확인 가능한 시즌 기록만 표시", color=discord.Color.dark_gray())
    for side in (report.get("away", {}), report.get("home", {})):
        bullpen = side.get("bullpenAnalysis", []) or []
        lines = []
        for p in bullpen[:12]:
            name = text(p.get("playerName"), "투수")
            suffix = " (선발)" if p.get("isStarter") else ""
            details = []
            apps = p.get("_appearanceCount")
            inn = p.get("_innings")
            era = first_value(p.get("era"), p.get("ERA"))
            if apps is not None:
                try: details.append(f"{int(float(apps))}경기")
                except (TypeError, ValueError): pass
            if inn is not None:
                try: details.append(f"{float(inn):.1f}이닝")
                except (TypeError, ValueError): pass
            if era not in (None, "", "-"): details.append(f"ERA {era}")
            lines.append(f"{name}{suffix} — {' · '.join(details) if details else name}")
        embed.add_field(name=safe_side_name(side), value="\n".join(lines) if lines else "불펜 데이터 없음", inline=False)
    return embed

def build_top_player_embed(report: dict)->discord.Embed:
    embed=discord.Embed(title="핵심 타자",description="네이버 프리뷰에서 제공하는 핵심 타자 데이터",color=discord.Color.dark_gray())
    for side in (report.get("away",{}),report.get("home",{})):
        tp=side.get("topPlayer") or {}
        info=tp.get("playerInfo",{}) or {}
        season=tp.get("currentSeasonStats",{}) or {}
        recent=tp.get("recentFiveGamesStats",{}) or {}
        vs=tp.get("currentSeasonStatsOnOpponents",{}) or {}
        name=text(info.get("name"),"-" )
        value=f"타율 {text(season.get('hra'))} · HR {text(season.get('hr'))} · 타점 {text(season.get('rbi'))}\n최근 5경기 타율 {text(recent.get('hra'))} · {text(recent.get('hit'))}/{text(recent.get('ab'))} · HR {text(recent.get('hr'))}"
        if vs: value += f"\n상대전 타율 {text(vs.get('hra'))} · HR {text(vs.get('hr'))}"
        embed.add_field(name=f"{safe_side_name(side)} · {name}",value=value,inline=False)
    return embed



def build_data_confidence_embed(report: dict) -> discord.Embed:
    confidence = report.get("dataConfidence", {}) or {}
    score = confidence.get("score", 0)
    grade = confidence.get("grade", "확인 불가")
    checks = confidence.get("checks", {}) or {}
    home = confidence.get("home", {}) or {}
    away = confidence.get("away", {}) or {}

    embed = discord.Embed(
        title="데이터 신뢰도",
        description=f"**{score}% · {grade}**\n분석에 실제로 확보된 데이터의 범위를 표시합니다.",
        color=discord.Color.dark_gray(),
    )
    embed.add_field(
        name="핵심 데이터",
        value=(
            f"홈 라인업: {home.get('lineup', '미확인')}\n"
            f"원정 라인업: {away.get('lineup', '미확인')}\n"
            f"홈 선발: {home.get('starter', '미확인')}\n"
            f"원정 선발: {away.get('starter', '미확인')}\n"
            f"홈 불펜: {home.get('bullpen', '미확인')}\n"
            f"원정 불펜: {away.get('bullpen', '미확인')}"
        ),
        inline=False,
    )
    items = list(checks.items())
    for i in range(0, len(items), 8):
        chunk = items[i:i+8]
        embed.add_field(
            name="데이터 점검" if i == 0 else "추가 점검",
            value="\n".join(f"{k}: {'확보' if v else '미확인'}" for k, v in chunk),
            inline=False,
        )
    if score < 75:
        embed.set_footer(text="데이터가 부족한 항목은 분석에서 과도한 가중치를 주지 않습니다.")
    else:
        embed.set_footer(text="확보된 데이터 범위 내에서 분석합니다.")
    return embed


def make_radar_chart(report: dict) -> io.BytesIO:
    import matplotlib.pyplot as plt
    import numpy as np
    prop = _configure_korean_matplotlib()
    away = report.get("away", {}) or {}
    home = report.get("home", {}) or {}
    categories = ["승률", "팀ERA", "팀타율", "최근흐름", "선발ERA"]

    def values(side):
        st = side.get("standings", {}) or {}
        metrics = side.get("teamMetrics", {}) or {}
        starter = side.get("starter", {}) or {}
        sb = starter.get("currentSeasonStats", {}) or {}
        wra = num(st.get("wra")) or 0.0
        if wra > 1: wra /= 100
        era = num(metrics.get("era")) or 5.0
        avg = num(metrics.get("battingAverage")) or num(metrics.get("hra")) or 0.25
        if avg > 1: avg /= 100
        w, d, l = recent_wdl(side)
        recent = w / (w+d+l) if (w+d+l) else 0.0
        s_era = num(sb.get("era")) or 5.0
        return [
            max(0.0, min(1.0, wra)),
            max(0.0, min(1.0, (5-era)/5)),
            max(0.0, min(1.0, avg*3)),
            max(0.0, min(1.0, recent)),
            max(0.0, min(1.0, (5-s_era)/5)),
        ]

    av = values(away); hv = values(home)
    angles = np.linspace(0, 2*np.pi, len(categories), endpoint=False).tolist()
    av += av[:1]; hv += hv[:1]; angles += angles[:1]
    fig, ax = plt.subplots(figsize=(6.5,6.5), subplot_kw={"polar":True}, dpi=160)
    ax.plot(angles, av, linewidth=2, label=safe_side_name(away))
    ax.fill(angles, av, alpha=0.18)
    ax.plot(angles, hv, linewidth=2, label=safe_side_name(home))
    ax.fill(angles, hv, alpha=0.18)
    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(categories, fontproperties=prop)
    ax.set_yticklabels([])
    ax.set_title("종합 지표 비교", fontproperties=prop, fontsize=14, pad=20)
    ax.legend(loc="upper right", bbox_to_anchor=(1.35, 1.1), prop=prop)
    fig.tight_layout()
    buf=io.BytesIO()
    _save_korean_chart(fig, buf, prop)
    plt.close(fig)
    buf.seek(0)
    return buf


def _advanced_team_snapshot(side: dict) -> dict:
    hitters = _hitters(side)
    pitchers = _pitchers(side)

    def avg(values):
        values = [v for v in values if v is not None]
        return sum(values) / len(values) if values else None

    season_ops = [ _season_ops(p) for p in hitters ]
    recent_ops = [ _recent_ops(p, 10)[0] for p in hitters ]
    season_whip = [ _season_whip(p) for p in pitchers ]
    recent_whip = [ _recent_whip(p, 10)[0] for p in pitchers ]

    w, d, l = recent_wdl(side)
    metrics = side.get("teamMetrics", {}) or {}
    standings = side.get("standings", {}) or {}
    starter = side.get("starter", {}) or {}
    starter_info = starter.get("playerInfo", {}) or {}
    starter_stats = starter.get("currentSeasonStats", {}) or {}

    return {
        "team": safe_side_name(side),
        "season_ops": avg(season_ops),
        "recent_ops": avg(recent_ops),
        "season_whip": avg(season_whip),
        "recent_whip": avg(recent_whip),
        "recent_wdl": (w, d, l),
        "rank": int_num(standings.get("rank")),
        "winrate": num(standings.get("wra")),
        "team_era": num(metrics.get("era")),
        "team_avg": num(metrics.get("battingAverage")),
        "starter_name": text(starter_info.get("name"), "선발 미확인"),
        "starter_era": num(starter_stats.get("era")),
        "starter_whip": num(starter_stats.get("whip")),
        "lineup_count": len(hitters),
        "bullpen_count": len(pitchers),
    }


def _fmt_change(season, recent, digits=3, higher_is_better=True):
    if season is None or recent is None:
        return "데이터 부족"
    delta = recent - season
    sign = "+" if delta > 0 else ""
    if higher_is_better:
        direction = "↑" if delta > 0.0005 else "↓" if delta < -0.0005 else "→"
    else:
        direction = "↓" if delta < -0.0005 else "↑" if delta > 0.0005 else "→"
    return f"{season:.{digits}f} → {recent:.{digits}f} ({sign}{delta:.{digits}f} {direction})"


def build_comprehensive_judgement_embed(report: dict) -> discord.Embed:
    away = _advanced_team_snapshot(report.get("away", {}) or {})
    home = _advanced_team_snapshot(report.get("home", {}) or {})

    categories = []
    def add_category(name, a, h, higher=True):
        if a is None or h is None:
            categories.append((name, "데이터 부족"))
            return
        if abs(a - h) < 1e-9:
            categories.append((name, "동률"))
        else:
            winner = away["team"] if ((a > h) == higher) else home["team"]
            categories.append((name, winner))

    add_category("시즌 타격", away["season_ops"], home["season_ops"], True)
    add_category("최근 10경기 타격", away["recent_ops"], home["recent_ops"], True)
    add_category("중계투수", away["season_whip"], home["season_whip"], False)
    add_category("최근 중계투수", away["recent_whip"], home["recent_whip"], False)
    add_category("선발 WHIP", away["starter_whip"], home["starter_whip"], False)
    add_category("최근 흐름", away["recent_wdl"][0], home["recent_wdl"][0], True)

    scores = {away["team"]: 0, home["team"]: 0}
    for _, winner in categories:
        if winner in scores:
            scores[winner] += 1

    top_team = max(scores, key=scores.get)
    top_count = scores[top_team]
    other = home["team"] if top_team == away["team"] else away["team"]
    if scores[top_team] == scores[other]:
        overall = "우위 지표 동률"
    else:
        overall = f"현재 데이터 기준 {top_team} 우위 ({top_count}개 지표)"

    lines = [f"**{overall}**", ""]
    for name, winner in categories:
        lines.append(f"• {name}: {winner}")

    embed = discord.Embed(
        title="종합 판정",
        description="승리확률을 임의로 만들지 않고 확보된 지표의 상대적 우위만 종합합니다.\n\n" + "\n".join(lines),
        color=discord.Color.dark_gray(),
    )
    embed.add_field(
        name=away["team"],
        value=(
            f"공격 OPS {away['season_ops']:.3f}" if away["season_ops"] is not None else "공격 OPS 데이터 부족"
        ) + "\n" + (
            f"최근 OPS {away['recent_ops']:.3f}" if away["recent_ops"] is not None else "최근 OPS 데이터 부족"
        ) + "\n" + (
            f"중계 WHIP {away['season_whip']:.2f}" if away["season_whip"] is not None else "중계 WHIP 데이터 부족"
        ),
        inline=True,
    )
    embed.add_field(
        name=home["team"],
        value=(
            f"공격 OPS {home['season_ops']:.3f}" if home["season_ops"] is not None else "공격 OPS 데이터 부족"
        ) + "\n" + (
            f"최근 OPS {home['recent_ops']:.3f}" if home["recent_ops"] is not None else "최근 OPS 데이터 부족"
        ) + "\n" + (
            f"중계 WHIP {home['season_whip']:.2f}" if home["season_whip"] is not None else "중계 WHIP 데이터 부족"
        ),
        inline=True,
    )
    embed.set_footer(text="종합 판정은 확보된 데이터 범위 내의 비교 결과이며 경기 결과를 보장하지 않습니다.")
    return embed


def build_change_analysis_embed(report: dict) -> discord.Embed:
    away = _advanced_team_snapshot(report.get("away", {}) or {})
    home = _advanced_team_snapshot(report.get("home", {}) or {})
    lines = []
    for s in (away, home):
        lines.append(f"**{s['team']}**")
        lines.append(f"타격: {_fmt_change(s['season_ops'], s['recent_ops'], 3, True)}")
        lines.append(f"중계투수 WHIP: {_fmt_change(s['season_whip'], s['recent_whip'], 2, False)}")
        lines.append("")

    lines.append("**해석**")
    if away["recent_ops"] is not None and home["recent_ops"] is not None:
        if away["recent_ops"] > home["recent_ops"]:
            lines.append(f"최근 타격 흐름은 {away['team']}가 상대적으로 좋습니다.")
        elif home["recent_ops"] > away["recent_ops"]:
            lines.append(f"최근 타격 흐름은 {home['team']}가 상대적으로 좋습니다.")
        else:
            lines.append("최근 타격 흐름은 비슷합니다.")
    if away["recent_whip"] is not None and home["recent_whip"] is not None:
        if away["recent_whip"] < home["recent_whip"]:
            lines.append(f"최근 중계투수 안정성은 {away['team']}가 상대적으로 좋습니다.")
        elif home["recent_whip"] < away["recent_whip"]:
            lines.append(f"최근 중계투수 안정성은 {home['team']}가 상대적으로 좋습니다.")
        else:
            lines.append("최근 중계투수 안정성은 비슷합니다.")

    return discord.Embed(
        title="시즌 ↔ 최근 10경기 변화량",
        description="최근 흐름이 시즌 평균과 비교해 어떻게 바뀌었는지 표시합니다.\n\n" + "\n".join(lines),
        color=discord.Color.dark_gray(),
    )


def build_key_factors_embed(report: dict) -> discord.Embed:
    away = _advanced_team_snapshot(report.get("away", {}) or {})
    home = _advanced_team_snapshot(report.get("home", {}) or {})
    factors = []

    if away["recent_ops"] is not None and home["recent_ops"] is not None:
        diff = abs(away["recent_ops"] - home["recent_ops"])
        if diff > 0.03:
            winner = away["team"] if away["recent_ops"] > home["recent_ops"] else home["team"]
            factors.append((diff, f"최근 10경기 타격: {winner} 우위 (OPS 격차 {diff:.3f})"))

    if away["starter_whip"] is not None and home["starter_whip"] is not None:
        diff = abs(away["starter_whip"] - home["starter_whip"])
        if diff > 0.05:
            winner = away["team"] if away["starter_whip"] < home["starter_whip"] else home["team"]
            factors.append((diff, f"선발 안정성: {winner} 우위 (WHIP 격차 {diff:.2f})"))

    if away["season_whip"] is not None and home["season_whip"] is not None:
        diff = abs(away["season_whip"] - home["season_whip"])
        if diff > 0.05:
            winner = away["team"] if away["season_whip"] < home["season_whip"] else home["team"]
            factors.append((diff, f"중계투수: {winner} 우위 (WHIP 격차 {diff:.2f})"))

    aw, ad, al = away["recent_wdl"]
    hw, hd, hl = home["recent_wdl"]
    if aw != hw:
        winner = away["team"] if aw > hw else home["team"]
        factors.append((abs(aw - hw), f"최근 흐름: {winner}가 최근 승수에서 우위 ({aw if winner == away['team'] else hw}승)"))

    if away["season_ops"] is not None and home["season_ops"] is not None:
        diff = abs(away["season_ops"] - home["season_ops"])
        if diff > 0.03:
            winner = away["team"] if away["season_ops"] > home["season_ops"] else home["team"]
            factors.append((diff, f"시즌 타격: {winner} 우위 (OPS 격차 {diff:.3f})"))

    factors.sort(key=lambda x: x[0], reverse=True)
    selected = factors[:3]

    if selected:
        body = "\n".join(f"**{i}.** {text}" for i, (_, text) in enumerate(selected, 1))
    else:
        body = "현재 확보된 데이터에서 뚜렷한 차이를 확인하기 어렵습니다."

    embed = discord.Embed(
        title="핵심 변수 TOP 3",
        description=body,
        color=discord.Color.dark_gray(),
    )
    embed.add_field(
        name="주의",
        value="지표 차이가 작은 항목은 핵심 변수에서 제외하고, 실제 확보된 데이터만 사용합니다.",
        inline=False,
    )
    return embed


def build_pre_game_checklist_embed(report: dict) -> discord.Embed:
    checks = []
    for key, label, ok in [
        ("away_starter", "원정 선발", bool((report.get("away", {}) or {}).get("starter", {}).get("playerInfo"))),
        ("home_starter", "홈 선발", bool((report.get("home", {}) or {}).get("starter", {}).get("playerInfo"))),
        ("away_lineup", "원정 라인업", len(_hitters(report.get("away", {}) or {})) >= 9),
        ("home_lineup", "홈 라인업", len(_hitters(report.get("home", {}) or {})) >= 9),
        ("away_bullpen", "원정 중계투수", len(_pitchers(report.get("away", {}) or {})) > 0),
        ("home_bullpen", "홈 중계투수", len(_pitchers(report.get("home", {}) or {})) > 0),
        ("stadium", "구장", bool((report.get("gameInfo", {}) or {}).get("stadium"))),
        ("confidence", "기본 데이터 신뢰도", bool(report.get("dataConfidence"))),
    ]:
        checks.append((label, ok))

    info = report.get("gameInfo", {}) or {}
    confidence = report.get("dataConfidence", {}) or {}
    score = confidence.get("score")
    lines = [
        f"**{text(info.get('aName'))} @ {text(info.get('hName'))}**",
        "",
    ]
    for label, ok in checks:
        lines.append(f"{'✅' if ok else '⚠️'} {label}")

    good = sum(1 for _, ok in checks if ok)
    if good == len(checks):
        level = "높음"
    elif good >= len(checks) * 0.75:
        level = "양호"
    else:
        level = "제한적"

    lines.extend([
        "",
        f"분석 가능 수준: **{level}**",
    ])
    if score is not None:
        lines.append(f"기본 데이터 신뢰도: **{score}%**")

    embed = discord.Embed(
        title="경기 전 체크리스트",
        description="분석에 필요한 핵심 데이터가 얼마나 확보됐는지 확인합니다.\n\n" + "\n".join(lines),
        color=discord.Color.dark_gray(),
    )
    embed.set_footer(text="경기 전 상태이므로 라인업·선발·구장 정보는 경기 직전 변경될 수 있습니다.")
    return embed


def build_full_report_embed(report: dict) -> discord.Embed:
    judgement = build_comprehensive_judgement_embed(report)
    change = build_change_analysis_embed(report)
    factors = build_key_factors_embed(report)
    checklist = build_pre_game_checklist_embed(report)

    embed = discord.Embed(
        title="KBO 경기 전 종합 리포트",
        description=judgement.description,
        color=discord.Color.dark_gray(),
    )
    embed.add_field(name="시즌 ↔ 최근10 변화", value=change.description[:1024], inline=False)
    embed.add_field(name="핵심 변수 TOP 3", value=factors.description[:1024], inline=False)
    embed.add_field(name="경기 전 체크", value=checklist.description[:1024], inline=False)
    embed.set_footer(text="확률 예측이 아닌 실제 확보 데이터 기반 비교 리포트입니다.")
    return embed


def get_game_id_from_report(report: dict) -> str:
    info = report.get("gameInfo", {}) or {}
    return str(report.get("gameId") or info.get("gameId") or "").strip()


def published_manual_text(game_id: str) -> str:
    item = get_manual_analysis(game_id)
    if not item or item.get("status") != "published":
        return ""
    return str(item.get("summary") or "").strip()


# ============================================================
# 중복 분석 방지 확인창 View
# ============================================================
class ConfirmAnalysisView(View):
    def __init__(self, user_id: str, original_interaction: discord.Interaction):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.original_interaction = original_interaction

    @discord.ui.button(label="예, 계속할게요", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: Button):
        _ANALYSIS_IN_PROGRESS[self.user_id] = False
        await interaction.response.edit_message(content="✅ 새 분석을 시작합니다.", view=None)
        # 새 분석 시작 (원본 interaction 사용)
        await start_analysis_logic(self.original_interaction, self.user_id)

    @discord.ui.button(label="아니오, 취소할게요", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: Button):
        _ANALYSIS_IN_PROGRESS[self.user_id] = False
        await interaction.response.edit_message(content="❌ 분석을 취소했습니다.", view=None)


# ============================================================
# 분석 시작 로직 (분리됨)
# ============================================================
async def start_analysis_logic(interaction: discord.Interaction, user_id: str):
    """실제 분석 시작 처리 (defer + 경기 목록 표시)"""
    if analysis_rate_limited(user_id):
        await interaction.response.send_message("잠시 후 다시 시도해주세요.", ephemeral=True)
        return
    try:
        await interaction.response.defer(ephemeral=True)
    except (discord.NotFound, discord.HTTPException):
        return
    lock = await get_analysis_lock(user_id)
    async with lock:
        if _ANALYSIS_IN_PROGRESS.get(user_id, False):
            await interaction.followup.send("이미 분석이 진행 중입니다.", ephemeral=True)
            return
        _ANALYSIS_IN_PROGRESS[user_id] = True
        try:
            from schedule_data import get_today_games, get_next_available_games
            games = get_today_games()
            target_date = today_iso()
            if not games:
                games, target_date = get_next_available_games(max_days=7)
            if not games:
                await interaction.followup.send("오늘 및 앞으로 7일 동안 예정된 KBO 경기를 찾을 수 없습니다.", ephemeral=True)
                _ANALYSIS_IN_PROGRESS[user_id] = False
                return
            selectable = [g for g in games if game_status_text(g) != "CANCEL"]
            lines = []
            for g in games[:10]:
                away, home = game_team_names(g)
                status = game_status_text(g)
                suffix = " · 취소" if status == "CANCEL" else (" · 경기 진행 중" if status == "LIVE" else (" · 경기 종료" if status == "END" else ""))
                lines.append(f"**{away} @ {home}** — {format_game_datetime(g)}{suffix}")
            title = "오늘의 KBO 경기" if target_date == today_iso() else f"{target_date} KBO 경기"
            prefix = "" if target_date == today_iso() else f"오늘({today_iso()}) 경기가 없어 {target_date} 경기를 표시합니다.\n\n"
            embed = discord.Embed(title=title, description=prefix + "\n".join(lines) + "\n\n분석할 경기를 선택하세요.", color=discord.Color.from_rgb(24, 36, 52))
            if not selectable:
                await interaction.followup.send(embed=embed, ephemeral=True)
                _ANALYSIS_IN_PROGRESS[user_id] = False
                return
            # GameSelectView에 user_id 전달하여 분석 완료 시 상태 해제 가능하도록
            view = GameSelectView(selectable, created_date=today_kst(), user_id=user_id)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            import traceback
            print(f"[경기 선택창 오류] {type(e).__name__}: {e}")
            traceback.print_exc()
            _ANALYSIS_IN_PROGRESS[user_id] = False
            try:
                await interaction.followup.send(f"경기 정보를 불러오는 중 오류가 발생했습니다.\n{type(e).__name__}: {e}", ephemeral=True)
            except Exception:
                pass


# ============================================================
# MatchupMenuView (이전과 동일)
# ============================================================
class MatchupMenuView(View):
    def __init__(self, report: dict, created_date: Optional[datetime.date] = None):
        super().__init__(timeout=None)
        self.report=report
        self.created_date=created_date or today_kst()

    async def _guard(self, interaction: discord.Interaction)->bool:
        if not session_date_is_today(self.created_date):
            await reset_to_today_games(interaction)
            return False
        return True

    @discord.ui.button(label="요약", style=discord.ButtonStyle.secondary, custom_id="kbo_match_summary", row=0)
    async def summary_button(self, interaction: discord.Interaction, button: Button):
        if await self._guard(interaction): await interaction.response.edit_message(embed=build_matchup_summary_embed(self.report),view=self)

    @discord.ui.button(label="선발", style=discord.ButtonStyle.secondary, custom_id="kbo_match_starter", row=0)
    async def starter_button(self, interaction: discord.Interaction, button: Button):
        if await self._guard(interaction): await interaction.response.edit_message(embed=build_starter_detail_embed(self.report),view=self)

    @discord.ui.button(label="라인업", style=discord.ButtonStyle.secondary, custom_id="kbo_match_lineup", row=0)
    async def lineup_button(self, interaction: discord.Interaction, button: Button):
        if not await self._guard(interaction):
            return
        await interaction.response.edit_message(embed=build_lineup_embed(self.report), view=self)
        await interaction.followup.send(
            "타자 상세 선택",
            view=PlayerButtonInfoView(self.report, "lineup"),
            ephemeral=True,
        )

    @discord.ui.button(label="불펜", style=discord.ButtonStyle.secondary, custom_id="kbo_match_bullpen", row=0)
    async def bullpen_button(self, interaction: discord.Interaction, button: Button):
        if not await self._guard(interaction):
            return
        await interaction.response.edit_message(embed=build_bullpen_embed(self.report), view=self)
        bullpen_view = PlayerButtonInfoView(self.report, "bullpen")
        if not bullpen_view.children:
            await interaction.followup.send(
                "이 경기의 불펜 선수 데이터를 네이버에서 확인하지 못했습니다.\n경기 상세 데이터 구조가 다른 경우를 자동 보정 중입니다.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            "불펜 투수 상세 선택",
            view=bullpen_view,
            ephemeral=True,
        )

    @discord.ui.button(label="핵심 타자", style=discord.ButtonStyle.secondary, custom_id="kbo_match_top_player", row=0)
    async def top_player_button(self, interaction: discord.Interaction, button: Button):
        if await self._guard(interaction): await interaction.response.edit_message(embed=build_top_player_embed(self.report),view=self)

    @discord.ui.button(label="데이터 신뢰도", style=discord.ButtonStyle.secondary, custom_id="kbo_match_data_confidence", row=1)
    async def data_confidence_button(self, interaction: discord.Interaction, button: Button):
        if not await self._guard(interaction):
            return
        await interaction.response.edit_message(embed=build_data_confidence_embed(self.report), view=self)

    @discord.ui.button(label="코멘트", style=discord.ButtonStyle.secondary, custom_id="kbo_match_comment", row=1)
    async def comment_button(self, interaction: discord.Interaction, button: Button):
        if not await self._guard(interaction):
            return
        await interaction.response.defer()
        embed = await build_comment_embed(self.report)
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="결장 · 구장", style=discord.ButtonStyle.secondary, custom_id="kbo_match_absence", row=1)
    async def absence_button(self, interaction: discord.Interaction, button: Button):
        if not await self._guard(interaction):
            return
        await interaction.response.defer()
        embed = await build_absence_stadium_embed(self.report)
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="최근 5경기", style=discord.ButtonStyle.secondary, custom_id="kbo_match_recent", row=1)
    async def recent_games_button(self, interaction: discord.Interaction, button: Button):
        if await self._guard(interaction): await interaction.response.edit_message(embed=build_team_recent_games_embed(self.report),view=self)

    @discord.ui.button(label="팀 지표", style=discord.ButtonStyle.secondary, custom_id="kbo_match_team", row=1)
    async def team_button(self, interaction: discord.Interaction, button: Button):
        if await self._guard(interaction): await interaction.response.edit_message(embed=build_team_form_embed(self.report),view=self)

    @discord.ui.button(label="타자(시즌전체)", style=discord.ButtonStyle.secondary, custom_id="kbo_match_hitter_season", row=2)
    async def hitter_season_button(self, interaction: discord.Interaction, button: Button):
        if not await self._guard(interaction):
            return
        await interaction.response.defer()
        try:
            embed, buf = _analysis_embed(self.report, "hitter", recent=False)
            if buf is None:
                await interaction.edit_original_response(embed=embed, view=self)
                return
            file = discord.File(buf, filename="kbo_hitter_analysis.png")
            embed.set_image(url="attachment://kbo_hitter_analysis.png")
            await interaction.edit_original_response(embed=embed, view=self, attachments=[file])
        except Exception as e:
            print(f"[타자 시즌 분석 오류] {type(e).__name__}: {e}")
            await interaction.edit_original_response(content=f"타자 분석 생성 실패: {e}", embed=None, view=self)

    @discord.ui.button(label="타자(최근10)", style=discord.ButtonStyle.secondary, custom_id="kbo_match_hitter_recent", row=2)
    async def hitter_recent_button(self, interaction: discord.Interaction, button: Button):
        if not await self._guard(interaction):
            return
        await interaction.response.defer()
        try:
            embed, buf = _analysis_embed(self.report, "hitter", recent=True)
            if buf is None:
                await interaction.edit_original_response(embed=embed, view=self)
                return
            file = discord.File(buf, filename="kbo_hitter_analysis.png")
            embed.set_image(url="attachment://kbo_hitter_analysis.png")
            await interaction.edit_original_response(embed=embed, view=self, attachments=[file])
        except Exception as e:
            print(f"[타자 최근10 분석 오류] {type(e).__name__}: {e}")
            await interaction.edit_original_response(content=f"타자 분석 생성 실패: {e}", embed=None, view=self)

    @discord.ui.button(label="투수(시즌전체)", style=discord.ButtonStyle.secondary, custom_id="kbo_match_pitcher_season", row=2)
    async def pitcher_season_button(self, interaction: discord.Interaction, button: Button):
        if not await self._guard(interaction):
            return
        await interaction.response.defer()
        try:
            embed, buf = _analysis_embed(self.report, "pitcher", recent=False)
            if buf is None:
                await interaction.edit_original_response(embed=embed, view=self)
                return
            file = discord.File(buf, filename="kbo_pitcher_analysis.png")
            embed.set_image(url="attachment://kbo_pitcher_analysis.png")
            await interaction.edit_original_response(embed=embed, view=self, attachments=[file])
        except Exception as e:
            print(f"[투수 시즌 분석 오류] {type(e).__name__}: {e}")
            await interaction.edit_original_response(content=f"투수 분석 생성 실패: {e}", embed=None, view=self)

    @discord.ui.button(label="투수(최근10)", style=discord.ButtonStyle.secondary, custom_id="kbo_match_pitcher_recent", row=2)
    async def pitcher_recent_button(self, interaction: discord.Interaction, button: Button):
        if not await self._guard(interaction):
            return
        await interaction.response.defer()
        try:
            embed, buf = _analysis_embed(self.report, "pitcher", recent=True)
            if buf is None:
                await interaction.edit_original_response(embed=embed, view=self)
                return
            file = discord.File(buf, filename="kbo_pitcher_analysis.png")
            embed.set_image(url="attachment://kbo_pitcher_analysis.png")
            await interaction.edit_original_response(embed=embed, view=self, attachments=[file])
        except Exception as e:
            print(f"[투수 최근10 분석 오류] {type(e).__name__}: {e}")
            await interaction.edit_original_response(content=f"투수 분석 생성 실패: {e}", embed=None, view=self)

    @discord.ui.button(label="핵심 매치업", style=discord.ButtonStyle.secondary, custom_id="kbo_match_core_matchup", row=2)
    async def core_matchup_button(self, interaction: discord.Interaction, button: Button):
        if not await self._guard(interaction):
            return
        try:
            await interaction.response.defer()
            embed = await asyncio.to_thread(build_core_matchup_embed, self.report)
            await interaction.edit_original_response(embed=embed, view=self)
        except discord.NotFound:
            print("[핵심 매치업] Interaction 만료")
        except Exception as e:
            print(f"[핵심 매치업 오류] {type(e).__name__}: {e}")
            try:
                await interaction.edit_original_response(content=f"핵심 매치업 생성 실패: {e}", embed=None, view=self)
            except Exception:
                pass

    @discord.ui.button(label="종합판정", style=discord.ButtonStyle.secondary, custom_id="kbo_match_overall", row=4)
    async def overall_button(self, interaction: discord.Interaction, button: Button):
        if not await self._guard(interaction):
            return
        try:
            await interaction.response.defer()
            embed = await asyncio.to_thread(build_comprehensive_judgement_embed, self.report)
            await interaction.edit_original_response(embed=embed, view=self)
        except discord.NotFound:
            print("[종합판정] Interaction 만료")
        except Exception as e:
            print(f"[종합판정 오류] {type(e).__name__}: {e}")
            try:
                await interaction.edit_original_response(content=f"종합판정 생성 실패: {e}", embed=None, view=self)
            except Exception:
                pass

    @discord.ui.button(label="변화량", style=discord.ButtonStyle.secondary, custom_id="kbo_match_change", row=4)
    async def change_button(self, interaction: discord.Interaction, button: Button):
        if not await self._guard(interaction):
            return
        try:
            await interaction.response.defer()
            embed = await asyncio.to_thread(build_change_analysis_embed, self.report)
            await interaction.edit_original_response(embed=embed, view=self)
        except discord.NotFound:
            print("[변화량] Interaction 만료")
        except Exception as e:
            print(f"[변화량 오류] {type(e).__name__}: {e}")
            try:
                await interaction.edit_original_response(content=f"변화량 분석 생성 실패: {e}", embed=None, view=self)
            except Exception:
                pass

    @discord.ui.button(label="핵심변수", style=discord.ButtonStyle.secondary, custom_id="kbo_match_key_factors", row=4)
    async def key_factors_button(self, interaction: discord.Interaction, button: Button):
        if not await self._guard(interaction):
            return
        try:
            await interaction.response.defer()
            embed = await asyncio.to_thread(build_key_factors_embed, self.report)
            await interaction.edit_original_response(embed=embed, view=self)
        except discord.NotFound:
            print("[핵심변수] Interaction 만료")
        except Exception as e:
            print(f"[핵심변수 오류] {type(e).__name__}: {e}")
            try:
                await interaction.edit_original_response(content=f"핵심변수 생성 실패: {e}", embed=None, view=self)
            except Exception:
                pass

    @discord.ui.button(label="경기전 체크", style=discord.ButtonStyle.secondary, custom_id="kbo_match_checklist", row=4)
    async def checklist_button(self, interaction: discord.Interaction, button: Button):
        if not await self._guard(interaction):
            return
        try:
            await interaction.response.defer()
            embed = await asyncio.to_thread(build_pre_game_checklist_embed, self.report)
            await interaction.edit_original_response(embed=embed, view=self)
        except discord.NotFound:
            print("[경기전 체크] Interaction 만료")
        except Exception as e:
            print(f"[경기전 체크 오류] {type(e).__name__}: {e}")
            try:
                await interaction.edit_original_response(content=f"경기전 체크 생성 실패: {e}", embed=None, view=self)
            except Exception:
                pass

    @discord.ui.button(label="전체 리포트", style=discord.ButtonStyle.secondary, custom_id="kbo_match_full_report", row=3)
    async def full_report_button(self, interaction: discord.Interaction, button: Button):
        if not await self._guard(interaction):
            return
        try:
            await interaction.response.defer()
            embed = await asyncio.to_thread(build_full_report_embed, self.report)
            game_id = get_game_id_from_report(self.report)
            if game_id and is_published(game_id):
                manual_embed = build_published_manual_embed(game_id)
                if manual_embed is not None:
                    if manual_embed.description:
                        embed.add_field(name="관리자 검수 분석", value=manual_embed.description[:1024], inline=False)
                    for field in manual_embed.fields:
                        embed.add_field(name=f"관리자 · {field.name}", value=field.value[:1024], inline=False)
                    embed.set_footer(text="기본 데이터 분석 + 관리자 검수 분석")
            await interaction.edit_original_response(embed=embed, view=self)
        except discord.NotFound:
            print("[전체 리포트] Interaction 만료")
        except Exception as e:
            print(f"[전체 리포트 오류] {type(e).__name__}: {e}")
            try:
                await interaction.edit_original_response(content=f"전체 리포트 생성 실패: {e}", embed=None, view=self)
            except Exception:
                pass

    @discord.ui.button(label="승패 그래프", style=discord.ButtonStyle.secondary, custom_id="kbo_match_wdl_graph", row=3)
    async def wdl_graph_button(self, interaction: discord.Interaction, button: Button):
        if not await self._guard(interaction): return
        await interaction.response.defer()
        try:
            buf=make_wdl_graph(self.report)
            file=discord.File(buf,filename="kbo_wdl.png")
            embed=discord.Embed(title="매칭 팀별 승 · 무 · 패",color=discord.Color.dark_gray())
            embed.set_image(url="attachment://kbo_wdl.png")
            await interaction.edit_original_response(embed=embed,view=self,attachments=[file])
        except Exception as e:
            await interaction.edit_original_response(content=f"그래프 생성 실패: {e}",embed=build_team_form_embed(self.report),view=self)

    @discord.ui.button(label="홈 · 원정", style=discord.ButtonStyle.secondary, custom_id="kbo_match_ha_graph", row=3)
    async def home_away_graph_button(self, interaction: discord.Interaction, button: Button):
        if not await self._guard(interaction): return
        await interaction.response.defer()
        try:
            buf=make_home_away_winrate_graph(self.report)
            file=discord.File(buf,filename="kbo_home_away.png")
            embed=discord.Embed(title="홈팀 · 원정팀 승률",color=discord.Color.dark_gray())
            embed.set_image(url="attachment://kbo_home_away.png")
            await interaction.edit_original_response(embed=embed,view=self,attachments=[file])
        except Exception as e:
            await interaction.edit_original_response(content=f"그래프 생성 실패: {e}",embed=build_team_form_embed(self.report),view=self)

    @discord.ui.button(label="종합 레이더", style=discord.ButtonStyle.secondary, custom_id="kbo_match_radar", row=3)
    async def radar_button(self, interaction: discord.Interaction, button: Button):
        if not await self._guard(interaction):
            return
        await interaction.response.defer()
        try:
            buf = make_radar_chart(self.report)
            file = discord.File(buf, filename="kbo_radar.png")
            embed = discord.Embed(title="종합 지표 레이더", color=discord.Color.dark_gray())
            embed.set_image(url="attachment://kbo_radar.png")
            await interaction.edit_original_response(embed=embed, view=self, attachments=[file])
        except Exception as e:
            await interaction.edit_original_response(content=f"레이더 그래프 생성 실패: {e}", embed=build_team_form_embed(self.report), view=self)

    @discord.ui.button(label="팀 ERA · 타율", style=discord.ButtonStyle.secondary, custom_id="kbo_match_team_metrics", row=3)
    async def team_metrics_button(self, interaction: discord.Interaction, button: Button):
        if not await self._guard(interaction): return
        await interaction.response.defer()
        try:
            buf=make_team_metrics_chart(self.report)
            file=discord.File(buf,filename="kbo_team_metrics.png")
            embed=discord.Embed(title="팀 평균자책점 · 타율",color=discord.Color.dark_gray())
            embed.set_image(url="attachment://kbo_team_metrics.png")
            await interaction.edit_original_response(embed=embed,view=self,attachments=[file])
        except Exception as e:
            await interaction.edit_original_response(content=f"그래프 생성 실패: {e}",embed=build_team_form_embed(self.report),view=self)


# -----------------------------------------------------------------------------
# 경기 선택
# -----------------------------------------------------------------------------
class GameSelect(Select):
    def __init__(self, games: List[dict], user_id: str):
        self.games = games
        self.user_id = user_id
        options = []
        for idx, g in enumerate(games[:25]):
            away, home = game_team_names(g)
            dt = format_game_datetime(g)
            stadium = first_value(g.get("stadium"), g.get("venue"), default="")
            description = f"{dt}"
            if stadium:
                description += f" · {stadium}"
            options.append(
                discord.SelectOption(
                    label=f"{away} @ {home}"[:100],
                    value=str(idx),
                    description=description[:100],
                )
            )
        super().__init__(placeholder="분석할 경기를 선택하세요.", min_values=1, max_values=1, options=options, custom_id="kbo_game_select")

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not isinstance(view, GameSelectView):
            await interaction.response.send_message("경기 선택 세션을 찾을 수 없습니다. 다시 시작해주세요.", ephemeral=True)
            return
        if getattr(view, "consumed", False):
            await interaction.response.send_message("이미 분석이 접수된 경기 선택창입니다.", ephemeral=True)
            return
        if not session_date_is_today(view.created_date):
            await reset_to_today_games(interaction)
            return

        try:
            game = self.games[int(self.values[0])]
        except (ValueError, IndexError):
            await interaction.response.send_message("잘못된 경기 선택입니다. 다시 경기 분석을 시작해주세요.", ephemeral=True)
            return

        discord_id = self.user_id  # 저장된 user_id 사용
        lock = await get_analysis_lock(discord_id)
        async with lock:
            if view.consumed:
                await interaction.response.send_message("이미 분석이 접수된 경기 선택창입니다.", ephemeral=True)
                return

            away, home = game_team_names(game)
            date = first_value(game.get("gdate"), game.get("date"), default=today_iso())
            time_text = first_value(game.get("gtime"), game.get("time"), default="시간 미정")
            stadium = first_value(game.get("stadium"), game.get("venue"), default="구장 미정")
            await interaction.response.edit_message(
                content=f"**{date} {time_text}** · {away} @ {home} · {stadium}\n경기 분석 자료를 종합하는 중...",
                embed=None,
                view=None,
            )
            try:
                report = build_matchup_report(str(date), away, home)
                if not use_credit(discord_id):
                    await interaction.edit_original_response(
                        content="사용 가능 횟수가 없습니다. 구매하기에서 충전해주세요.",
                        embed=None,
                        view=None,
                    )
                    _ANALYSIS_IN_PROGRESS[discord_id] = False
                    return
                view.consumed = True
                record_usage_log(discord_id, away, home, str(date), str(time_text))
                embed = build_matchup_summary_embed(report)
                matchup_view = MatchupMenuView(report, created_date=today_kst())
                await interaction.edit_original_response(content=None, embed=embed, view=matchup_view)
                # 분석 완료 -> 상태 해제
                _ANALYSIS_IN_PROGRESS[discord_id] = False
            except MatchupReportError as e:
                await interaction.edit_original_response(content=f"분석 실패: {e}", embed=None, view=None)
                _ANALYSIS_IN_PROGRESS[discord_id] = False
            except Exception:
                await interaction.edit_original_response(content="경기 분석 자료를 불러오는 중 오류가 발생했습니다.", embed=None, view=None)
                _ANALYSIS_IN_PROGRESS[discord_id] = False


class GameSelectView(View):
    def __init__(self, games: List[dict], created_date: Optional[datetime.date] = None, user_id: str = None):
        super().__init__(timeout=None)
        self.created_date = created_date or today_kst()
        self.consumed = False
        self.user_id = user_id
        self.add_item(GameSelect(games, user_id))


async def get_analysis_lock(user_id: str) -> asyncio.Lock:
    lock = _ANALYSIS_LOCKS.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _ANALYSIS_LOCKS[user_id] = lock
    return lock


def analysis_rate_limited(user_id: str) -> bool:
    now = time.monotonic()
    last = _LAST_ANALYSIS_REQUEST.get(user_id, 0.0)
    if now - last < _ANALYSIS_COOLDOWN_SECONDS:
        return True
    _LAST_ANALYSIS_REQUEST[user_id] = now
    return False


# -----------------------------------------------------------------------------
# 시작 패널 / 구매
# -----------------------------------------------------------------------------
class StartView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="경기 분석 시작", style=discord.ButtonStyle.secondary, custom_id="kbo_panel_use")
    async def use_button(self, interaction: discord.Interaction, button: Button):
        user_id = str(interaction.user.id)

        # 이미 분석 중인지 확인
        if _ANALYSIS_IN_PROGRESS.get(user_id, False):
            # 확인창 띄우기
            view = ConfirmAnalysisView(user_id, interaction)
            await interaction.response.send_message(
                "⚠️ 이미 분석이 진행 중입니다. 새로 시작하시겠습니까? (기존 분석은 취소됩니다.)",
                view=view,
                ephemeral=True
            )
            return

        # 분석 시작
        await start_analysis_logic(interaction, user_id)

    @discord.ui.button(label="내 정보", style=discord.ButtonStyle.secondary, custom_id="kbo_panel_info")
    async def info_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(
            embed=build_my_info_embed(interaction.user),
            ephemeral=True,
        )

    @discord.ui.button(label="구매하기", style=discord.ButtonStyle.secondary, custom_id="kbo_panel_buy")
    async def buy_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        discord_id = str(interaction.user.id)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("서버에서만 이용할 수 있습니다.", ephemeral=True)
            return

        existing_channel_id = get_open_ticket(discord_id)
        if existing_channel_id:
            existing_channel = guild.get_channel(existing_channel_id)
            if existing_channel:
                await interaction.followup.send(f"이미 진행 중인 구매 문의가 있습니다: {existing_channel.mention}", ephemeral=True)
                return
            remove_ticket_by_channel(existing_channel_id)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        safe_name = "".join(c for c in interaction.user.display_name if c.isalnum())[:20] or "user"
        ticket_channel = await guild.create_text_channel(
            name=f"구매문의-{safe_name}",
            overwrites=overwrites,
            reason=f"{interaction.user}의 사용권 구매 문의 티켓",
        )
        record_ticket(discord_id, ticket_channel.id)
        embed = discord.Embed(
            title="사용권 구매 문의",
            description=(
                f"{interaction.user.mention}님, 문의 채널이 생성되었습니다.\n\n"
                "**입금 계좌 안내**\n(관리자가 채널에서 안내드립니다)\n\n"
                "입금 후 이 채널에 입금자명/금액을 남겨주시면 확인 후 사용권을 충전합니다."
            ),
            color=discord.Color.gold(),
        )
        await ticket_channel.send(embed=embed, view=TicketCloseView())
        await interaction.followup.send(f"티켓이 생성되었습니다: {ticket_channel.mention}", ephemeral=True)


def build_panel_embed() -> discord.Embed:
    return discord.Embed(
        title="KBO 정밀 분석 봇",
        description=(
            "실제 경기 데이터 기반으로 선발·라인업·불펜·타자·투수를 종합 분석합니다.\n\n"
            "**경기 분석 시작** — 사용권 1회 차감 후 오늘 경기 선택\n"
            "**내 정보** — 남은 사용 횟수·누적 사용 횟수·최근 5회 사용 로그 확인\n"
            "**구매하기** — 사용권 충전 문의\n"
            "**수동분석** — 관리자 검수 후 분석 공개\n\n"
            "분석 화면은 3분 후 만료되지 않으며, **한국시간 자정(00:00)에 오늘 기준으로 자동 초기화**됩니다."
        ),
        color=discord.Color.from_rgb(24, 36, 52),
    )


class TicketCloseView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="티켓삭제", style=discord.ButtonStyle.secondary, custom_id="kbo_ticket_close")
    async def close_button(self, interaction: discord.Interaction, button: Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("이 버튼은 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        await interaction.response.send_message("이 채널을 5초 후 삭제합니다...")
        remove_ticket_by_channel(interaction.channel.id)
        import asyncio
        await asyncio.sleep(5)
        await interaction.channel.delete(reason=f"{interaction.user}가 티켓 종료")


# -----------------------------------------------------------------------------
# 봇 이벤트 / 명령어
# -----------------------------------------------------------------------------
async def refresh_saved_panel():
    record = load_panel_record()
    if not record:
        return
    try:
        channel_id = int(record.get("channel_id"))
        message_id = int(record.get("message_id"))
        channel = bot.get_channel(channel_id)
        if channel is None:
            channel = await bot.fetch_channel(channel_id)
        msg = await channel.fetch_message(message_id)
        await msg.edit(embed=build_panel_embed(), view=StartView())
        print("[패널 자동 갱신] 완료")
    except Exception as e:
        print(f"[패널 자동 갱신] 실패: {type(e).__name__}: {e}")


@bot.event
async def on_ready():
    bot.add_view(StartView())
    bot.add_view(TicketCloseView())
    await refresh_saved_panel()
    print(f"{bot.user} 로그인 완료! KST {now_kst().strftime('%Y-%m-%d %H:%M:%S')}")


def load_panel_record():
    try:
        with open(PANEL_RECORD_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_panel_record(channel_id: int, message_id: int):
    directory = os.path.dirname(os.path.abspath(PANEL_RECORD_FILE)) or "."
    payload = {"channel_id": int(channel_id), "message_id": int(message_id)}
    fd, temp_path = tempfile.mkstemp(prefix="panel_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, PANEL_RECORD_FILE)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


@bot.command(name="패널")
@commands.has_permissions(administrator=True)
async def panel_command(ctx):
    record = load_panel_record()
    if record:
        try:
            channel_id = int(record.get("channel_id"))
            message_id = int(record.get("message_id"))
            old_channel = bot.get_channel(channel_id)
            if old_channel is None:
                old_channel = await bot.fetch_channel(channel_id)
            old_msg = await old_channel.fetch_message(message_id)
            await old_msg.edit(embed=build_panel_embed(), view=StartView())
            await ctx.send(f"기존 패널을 최신 UI로 갱신했습니다.\n{old_msg.jump_url}")
            return
        except (discord.NotFound, discord.HTTPException, ValueError, TypeError) as e:
            print(f"[패널 갱신] 기존 패널 접근 실패: {e}")

    msg = await ctx.send(embed=build_panel_embed(), view=StartView())
    try:
        await msg.pin()
    except discord.Forbidden:
        await ctx.send("메시지 고정 권한이 없어 고정은 못했지만 패널은 게시되었습니다.")
    save_panel_record(ctx.channel.id, msg.id)


@panel_command.error
async def panel_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(" 이 명령어는 관리자만 사용할 수 있습니다.")


@bot.command(name="충전")
@commands.has_permissions(administrator=True)
async def charge_command(ctx, member: discord.Member = None, amount: int = None):
    if ctx.guild is None:
        await ctx.send("서버에서만 사용할 수 있습니다.")
        return
    if member is None or amount is None:
        await ctx.send("사용법: `!충전 @유저 5`")
        return
    if amount <= 0 or amount > MAX_CREDIT_CHARGE:
        await ctx.send(f"충전 횟수는 1~{MAX_CREDIT_CHARGE} 범위에서 입력해주세요.")
        return
    try:
        new_balance = add_credits(str(member.id), amount)
    except Exception:
        await ctx.send("사용권 충전에 실패했습니다.")
        return
    await ctx.send(f"{member.display_name}님에게 {amount}회 충전 완료. (잔여 {new_balance}회)")


@charge_command.error
async def charge_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(" 이 명령어는 관리자만 사용할 수 있습니다.")


@bot.command(name="선수")
async def player_command(ctx, *, name: str = None):
    if not name:
        await ctx.send("조회할 선수 이름을 입력해주세요. (예: `!선수 김진호`)")
        return
    search_result = find_player_by_name(name)
    if search_result is None:
        await ctx.send(f" '{name}'(으)로 검색된 선수가 없습니다.")
        return
    if isinstance(search_result, dict):
        temp_msg = await ctx.send(f"'{search_result.get('playerName', name)}' 선수 데이터를 불러오는 중...")
        try:
            data = get_player_data(str(first_value(search_result.get("playerId"), search_result.get("playerID"), search_result.get("id"))))
            await temp_msg.edit(content=None, embed=build_basic_embed(data), view=PlayerMenuView(data))
        except Exception as e:
            await temp_msg.edit(content=f"데이터를 불러오지 못했습니다: {e}")
    else:
        await ctx.send(
            f"🔍 '{name}'(으)로 총 {len(search_result)}명의 선수가 검색되었습니다.\n아래 메뉴에서 원하는 선수를 선택해주세요.",
            view=PlayerSelectView(search_result),
        )


@bot.command(name="수동분석")
@commands.has_permissions(administrator=True)
async def manual_analysis_command(ctx):
    try:
        from schedule_data import get_today_games, get_next_available_games
        games = get_today_games()
        target_date = today_iso()
        if not games:
            games, target_date = get_next_available_games(max_days=7)
        games = [g for g in (games or []) if game_status_text(g) != "CANCEL"]
        if not games:
            await ctx.send("오늘 및 앞으로 7일 동안 수동 분석 가능한 경기가 없습니다.")
            return
        lines=[]
        for g in games[:10]:
            away, home = game_team_names(g)
            lines.append(f"**{away} @ {home}** — {format_game_datetime(g)}")
        prefix = "" if target_date == today_iso() else f"오늘({today_iso()}) 경기가 없어 {target_date} 경기를 표시합니다.\n\n"
        embed=discord.Embed(title="관리자 수동 분석 작성", description=prefix+"\n".join(lines)+"\n\n아래 메뉴에서 경기를 선택하세요.", color=discord.Color.gold())
        await ctx.send(embed=embed, view=ManualGameSelectView(games))
    except Exception as e:
        import traceback
        print(f"[수동 분석 명령 오류] {type(e).__name__}: {e}")
        traceback.print_exc()
        await ctx.send(f"수동 분석 경기 목록을 불러오지 못했습니다: {e}")


@manual_analysis_command.error
async def manual_analysis_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("이 명령어는 관리자만 사용할 수 있습니다.")


@bot.command(name="분석")
async def matchup_command(ctx, team_a: str = None, team_b: str = None, date: str = None):
    if not team_a or not team_b:
        await ctx.send("사용법: `!분석 롯데 NC` (팀명 두 개 입력)")
        return
    user_id = str(ctx.author.id)
    if analysis_rate_limited(user_id):
        await ctx.send("잠시 후 다시 시도해주세요.")
        return
    lock = await get_analysis_lock(user_id)
    async with lock:
        date = date or today_iso()
        await ctx.send(f"{date} {team_a} vs {team_b} 경기 분석 중...")
        try:
            report = build_matchup_report(date, team_a, team_b)
        except MatchupReportError as e:
            await ctx.send(f"분석 실패: {e}")
            return
        except Exception:
            await ctx.send("경기 분석 자료를 불러오는 중 오류가 발생했습니다.")
            return
        if not use_credit(user_id):
            await ctx.send("사용 가능 횟수가 없습니다. 구매하기에서 충전해주세요.")
            return
        info = report.get("gameInfo", {})
        record_usage_log(
            user_id,
            str(info.get("aName", team_a)),
            str(info.get("hName", team_b)),
            str(info.get("gdate", date)),
            str(info.get("gtime", "시간 미정")),
        )
        await ctx.send(
            embed=build_matchup_summary_embed(report),
            view=MatchupMenuView(report, created_date=today_kst()),
        )


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN 환경변수가 없습니다.")

TOKEN = os.getenv("DISCORD_TOKEN") 

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN 환경변수가 없습니다.")

bot.run(TOKEN)
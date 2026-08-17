import datetime
import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://m.sports.naver.com/",
}
SCHEDULE_URL = "https://api-gw.sports.naver.com/schedule/games"


def _kst_today():
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).date()


def _request(date):
    params = {
        "fields": "basic,schedule,baseball,manualRelayUrl",
        "upperCategoryId": "kbaseball",
        "fromDate": date,
        "toDate": date,
        "size": 500,
    }
    r = requests.get(SCHEDULE_URL, params=params, headers=HEADERS, timeout=10)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise ValueError(f"네이버 일정 응답 실패: {data}")
    return (data.get("result") or {}).get("games", []) or []


def _real_kbo(game):
    if not isinstance(game, dict):
        return False
    gid = str(game.get("gameId") or "")
    title = str(game.get("title") or "").lower()
    home = str(game.get("homeTeamName") or "").strip()
    away = str(game.get("awayTeamName") or "").strip()
    hcode = str(game.get("homeTeamCode") or "").strip()
    acode = str(game.get("awayTeamCode") or "").strip()
    if any(x in title for x in ("라이브", "치지직", "클럽챔피언십", "야구부장", "주간오프너")):
        return False
    if not home or not away:
        return False
    if "KBO" in gid.upper() and (hcode or acode):
        return True
    known = {"LG", "KT", "SS", "NC", "OB", "HT", "LT", "WO", "SK", "HH"}
    return hcode.upper() in known and acode.upper() in known


def _norm(g):
    date = g.get("gameDate") or g.get("gdate") or g.get("date") or ""
    dt = str(g.get("gameDateTime") or "")
    tm = dt.split("T", 1)[1][:5] if "T" in dt else (g.get("gtime") or g.get("time") or "")
    cancel = bool(g.get("cancel"))
    raw_status = str(g.get("statusCode") or "UNKNOWN").upper()
    if cancel:
        status = "CANCEL"
    elif raw_status in {"BEFORE", "SCHEDULED", "UPCOMING", "WAIT", "READY"}:
        status = "BEFORE"
    elif raw_status in {"LIVE", "PLAYING", "IN_PROGRESS", "STARTED"}:
        status = "LIVE"
    elif raw_status in {"END", "ENDED", "FINAL", "FINISHED", "GAME_END"}:
        status = "END"
    else:
        status = raw_status
    return {
        "gameId": g.get("gameId"), "date": date, "gdate": date, "gameDate": date,
        "gtime": tm, "time": tm, "gameTime": tm,
        "stadium": g.get("stadium") or "", "venue": g.get("stadium") or "",
        "team_a": g.get("awayTeamName") or "", "team_b": g.get("homeTeamName") or "",
        "away": g.get("awayTeamName") or "", "home": g.get("homeTeamName") or "",
        "awayTeamName": g.get("awayTeamName") or "", "homeTeamName": g.get("homeTeamName") or "",
        "aName": g.get("awayTeamName") or "", "hName": g.get("homeTeamName") or "",
        "awayTeamCode": g.get("awayTeamCode") or "", "homeTeamCode": g.get("homeTeamCode") or "",
        "aCode": g.get("awayTeamCode") or "", "hCode": g.get("homeTeamCode") or "",
        "categoryId": g.get("categoryId"), "categoryName": g.get("categoryName"),
        "statusCode": raw_status, "gameStatusNormalized": status, "cancel": cancel,
        "suspended": bool(g.get("suspended")), "title": g.get("title") or "", "raw": g,
    }


def fetch_games(date):
    return sorted([_norm(g) for g in _request(date) if _real_kbo(g)], key=lambda x: (x.get("gdate", ""), x.get("gtime", ""), x.get("gameId", "")))


def get_games_by_date(date):
    try:
        games = fetch_games(date)
    except Exception as e:
        print(f"[KBO 일정 실패] {date}: {type(e).__name__}: {e}")
        return []
    print(f"[{date}] KBO 경기 수: {len(games)}")
    for i, g in enumerate(games, 1):
        print(f"{i}. {g.get('gdate')} {g.get('gtime')} {g.get('team_a')} @ {g.get('team_b')} | 상태={g.get('gameStatusNormalized')} | ID={g.get('gameId')}")
    return games


def get_today_games():
    return get_games_by_date(_kst_today().isoformat())


def get_next_available_games(max_days=7):
    base = _kst_today()
    for offset in range(max_days + 1):
        d = base + datetime.timedelta(days=offset)
        ds = d.isoformat()
        games = get_games_by_date(ds)
        if games:
            return games, ds
    return [], None


def find_matchup(date, team_a, team_b):
    target = {str(team_a or "").strip(), str(team_b or "").strip()}
    for g in get_games_by_date(date):
        actual = {str(g.get("awayTeamName") or "").strip(), str(g.get("homeTeamName") or "").strip(), str(g.get("awayTeamCode") or "").strip(), str(g.get("homeTeamCode") or "").strip()}
        if target.issubset(actual):
            return g
    return None


def get_game_status(game):
    if not isinstance(game, dict):
        return "UNKNOWN"
    if game.get("cancel") is True:
        return "CANCEL"
    return str(game.get("gameStatusNormalized") or game.get("statusCode") or "UNKNOWN").upper()


def all_games_finished(games):
    return bool(games) and all(get_game_status(g) not in {"BEFORE", "LIVE", "UNKNOWN"} for g in games)
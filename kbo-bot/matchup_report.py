"""
매치업 리포트 모듈 v3

- game_preview API를 중심으로 경기 전 분석 리포트를 구성
- 오늘 라인업이 없으면 최근 경기 라인업을 참고용으로 사용
- 타선/불펜을 UI에서 쉽게 사용할 수 있도록 표준화
- 최근 승/무/패 및 홈/원정 성적을 리포트에 계산
- 결장자/경기 코멘트/분석 자료는 game_preview에 존재하는 키를 최대한 보존

중요:
이 모듈은 game_preview가 실제로 제공하지 않는 데이터를 임의로 생성하지 않습니다.
결장자나 코멘트가 API에 없으면 해당 항목은 빈 값으로 남습니다.
"""

from typing import Any, Dict, Iterable, List, Optional, Tuple

from schedule_data import find_matchup
from game_preview import fetch_game_preview
from player_data import get_player_data


class MatchupReportError(Exception):
    pass


def _first(data: dict, *keys, default=None):
    if not isinstance(data, dict):
        return default
    for key in keys:
        value = data.get(key)
        if value not in (None, "", "-"):
            return value
    return default


def _number(value, default=0.0) -> float:
    try:
        if value in (None, "", "-"):
            return default
        return float(str(value).replace("%", ""))
    except (TypeError, ValueError):
        return default


def _int(value, default=0) -> int:
    try:
        if value in (None, "", "-"):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _lineup_sort_key(game: dict):
    value = str(game.get("gdate") or game.get("date") or "")[:10]
    return value


def _confirmed_batters(lineup: dict) -> List[dict]:
    players = []
    seen_orders = set()
    for player in (lineup or {}).get("fullLineUp", []) or []:
        if not isinstance(player, dict):
            continue
        order = _int(player.get("batorder"), 99)
        name = str(player.get("playerName") or player.get("name") or "").strip()
        if 1 <= order <= 9 and name and order not in seen_orders:
            players.append(player)
            seen_orders.add(order)
    return sorted(players, key=lambda p: _int(p.get("batorder"), 99))


def get_fallback_lineup(team_code: str, previous_games: list, team_name: str = ""):
    """오늘 라인업이 확정되지 않았으면 가장 최근 실제 경기의 1~9번 라인업을 가져온다."""
    games = sorted(
        [g for g in (previous_games or []) if isinstance(g, dict) and g.get("gameId")],
        key=_lineup_sort_key,
        reverse=True,
    )
    for game in games:
        recent_game_id = game.get("gameId")
        recent_date = game.get("gdate") or game.get("date")
        try:
            recent_preview = fetch_game_preview(recent_game_id)
        except Exception as e:
            print(f"  [디버그] 최근경기({recent_game_id}) 조회 실패: {e}")
            continue

        game_info = recent_preview.get("gameInfo", {}) or {}
        h_code = str(game_info.get("hCode") or "").strip()
        a_code = str(game_info.get("aCode") or "").strip()
        h_name = str(game_info.get("hName") or "").strip()
        a_name = str(game_info.get("aName") or "").strip()
        target_code = str(team_code or "").strip()
        target_name = str(team_name or "").strip()
        if target_code and h_code == target_code or target_name and h_name == target_name:
            lineup = recent_preview.get("homeTeamLineUp", {}) or {}
        elif target_code and a_code == target_code or target_name and a_name == target_name:
            lineup = recent_preview.get("awayTeamLineUp", {}) or {}
        else:
            continue

        batters = _confirmed_batters(lineup)
        if len(batters) >= 9:
            lineup = dict(lineup)
            lineup["fullLineUp"] = batters
            return lineup, recent_date

    return None, None


def _extract_batters(lineup: dict) -> List[dict]:
    return _confirmed_batters(lineup)


def _recursive_stat_value(obj: Any, keys: Iterable[str], default=None):
    wanted = {str(k).lower() for k in keys}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in wanted and v not in (None, "", "-"):
                return v
        for v in obj.values():
            found = _recursive_stat_value(v, keys, default=None)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _recursive_stat_value(v, keys, default=None)
            if found is not None:
                return found
    return default


def _normalize_stat_from_player_data(player_data: dict) -> Tuple[Optional[int], Optional[float]]:
    if not isinstance(player_data, dict):
        return None, None
    basic = (player_data.get("basicRecord") or {}).get("basic", {}) or {}
    containers = [basic, player_data.get("basicRecord", {}), player_data]
    app_keys = ("games", "game", "gameCount", "appearance", "appearances", "gp", "g", "경기", "등판")
    inn_keys = ("inn", "inning", "innings", "pitInn", "투구이닝", "이닝")
    apps = innings = None
    for obj in containers:
        if apps is None:
            apps = _int(_recursive_stat_value(obj, app_keys, default=None), None)
        if innings is None:
            innings = _number(_recursive_stat_value(obj, inn_keys, default=None), None)
    games = (player_data.get("record") or {}).get("game", []) or []
    if isinstance(games, list) and games:
        valid_games = 0
        total_innings = 0.0
        for g in games:
            if not isinstance(g, dict):
                continue
            gi = _number(_recursive_stat_value(g, inn_keys, default=None), None)
            if gi is not None:
                valid_games += 1
                total_innings += gi
        if apps is None and valid_games:
            apps = valid_games
        if innings is None and valid_games:
            innings = round(total_innings, 1)
    return apps, innings


def _pitcher_appearance_count(player: dict) -> Optional[int]:
    return _int(_recursive_stat_value(player, ("appearance", "appearances", "games", "gameCount", "g", "gp", "경기", "등판"), default=None), None)


def _pitcher_innings(player: dict) -> Optional[float]:
    return _number(_recursive_stat_value(player, ("inn", "inning", "innings", "pitInn", "투구이닝", "이닝"), default=None), None)


# -----------------------------------------------------------------------------
# 불펜 피로도 분석
# -----------------------------------------------------------------------------
def _parse_game_date(game: dict):
    import datetime as _dt

    raw = _first(game, "gdate", "date", "gameDate", "game_date", "regDate", "gameDay", default="")
    text = str(raw or "").strip()
    if len(text) >= 10:
        text = text[:10]
    for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d"):
        try:
            return _dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _recent_pitching_workload(player_data: dict, today=None) -> dict:
    """선수 기록의 game 배열에서 최근 3일/7일 투구 부담을 보수적으로 계산."""
    import datetime as _dt

    today = today or _dt.date.today()
    games = ((player_data.get("record") or {}).get("game") or [])
    if not isinstance(games, list):
        return {
            "recent2Games": 0,
            "recent3Days": 0,
            "recent3DayInnings": 0.0,
            "recent7Days": 0,
            "recent7DayInnings": 0.0,
            "lastGameDate": None,
            "source": "기록 없음",
        }

    recent_3 = []
    recent_7 = []
    last_date = None

    for game in games:
        if not isinstance(game, dict):
            continue
        date = _parse_game_date(game)
        if not date:
            continue

        # 향후/현재 경기 데이터는 제외하고 최근 기록만 사용
        days = (today - date).days
        if days < 0:
            continue

        innings = _number(
            _first(game, "inn", "inning", "innings", "pitInn", "투구이닝", default=0),
            0.0,
        )

        if last_date is None or date > last_date:
            last_date = date

        if days <= 2:
            recent_3.append((date, innings))
        if days <= 6:
            recent_7.append((date, innings))

    recent_3_sorted = sorted(recent_3, key=lambda x: x[0], reverse=True)

    return {
        "recent2Games": min(len(recent_3_sorted), 2),
        "recent3Days": len({d for d, _ in recent_3_sorted}),
        "recent3DayInnings": round(sum(i for _, i in recent_3_sorted), 1),
        "recent7Days": len({d for d, _ in recent_7}),
        "recent7DayInnings": round(sum(i for _, i in recent_7), 1),
        "lastGameDate": last_date.isoformat() if last_date else None,
        "source": "선수 최근 경기 기록",
    }


def _fatigue_grade(workload: dict, season_appearances: int = 0, season_innings: float = 0.0) -> str:
    """최근 등판 기록이 있으면 최근 workload를 우선, 없으면 시즌 workload를 보조 지표로 사용."""
    recent3_days = int(workload.get("recent3Days", 0) or 0)
    recent3_inn = float(workload.get("recent3DayInnings", 0.0) or 0.0)
    recent7_games = int(workload.get("recent7Days", 0) or 0)
    recent7_inn = float(workload.get("recent7DayInnings", 0.0) or 0.0)

    # 최근 3일 연투/다량 투구를 최우선으로 판정
    if recent3_days >= 3 or recent3_inn >= 3.0 or recent7_inn >= 6.0:
        return "높음"
    if recent3_days >= 2 or recent3_inn >= 2.0 or recent7_inn >= 4.0:
        return "보통"

    # 최근 데이터가 부족한 경우 시즌 사용량은 '피로도'가 아니라 참고 workload로만 사용
    if not workload.get("lastGameDate"):
        if season_appearances >= 25 or season_innings >= 25:
            return "참고상 높음"
        if season_appearances >= 15 or season_innings >= 15:
            return "참고상 보통"
        return "판정 보류"

    return "낮음"


def _enrich_bullpen_fatigue(players: List[dict], max_lookup: int = 24) -> List[dict]:
    import datetime as _dt
    enriched=[]
    for idx, player in enumerate(players):
        item=dict(player)
        pid=str(_first(item,"playerId","playerID","playerNo","id",default="") or "").strip()
        apps=_pitcher_appearance_count(item)
        innings=_pitcher_innings(item)
        stats_source="경기 프리뷰"
        workload={"recent2Games":0,"recent3Days":0,"recent3DayInnings":0.0,"recent7Days":0,"recent7DayInnings":0.0,"lastGameDate":None,"source":"기록 없음"}
        if pid and idx < max_lookup:
            try:
                pdata=get_player_data(pid)
                api_apps,api_innings=_normalize_stat_from_player_data(pdata)
                if api_apps is not None: apps=api_apps
                if api_innings is not None: innings=api_innings
                if api_apps is not None or api_innings is not None: stats_source="선수 기록 API"
                workload=_recent_pitching_workload(pdata,_dt.date.today())
            except Exception as exc:
                item["_bullpenLookupError"]=str(exc)
        item["_appearanceCount"]=apps
        item["_innings"]=innings
        item["_statsAvailable"]=apps is not None or innings is not None
        item["_statsSource"]=stats_source if item["_statsAvailable"] else "미확인"
        item["_recentWorkload"]=workload
        item["_fatigueGrade"]=_fatigue_grade(workload,apps or 0,innings or 0.0)
        item["_fatigueSource"]=workload.get("source","기록 없음")
        enriched.append(item)
    enriched.sort(key=lambda p:(1 if not p.get("_statsAvailable") else 0,-(p.get("_appearanceCount") or 0),-(p.get("_innings") or 0.0),str(p.get("playerName", ""))))
    return enriched

def _extract_bullpen(lineup: dict, starter: Optional[dict] = None) -> List[dict]:
    """네이버 응답 구조 차이를 흡수해 불펜 투수를 최대한 복원한다."""
    lineup = lineup or {}
    bullpen = []
    starter = starter or {}
    starter_info = starter.get("playerInfo", {}) or {}
    starter_name = str(_first(starter_info, "name", "playerName", default="") or "").strip()
    starter_id = str(_first(starter_info, "playerId", "playerID", "id", default="") or "").strip()

    candidates = []
    # 정상적인 구조
    for key in ("pitcherBullpen", "pitcherBullPen", "bullpen", "bullpenPitchers", "pitcherList", "pitchers", "pitcher"):
        value = lineup.get(key)
        if isinstance(value, list):
            candidates.extend(value)
        elif isinstance(value, dict):
            # 일부 응답은 player/list/items 아래에 배열을 넣는다.
            for subkey in ("players", "list", "items", "player"):
                sub = value.get(subkey)
                if isinstance(sub, list):
                    candidates.extend(sub)

    # 최후 fallback: fullLineUp에 투수가 섞여 있는 경우
    if not candidates:
        for player in lineup.get("fullLineUp", []) or []:
            if not isinstance(player, dict):
                continue
            pos = str(_first(player, "positionName", "position", "playerType", default="") or "").upper()
            if "투수" in pos or pos in {"P", "SP", "RP", "PITCHER"}:
                candidates.append(player)

    seen = set()
    for player in candidates:
        if not isinstance(player, dict):
            continue
        item = dict(player)
        item["playerId"] = _first(item, "playerId", "playerID", "playerNo", "id")
        item["playerName"] = _first(item, "playerName", "name", default="")
        item["playerType"] = "PITCHER"
        pid = str(item.get("playerId") or "").strip()
        pname = str(item.get("playerName") or "").strip()
        identity = pid or pname
        if not identity or identity in seen:
            continue
        seen.add(identity)
        item["_appearanceCount"] = _pitcher_appearance_count(player)
        item["_innings"] = _pitcher_innings(player)
        item["isStarter"] = bool(
            player.get("isStarter") or player.get("starter") or
            (starter_name and pname == starter_name) or
            (starter_id and pid and pid == starter_id)
        )
        bullpen.append(item)

    def _bullpen_sort_num(value):
        try:
            return float(value) if value is not None else -1.0
        except (TypeError, ValueError):
            return -1.0
    bullpen.sort(key=lambda p: (-_bullpen_sort_num(p.get("_appearanceCount")), -_bullpen_sort_num(p.get("_innings")), str(p.get("playerName", ""))))
    return bullpen




# -----------------------------------------------------------------------------
# 좌/우타 상성 분석
# -----------------------------------------------------------------------------
def _normalize_hand(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().upper()
    if text in {"L", "LEFT", "좌", "좌타"}:
        return "L"
    if text in {"R", "RIGHT", "우", "우타"}:
        return "R"
    return None


def _extract_batting_hand(player: dict) -> Optional[str]:
    """
    경기 라인업 데이터에 명시된 좌/우타 정보를 우선 확인.
    실제 정보가 없으면 None을 반환하며 임의 추정하지 않는다.
    """
    return _normalize_hand(_first(
        player,
        "battingHand", "batterHand", "hittingHand",
        "batSide", "hitSide", "battingSide",
        "타격손", "타격방향", "좌우타",
        default=None,
    ))


def _extract_pitching_hand(starter: dict) -> Optional[str]:
    info = starter.get("playerInfo", {}) or {}
    candidates = [
        _first(info, "throwingHand", "pitchHand", "pitchingHand", "throws", "투구손", "좌우", default=None),
        _first(starter, "throwingHand", "pitchHand", "pitchingHand", default=None),
    ]
    for value in candidates:
        hand = _normalize_hand(value)
        if hand:
            return hand
    return None


def _load_hands_for_batters(batters: List[dict], max_lookup: int = 18) -> List[dict]:
    enriched = []
    for index, batter in enumerate(batters):
        item = dict(batter)
        hand = _extract_batting_hand(item)
        pid = str(_first(item, "playerId", "playerID", "playerNo", "id", default="") or "").strip()

        if hand is None and pid and index < max_lookup:
            try:
                pdata = get_player_data(pid)
                hand = _normalize_hand(pdata.get("battingHand"))
            except Exception:
                pass

        item["_battingHand"] = hand
        enriched.append(item)

    return enriched


def _build_handedness_matchup(side: dict, opponent: dict) -> dict:
    """
    side 타선이 opponent 선발을 상대로 좌/우 상성을 어떻게 구성하는지 요약.
    선발 투수의 투구손 정보가 없으면 분석을 보류한다.
    """
    starter = side.get("starter", {})
    pitch_hand = _extract_pitching_hand(starter)

    # 실제 상대투수는 opponent 쪽 선발
    opponent_pitch_hand = _extract_pitching_hand(opponent.get("starter", {}) or {})
    if opponent_pitch_hand is None:
        opponent_pitch_hand = pitch_hand

    batters = side.get("batters", []) or []
    left = [p for p in batters if p.get("_battingHand") == "L"]
    right = [p for p in batters if p.get("_battingHand") == "R"]
    unknown = [p for p in batters if not p.get("_battingHand")]

    result = {
        "pitcherHand": opponent_pitch_hand,
        "leftCount": len(left),
        "rightCount": len(right),
        "unknownCount": len(unknown),
        "leftBatters": [str(p.get("playerName") or p.get("name") or "") for p in left],
        "rightBatters": [str(p.get("playerName") or p.get("name") or "") for p in right],
        "status": "확인 가능" if opponent_pitch_hand and len(left) + len(right) >= 5 else "데이터 부족",
    }

    # 단순한 구조상 우세 판단은 하지 않는다.
    # 좌/우타의 실제 상대 유형별 성적이 있는 경우에만 다음 단계에서 정량화한다.
    return result


def _team_result_from_game(team_name: str, game: dict) -> Optional[str]:
    result = str(game.get("result", "")).strip().upper()
    if result.startswith("W") or "승" in result:
        return "W"
    if result.startswith("D") or result.startswith("T") or "무" in result:
        return "D"
    if result.startswith("L") or "패" in result:
        return "L"

    h = _number(game.get("hScore"), None)
    a = _number(game.get("aScore"), None)
    if h is None or a is None:
        return None

    h_name = str(game.get("hName", ""))
    a_name = str(game.get("aName", ""))
    if team_name == h_name:
        team_score, opp_score = h, a
    elif team_name == a_name:
        team_score, opp_score = a, h
    else:
        return None

    if team_score > opp_score:
        return "W"
    if team_score < opp_score:
        return "L"
    return "D"


def _record_from_games(team_name: str, games: list, venue: Optional[str] = None) -> dict:
    w = d = l = 0
    usable = 0

    for game in games or []:
        if not isinstance(game, dict):
            continue

        h_name = str(game.get("hName", ""))
        a_name = str(game.get("aName", ""))
        is_home = team_name == h_name
        is_away = team_name == a_name

        if venue == "home" and not is_home:
            continue
        if venue == "away" and not is_away:
            continue

        result = _team_result_from_game(team_name, game)
        if result is None:
            continue

        usable += 1
        if result == "W":
            w += 1
        elif result == "D":
            d += 1
        else:
            l += 1

    total = w + d + l
    winrate = (w / total * 100) if total else None
    return {
        "w": w,
        "d": d,
        "l": l,
        "total": total,
        "winrate": round(winrate, 1) if winrate is not None else None,
        "sampleSize": usable,
    }


def _extract_text_values(obj: Any, keys: Tuple[str, ...], out: List[str], max_items=12):
    """중첩 preview 데이터에서 결장/코멘트 계열 문자열을 보수적으로 추출."""
    if len(out) >= max_items:
        return

    if isinstance(obj, dict):
        for key, value in obj.items():
            key_text = str(key).lower()
            matched = any(token.lower() in key_text for token in keys)
            if matched:
                if isinstance(value, str) and value.strip():
                    cleaned = " ".join(value.split())
                    if cleaned and cleaned not in out:
                        out.append(cleaned)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, str) and item.strip():
                            cleaned = " ".join(item.split())
                            if cleaned and cleaned not in out:
                                out.append(cleaned)
                        elif isinstance(item, dict):
                            # 이름/사유처럼 화면에 쓸 수 있는 최소 정보가 있으면 조합
                            name = _first(item, "playerName", "name", "선수명")
                            reason = _first(item, "reason", "status", "description", "사유", "상태")
                            if name or reason:
                                line = " · ".join(str(x) for x in (name, reason) if x not in (None, "", "-"))
                                if line and line not in out:
                                    out.append(line)
            if len(out) >= max_items:
                return
            _extract_text_values(value, keys, out, max_items)

    elif isinstance(obj, list):
        for item in obj:
            if len(out) >= max_items:
                return
            _extract_text_values(item, keys, out, max_items)


def _extract_absence_and_comments(preview: dict, home_side: dict, away_side: dict) -> Tuple[List[str], List[str]]:
    absences: List[str] = []
    comments: List[str] = []

    source = {
        "preview": preview,
        "home": home_side,
        "away": away_side,
    }

    _extract_text_values(
        source,
        ("injury", "injuries", "absence", "absent", "inactive", "out", "결장", "부상", "출전불가"),
        absences,
    )
    _extract_text_values(
        source,
        ("comment", "comments", "previewcomment", "gamecomment", "analysis", "commentary", "코멘트", "분석", "경기전망"),
        comments,
    )
    return absences, comments


def _team_metrics(side: dict) -> dict:
    standings = side.get("standings", {}) or {}
    preview_metrics = side.get("previewTeamMetrics", {}) or {}

    era = _first(
        preview_metrics, "era", "teamEra", "teamERA", "avgEra", "averageEra",
        default=_first(standings, "era", "teamEra", "teamERA", "avgEra", "averageEra")
    )
    avg = _first(
        preview_metrics, "hra", "avg", "battingAverage", "batAvg", "teamBattingAverage",
        default=_first(standings, "hra", "avg", "battingAverage", "batAvg", "teamBattingAverage")
    )

    return {
        "era": era,
        "battingAverage": avg,
        "wins": _first(preview_metrics, "w", "wins", default=_first(standings, "w", "wins")),
        "losses": _first(preview_metrics, "l", "losses", default=_first(standings, "l", "losses")),
        "draws": _first(preview_metrics, "d", "draws", default=_first(standings, "d", "draws")),
        "winRate": _first(preview_metrics, "wra", "winRate", "winrate", default=_first(standings, "wra", "winRate", "winrate")),
        "rank": _first(preview_metrics, "rank", default=_first(standings, "rank")),
    }




# -----------------------------------------------------------------------------
# 데이터 신뢰도
# -----------------------------------------------------------------------------
def _confidence_status(ok: bool) -> str:
    return "확보" if ok else "미확인"


def _build_side_confidence(side: dict) -> dict:
    batters = side.get("batters", []) or []
    bullpen = side.get("bullpenAnalysis", []) or []
    starter = side.get("starter", {}) or {}
    s_info = starter.get("playerInfo", {}) or {}
    metrics = side.get("teamMetrics", {}) or {}

    lineup_ok = len(batters) >= 9
    lineup_status = "확정" if side.get("lineupIsToday") and lineup_ok else (
        "참고 라인업" if lineup_ok else "불완전"
    )
    starter_ok = bool(_first(s_info, "name", "playerName", default=""))
    bullpen_ok = len(bullpen) > 0
    era_ok = _first(metrics, "era", default=None) not in (None, "", "-")
    avg_ok = _first(metrics, "battingAverage", default=None) not in (None, "", "-")
    return {
        "lineup": lineup_status,
        "starter": _confidence_status(starter_ok),
        "bullpen": _confidence_status(bullpen_ok),
        "teamERA": _confidence_status(era_ok),
        "teamAVG": _confidence_status(avg_ok),
    }


def _build_data_confidence(report: dict) -> dict:
    home = report.get("home", {}) or {}
    away = report.get("away", {}) or {}
    info = report.get("gameInfo", {}) or {}
    hc = _build_side_confidence(home)
    ac = _build_side_confidence(away)

    checks = {
        "경기 일정": bool(_first(info, "gdate", "date", default=None) and _first(info, "gtime", "time", default=None)),
        "구장": bool(_first(info, "stadium", "venue", default=None)),
        "홈 라인업": hc["lineup"] in ("확정", "참고 라인업"),
        "원정 라인업": ac["lineup"] in ("확정", "참고 라인업"),
        "홈 선발": hc["starter"] == "확보",
        "원정 선발": ac["starter"] == "확보",
        "홈 불펜": hc["bullpen"] == "확보",
        "원정 불펜": ac["bullpen"] == "확보",
        "홈 ERA": hc["teamERA"] == "확보",
        "원정 ERA": ac["teamERA"] == "확보",
        "홈 타율": hc["teamAVG"] == "확보",
        "원정 타율": ac["teamAVG"] == "확보",
        "최근전적": len(home.get("previousGames", []) or []) >= 3 and len(away.get("previousGames", []) or []) >= 3,
        "결장 데이터": bool(report.get("absenceData") is not None or (report.get("analysis", {}) or {}).get("absences") is not None),
        "날씨": isinstance(report.get("weather"), dict),
    }

    total = len(checks)
    score = round(sum(checks.values()) / total * 100) if total else 0
    grade = "높음" if score >= 90 else "양호" if score >= 75 else "보통" if score >= 55 else "낮음"
    return {
        "score": score,
        "grade": grade,
        "checks": checks,
        "home": hc,
        "away": ac,
    }


def build_matchup_report(date: str, team_a: str, team_b: str):
    game = find_matchup(date, team_a, team_b)
    if not game:
        raise MatchupReportError(f"{date}에 {team_a} vs {team_b} 경기를 찾을 수 없습니다.")

    game_id = game.get("gameId")
    if not game_id:
        raise MatchupReportError("경기 ID를 찾을 수 없습니다.")

    try:
        preview = fetch_game_preview(game_id)
    except Exception as e:
        raise MatchupReportError(f"경기 상세 정보를 불러오지 못했습니다: {e}")

    game_info = preview.get("gameInfo", {}) or {}

    home_side = {
        "standings": preview.get("homeStandings", {}) or {},
        "starter": preview.get("homeStarter", {}) or {},
        "lineup": preview.get("homeTeamLineUp", {}) or {},
        "previousGames": preview.get("homeTeamPreviousGames", []) or [],
        "topPlayer": preview.get("homeTopPlayer", {}) or {},
        "previewTeamMetrics": preview.get("homeTeamMetrics", {}) or {},
        "lineupIsToday": True,
        "lineupDate": None,
        "teamCode": game.get("homeTeamCode") or game_info.get("hCode"),
    }
    away_side = {
        "standings": preview.get("awayStandings", {}) or {},
        "starter": preview.get("awayStarter", {}) or {},
        "lineup": preview.get("awayTeamLineUp", {}) or {},
        "previousGames": preview.get("awayTeamPreviousGames", []) or [],
        "topPlayer": preview.get("awayTopPlayer", {}) or {},
        "previewTeamMetrics": preview.get("awayTeamMetrics", {}) or {},
        "lineupIsToday": True,
        "lineupDate": None,
        "teamCode": game.get("awayTeamCode") or game_info.get("aCode"),
    }

    # 오늘 라인업이 공개되지 않은 경우 최근 경기 라인업을 사용
    for side, team_code in [
        (home_side, game.get("homeTeamCode") or game_info.get("hCode")),
        (away_side, game.get("awayTeamCode") or game_info.get("aCode")),
    ]:
        batters_now = _extract_batters(side["lineup"])
        team_name = str(side.get("standings", {}).get("name") or "").strip()
        # 1~9번이 모두 확정되지 않았다면 최근 경기 라인업으로 대체
        if len(batters_now) < 9:
            fallback_lineup, fallback_date = get_fallback_lineup(team_code, side["previousGames"], team_name)
            if fallback_lineup:
                side["lineup"] = fallback_lineup
                side["lineupIsToday"] = False
                side["lineupDate"] = fallback_date

    # UI가 바로 사용할 수 있는 표준 데이터 추가
    for side in (home_side, away_side):
        team_name = str(side.get("standings", {}).get("name") or "")
        games = side.get("previousGames", []) or []
        side["batters"] = _extract_batters(side.get("lineup", {}))
        side["batters"] = _load_hands_for_batters(side["batters"])
        side["bullpenAnalysis"] = _extract_bullpen(side.get("lineup", {}), side.get("starter", {}))
        side["bullpenAnalysis"] = _enrich_bullpen_fatigue(side["bullpenAnalysis"])
        # 선수 상세 조회에서 동명이인을 구분할 수 있도록 팀 코드를 각 선수 데이터에 붙인다.
        for player in side["batters"]:
            player.setdefault("_teamCode", side.get("teamCode"))
        for player in side["bullpenAnalysis"]:
            player.setdefault("_teamCode", side.get("teamCode"))
            player["playerType"] = "PITCHER"
            if not player.get("playerName") and player.get("name"):
                player["playerName"] = player.get("name")
            # 불펜 데이터는 API에 playerType/positionName이 없는 경우가 있어
            # 선수 ID 검색 시 반드시 투수로 판별되도록 표준화한다.
            player["playerType"] = "PITCHER"
            if not player.get("playerName") and player.get("name"):
                player["playerName"] = player.get("name")
        starter_info = side.get("starter", {}).get("playerInfo", {}) or {}
        if starter_info:
            starter_info.setdefault("_teamCode", side.get("teamCode"))
        side["teamMetrics"] = _team_metrics(side)
        side["recentRecord"] = _record_from_games(team_name, games)
        side["homeRecord"] = _record_from_games(team_name, games, venue="home")
        side["awayRecord"] = _record_from_games(team_name, games, venue="away")
    home_side["handednessMatchup"] = _build_handedness_matchup(home_side, away_side)
    away_side["handednessMatchup"] = _build_handedness_matchup(away_side, home_side)

    absences, comments = _extract_absence_and_comments(preview, home_side, away_side)
    # 신뢰도 계산용으로 실제 결장 데이터 존재 여부를 보존
    home_side["absences"] = absences
    away_side["absences"] = absences
    report_context = {
        "gameInfo": game_info,
        "home": home_side,
        "away": away_side,
        "absenceData": preview.get("absenceData", preview.get("injuries", preview.get("absences"))),
        "analysis": {"absences": absences, "comments": comments},
        "weather": preview.get("weather"),
    }
    data_confidence = _build_data_confidence(report_context)

    return {
        "gameId": game_id,
        "gameInfo": game_info,
        "dataConfidence": data_confidence,
        "home": home_side,
        "away": away_side,
        "seasonVsResult": preview.get("seasonVsResult", {}) or {},
        "analysis": {
            "absences": absences,
            "comments": comments,
        },
        # preview가 별도 키로 제공하는 경우 기존 값을 그대로 보존
        "gameComments": preview.get("gameComments", preview.get("comments", [])),
        "absenceData": preview.get("absenceData", preview.get("injuries", preview.get("absences", []))),
    }


def print_report_debug(report: dict):
    info = report["gameInfo"]
    print(
        f"{info.get('gdate')} {info.get('gtime', '')} "
        f"{info.get('aName')} @ {info.get('hName')} ({info.get('stadium')})"
    )
    print()

    for side_key in ["away", "home"]:
        side = report[side_key]
        st = side["standings"]
        starter = side["starter"]
        print(f"--- {st.get('name')} ({st.get('rank')}위, 승률 {st.get('wra')}) ---")

        s_basic = starter.get("currentSeasonStats", {})
        s_info = starter.get("playerInfo", {})
        print(
            f"  선발: {s_info.get('name')} ERA {s_basic.get('era')} "
            f"{s_basic.get('w')}승{s_basic.get('l')}패 WHIP {s_basic.get('whip')}"
        )

        vs_opp = starter.get("currentSeasonStatsOnOpponents", {})
        if vs_opp:
            print(
                f"    상대팀전 성적: {vs_opp.get('gameCount')}경기 "
                f"ERA {vs_opp.get('era')} {vs_opp.get('w')}승{vs_opp.get('l')}패"
            )

        label = "오늘 확정 라인업" if side["lineupIsToday"] else f"참고용 ({side['lineupDate']} 라인업)"
        print(f"  라인업 [{label}] ({len(side['batters'])}명):")
        for batter in side["batters"]:
            print(
                f"    {batter.get('batorder')}번 {batter.get('positionName')} "
                f"{batter.get('playerName')}"
            )

        print("  불펜 [등판 수 → 이닝]:")
        for pitcher in side["bullpenAnalysis"]:
            print(
                f"    {pitcher.get('playerName')} "
                f"{pitcher.get('_appearanceCount', 0)}경기 "
                f"{pitcher.get('_innings', 0)}이닝"
            )

        print(f"  최근전적: {side['recentRecord']}")
        print(f"  홈전적:   {side['homeRecord']}")
        print(f"  원정전적: {side['awayRecord']}")
        print(f"  팀 지표:  {side['teamMetrics']}")
        print()

    analysis = report.get("analysis", {})
    print("[결장/부상]")
    for value in analysis.get("absences", []) or []:
        print(f"  - {value}")
    print("[경기 코멘트/분석]")
    for value in analysis.get("comments", []) or []:
        print(f"  - {value}")

    vs = report.get("seasonVsResult")
    if vs:
        print(f"[시즌 상대전적] {vs.get('hCode')} {vs.get('hw')}승 {vs.get('hd')}무 {vs.get('hl')}패")


if __name__ == "__main__":
    report = build_matchup_report("2026-08-16", "롯데", "NC")
    print_report_debug(report)
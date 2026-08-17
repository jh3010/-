import requests
import json
from functools import lru_cache

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://m.sports.naver.com/",
}

PREVIEW_URL = "https://api-gw.sports.naver.com/players/{category}/{player_id}/playerend-record"


def fetch_player_record(player_id: str, category: str = "kbo"):
    url = PREVIEW_URL.format(category=category, player_id=player_id)
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("success"):
        raise ValueError(f"응답 실패: {data}")

    return data["result"]


def _safe_load(raw):
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _find_hand(obj, max_depth=5, _depth=0):
    """
    선수 데이터에서 타격/투구 방향을 보수적으로 탐색한다.
    반환값은 L/R 또는 None.
    """
    if _depth > max_depth:
        return None

    if isinstance(obj, dict):
        for key, value in obj.items():
            k = str(key).strip().lower()
            if any(token in k for token in (
                "batterhand", "hittinghand", "hitterhand",
                "battinghand", "bat_hand", "hit_hand",
                "batting_side", "hitting_side", "hitter_side",
                "타격손", "타격방향", "좌우타", "타자손",
            )):
                val = str(value).strip().upper()
                if val in {"L", "LEFT", "좌", "좌타"}:
                    return "L"
                if val in {"R", "RIGHT", "우", "우타"}:
                    return "R"

        # 흔히 사용되는 간단한 필드도 확인
        for key in ("bats", "bat", "hitting", "hit", "batSide", "hitSide"):
            if key in obj:
                val = str(obj[key]).strip().upper()
                if val in {"L", "LEFT", "좌", "좌타"}:
                    return "L"
                if val in {"R", "RIGHT", "우", "우타"}:
                    return "R"

        for value in obj.values():
            found = _find_hand(value, max_depth, _depth + 1)
            if found:
                return found

    elif isinstance(obj, list):
        for value in obj:
            found = _find_hand(value, max_depth, _depth + 1)
            if found:
                return found

    return None


def parse_player_record(result: dict):
    basic_record = _safe_load(result.get("basicRecord"))
    record = _safe_load(result.get("record"))
    chart = _safe_load(result.get("chart"))
    vs_team = _safe_load(result.get("vsTeam"))

    # 원본 metadata에서 좌/우타 정보가 있으면 보존
    batting_hand = _find_hand(result)
    if batting_hand is None:
        batting_hand = _find_hand(basic_record)

    return {
        "playerId": result.get("playerId"),
        "year": result.get("year"),
        "playerType": result.get("playerType"),
        "teamCode": result.get("teamCode"),
        "battingHand": batting_hand,
        "basicRecord": basic_record,
        "record": record,
        "chart": chart,
        "vsTeam": vs_team,
        "rawMeta": {
            "playerId": result.get("playerId"),
            "teamCode": result.get("teamCode"),
            "playerType": result.get("playerType"),
        },
    }


@lru_cache(maxsize=512)
def get_player_data(player_id: str):
    raw = fetch_player_record(str(player_id))
    return parse_player_record(raw)

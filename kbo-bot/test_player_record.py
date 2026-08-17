"""
KBO 선수 상세 기록 테스트 스크립트
"""

import requests
import json

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://m.sports.naver.com/",
}


def fetch_player_record(player_id: str, category: str = "kbo"):
    url = f"https://api-gw.sports.naver.com/players/{category}/{player_id}/playerend-record"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("success"):
        raise ValueError(f"응답 실패: {data}")

    return data["result"]


def parse_player_record(result: dict):
    def safe_load(key):
        raw = result.get(key)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}

    parsed = {
        "playerId": result.get("playerId"),
        "year": result.get("year"),
        "playerType": result.get("playerType"),
        "teamCode": result.get("teamCode"),
        "basicRecord": safe_load("basicRecord"),
        "record": safe_load("record"),
        "chart": safe_load("chart"),
        "vsTeam": safe_load("vsTeam"),
    }
    return parsed


def print_summary(parsed: dict):
    print("=" * 50)
    print(f"선수 ID: {parsed['playerId']} | 시즌: {parsed['year']} | 팀: {parsed['teamCode']}")
    print("=" * 50)

    basic = parsed["basicRecord"].get("basic", {})
    print("\n[기본 기록]")
    print(f"  ERA {basic.get('era')}  WHIP {basic.get('whip')}  "
          f"승-패 {basic.get('w')}-{basic.get('l')}  "
          f"이닝 {basic.get('inn')}  탈삼진 {basic.get('kk')}")

    pit_kind = parsed["chart"].get("pit_kind", {}).get("player", {})
    print("\n[구종별 구속/구사율]")
    for code, info in pit_kind.items():
        speed = info.get("speed")
        rate = info.get("pit_rt")
        if speed and speed != "-":
            rate_str = f"{rate}%" if rate is not None else "구사율 정보 없음"
            print(f"  {info.get('pit')}: {speed}km/h ({rate_str})")

    games = parsed["record"].get("game", [])
    print(f"\n[최근 경기 기록] (최신 {min(5, len(games))}경기)")
    for g in games[:5]:
        print(f"  {g.get('gday')} vs {g.get('opponent'):4s}  "
              f"{g.get('inn'):>5s}이닝  실점 {g.get('r')}  자책 {g.get('er')}  "
              f"탈삼진 {g.get('kk')}  ERA {g.get('era')}")

    vs_teams = parsed["vsTeam"].get("vsteam", [])
    print(f"\n[팀별 상대전적] ({len(vs_teams)}개 팀)")
    for v in vs_teams:
        print(f"  vs {v.get('name'):4s}  ERA {v.get('era')}  "
              f"{v.get('w')}승 {v.get('l')}패  탈삼진 {v.get('kk')}")


if __name__ == "__main__":
    TEST_PLAYER_ID = "67954"  # 김진호

    print(f"선수 ID {TEST_PLAYER_ID} 상세 기록 조회 중...\n")
    raw_result = fetch_player_record(TEST_PLAYER_ID)
    parsed = parse_player_record(raw_result)
    print_summary(parsed)

    with open("test_player_record.json", "w", encoding="utf-8") as f:
        json.dump(parsed, f, ensure_ascii=False, indent=2)
    print("\n\n전체 파싱 결과는 test_player_record.json 에 저장했습니다.")
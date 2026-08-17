"""
KBO 전체 선수 ID 수집 스크립트

네이버 스포츠 비공식 API를 이용해서 10개 구단의 타자/투수 명단을
전부 긁어와 players.json 파일로 저장한다.
"""

import requests
import json
import time

BASE_URL = "https://api-gw.sports.naver.com/statistics/categories/kbo/seasons/{year}/players"

TEAM_CODES = ["NC", "OB", "LG", "HH", "SS", "LT", "HT", "WO", "SK", "KT"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://m.sports.naver.com/",
}


def fetch_players(team_code: str, player_type: str, year: int = 2026):
    params = {
        "teamCode": team_code,
        "sortField": "hitterHra" if player_type == "HITTER" else "era",
        "sortDirection": "desc",
        "playerType": player_type,
        "gameType": "REGULAR_SEASON",
    }
    url = BASE_URL.format(year=year)
    resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("success"):
        print(f"  [경고] {team_code} {player_type} 응답 실패: {data}")
        return []

    return data["result"]["seasonPlayerStats"]


def collect_all_players(year: int = 2026):
    all_players = {}

    for team_code in TEAM_CODES:
        for player_type in ["HITTER", "PITCHER"]:
            print(f"수집 중: {team_code} - {player_type}")
            try:
                players = fetch_players(team_code, player_type, year)
            except Exception as e:
                print(f"  [에러] {team_code} {player_type}: {e}")
                continue

            for p in players:
                pid = p["playerId"]
                all_players[pid] = {
                    "playerId": pid,
                    "playerName": p["playerName"],
                    "teamId": p["teamId"],
                    "backNumber": p.get("backNumber"),
                    "playerType": player_type,
                }

            time.sleep(0.5)

    return all_players


if __name__ == "__main__":
    players = collect_all_players(year=2026)
    print(f"\n총 {len(players)}명의 선수를 수집했습니다.")

    with open("players.json", "w", encoding="utf-8") as f:
        json.dump(list(players.values()), f, ensure_ascii=False, indent=2)

    print("players.json 파일로 저장 완료.")
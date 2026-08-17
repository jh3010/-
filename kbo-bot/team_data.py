"""
팀 순위/시즌 성적 조회 모듈
"""

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://m.sports.naver.com/",
}

TEAMS_URL = "https://api-gw.sports.naver.com/statistics/categories/kbo/seasons/{year}/teams"


def fetch_all_teams(year: int = 2026):
    url = TEAMS_URL.format(year=year)
    params = {"gameType": "REGULAR_SEASON"}
    resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("success"):
        raise ValueError(f"응답 실패: {data}")

    return data["result"]["seasonTeamStats"]


def get_team_stats(team_code: str, year: int = 2026):
    teams = fetch_all_teams(year)
    for t in teams:
        if t["teamId"] == team_code:
            return t
    return None
from schedule_data import find_matchup
from player_search import find_player_by_name

if __name__ == "__main__":
    DATE = "2026-08-16"
    TEAM_A = "롯데"
    TEAM_B = "NC"

    print(f"{DATE} {TEAM_A} vs {TEAM_B} 경기 찾는 중...\n")
    game = find_matchup(DATE, TEAM_A, TEAM_B)

    if not game:
        print("경기를 찾지 못했습니다.")
        exit()

    print("경기 찾음:")
    print(f"  {game['awayTeamName']} @ {game['homeTeamName']}  ({game['stadium']})")
    print(f"  홈 예고선발: {game.get('homeStarterName')}")
    print(f"  원정 예고선발: {game.get('awayStarterName')}")

    print("\n선수 ID 매칭 시도 중...\n")

    for label, name, team in [
        ("홈 선발", game.get("homeStarterName"), game.get("homeTeamCode")),
        ("원정 선발", game.get("awayStarterName"), game.get("awayTeamCode")),
    ]:
        result = find_player_by_name(name, team_code=team)
        print(f"[{label}] '{name}' ({team}) 검색 결과:")
        if result is None:
            print("  -> 매칭 실패 (수동 확인 필요)")
        elif isinstance(result, list):
            print(f"  -> 동명이인/다중 후보 {len(result)}명:")
            for r in result:
                print(f"     playerId={r['playerId']}  이름={r['playerName']}")
        else:
            print(f"  -> playerId={result['playerId']}  이름={result['playerName']}")
        print()
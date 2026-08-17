import json

PLAYERS_FILE = "players.json"


def load_players():
    with open(PLAYERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def find_player_by_name(name: str, team_code: str = None):
    players = load_players()

    if team_code:
        players = [p for p in players if p["teamId"] == team_code]

    exact = [p for p in players if p["playerName"] == name]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return exact

    partial = [p for p in players if name in p["playerName"] or p["playerName"] in name]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        return partial

    return None
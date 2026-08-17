import requests
from typing import Any, List

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://m.sports.naver.com/",
}

PREVIEW_URL = "https://api-gw.sports.naver.com/schedule/games/{game_id}/preview"


def fetch_game_preview(game_id: str):
    url = PREVIEW_URL.format(game_id=game_id)

    resp = requests.get(
        url,
        headers=HEADERS,
        timeout=10,
    )

    resp.raise_for_status()

    data = resp.json()

    if not data.get("success"):
        raise ValueError(
            f"응답 실패: {data}"
        )

    result = data.get(
        "result",
        {},
    ) or {}

    preview = result.get(
        "previewData"
    )

    if not isinstance(preview, dict):
        raise ValueError(
            f"previewData가 없습니다: {data}"
        )

    result_data = dict(preview)

    def first_value(*values):
        for value in values:
            if value not in (
                None,
                "",
                "-",
            ):
                return value
        return None

    def walk_dicts(value: Any):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from walk_dicts(child)

        elif isinstance(value, list):
            for child in value:
                yield from walk_dicts(child)

    def find_values_by_keys(
        root: Any,
        keys: set,
    ) -> List[Any]:
        found = []

        for item in walk_dicts(root):
            for key in keys:
                if key in item:
                    value = item[key]

                    if value not in (
                        None,
                        "",
                        [],
                        {},
                    ):
                        found.append(value)

        return found

    def find_first_dict(
        root: Any,
        keys: set,
    ):
        for item in walk_dicts(root):
            if any(
                key in item
                for key in keys
            ):
                return item

        return {}

    # ========================================================
    # 팀 지표
    # ========================================================
    def normalize_team_metrics(
        team: dict,
    ) -> dict:
        if not isinstance(team, dict):
            return {}

        standings = (
            team.get("standings")
            if isinstance(
                team.get("standings"),
                dict,
            )
            else team
        )

        metrics = dict(
            standings
        )

        nested = find_first_dict(
            team,
            {
                "hra",
                "avg",
                "battingAverage",
                "teamBattingAverage",
                "era",
                "teamEra",
            },
        )

        metrics["rank"] = first_value(
            standings.get("rank"),
            team.get("rank"),
        )

        metrics["w"] = first_value(
            standings.get("w"),
            team.get("w"),
        )

        metrics["l"] = first_value(
            standings.get("l"),
            team.get("l"),
        )

        metrics["d"] = first_value(
            standings.get("d"),
            standings.get("draw"),
            standings.get("draws"),
            team.get("d"),
        )

        metrics["wra"] = first_value(
            standings.get("wra"),
            team.get("wra"),
        )

        metrics["era"] = first_value(
            standings.get("era"),
            standings.get("teamEra"),
            team.get("era"),
            team.get("teamEra"),
            nested.get("era"),
            nested.get("teamEra"),
        )

        metrics["hra"] = first_value(
            standings.get("hra"),
            standings.get("avg"),
            standings.get("battingAverage"),
            standings.get("teamBattingAverage"),
            team.get("hra"),
            team.get("avg"),
            team.get("battingAverage"),
            team.get("teamBattingAverage"),
            nested.get("hra"),
            nested.get("avg"),
            nested.get("battingAverage"),
            nested.get("teamBattingAverage"),
        )

        return metrics

    home_standings = (
        result_data.get(
            "homeStandings"
        )
        or {}
    )

    away_standings = (
        result_data.get(
            "awayStandings"
        )
        or {}
    )

    result_data["homeTeamMetrics"] = (
        normalize_team_metrics(
            home_standings
        )
    )

    result_data["awayTeamMetrics"] = (
        normalize_team_metrics(
            away_standings
        )
    )

    # ========================================================
    # 라인업
    # ========================================================
    def normalize_lineup(
        lineup: Any,
    ) -> dict:
        if not isinstance(
            lineup,
            dict,
        ):
            return {}

        normalized = dict(
            lineup
        )

        players = (
            lineup.get(
                "fullLineUp"
            )
            or []
        )

        normalized_players = []

        if isinstance(
            players,
            list,
        ):
            for player in players:
                if not isinstance(
                    player,
                    dict,
                ):
                    continue

                p = dict(
                    player
                )

                p["batorder"] = (
                    first_value(
                        p.get("batorder"),
                        p.get("batOrder"),
                    )
                )

                p["playerName"] = (
                    first_value(
                        p.get("playerName"),
                        p.get("name"),
                        p.get("playerNm"),
                        "이름 없음",
                    )
                )

                p["playerId"] = (
                    first_value(
                        p.get("playerId"),
                        p.get("playerID"),
                        p.get("id"),
                        p.get("playerNo"),
                    )
                )

                p["positionName"] = (
                    first_value(
                        p.get("positionName"),
                        p.get("positionNm"),
                        p.get("position"),
                        "-",
                    )
                )

                normalized_players.append(
                    p
                )

        normalized[
            "fullLineUp"
        ] = normalized_players

        # ====================================================
        # 불펜
        # ====================================================
        bullpen = (
            lineup.get(
                "pitcherBullpen"
            )
            or []
        )

        normalized_bullpen = []

        if isinstance(
            bullpen,
            list,
        ):
            for pitcher in bullpen:
                if not isinstance(
                    pitcher,
                    dict,
                ):
                    continue

                p = dict(
                    pitcher
                )

                p["playerName"] = (
                    first_value(
                        p.get("playerName"),
                        p.get("name"),
                        p.get("playerNm"),
                        "이름 없음",
                    )
                )

                p["playerId"] = (
                    first_value(
                        p.get("playerId"),
                        p.get("playerID"),
                        p.get("id"),
                        p.get("playerNo"),
                    )
                )

                p["appearances"] = (
                    first_value(
                        p.get("appearances"),
                        p.get("appearance"),
                        p.get("gameCount"),
                        p.get("g"),
                        p.get("games"),
                        p.get("gp"),
                    )
                )

                p["innings"] = (
                    first_value(
                        p.get("innings"),
                        p.get("inn"),
                        p.get("inning"),
                    )
                )

                p["era"] = (
                    first_value(
                        p.get("era"),
                        p.get("ERA"),
                    )
                )

                normalized_bullpen.append(
                    p
                )

        def numeric(
            value,
        ):
            try:
                return float(
                    value
                )
            except (
                TypeError,
                ValueError,
            ):
                return -1.0

        normalized_bullpen.sort(
            key=lambda x: (
                numeric(
                    x.get(
                        "appearances"
                    )
                ),
                numeric(
                    x.get(
                        "innings"
                    )
                ),
            ),
            reverse=True,
        )

        normalized[
            "pitcherBullpen"
        ] = normalized_bullpen

        return normalized

    if "homeTeamLineUp" in result_data:
        result_data[
            "homeTeamLineUp"
        ] = normalize_lineup(
            result_data.get(
                "homeTeamLineUp"
            )
        )

    if "awayTeamLineUp" in result_data:
        result_data[
            "awayTeamLineUp"
        ] = normalize_lineup(
            result_data.get(
                "awayTeamLineUp"
            )
        )

    # ========================================================
    # 결장
    # ========================================================
    absence_keys = {
        "absentPlayers",
        "absencePlayers",
        "missingPlayers",
        "injuryPlayers",
        "injuredPlayers",
        "outPlayers",
        "notAvailablePlayers",
        "playersOut",
        "결장자",
        "부상자",
    }

    absence_values = find_values_by_keys(
        result_data,
        absence_keys,
    )

    result_data[
        "analysisAbsences"
    ] = (
        absence_values[0]
        if absence_values
        else []
    )

    # ========================================================
    # 코멘트
    # ========================================================
    comment_keys = {
        "comment",
        "comments",
        "gameComment",
        "gameComments",
        "previewComment",
        "previewComments",
        "analysisComment",
        "analysisComments",
        "gameCommentary",
        "commentary",
        "코멘트",
        "경기코멘트",
    }

    comment_values = find_values_by_keys(
        result_data,
        comment_keys,
    )

    result_data[
        "analysisComments"
    ] = (
        comment_values[0]
        if comment_values
        else []
    )

    # ========================================================
    # 정규화된 경기 정보 추출
    # schedule_data.py에서 사용
    # ========================================================
    game_info = find_first_dict(
        result_data,
        {
            "hName",
            "aName",
            "homeTeamName",
            "awayTeamName",
            "hCode",
            "aCode",
            "homeTeamCode",
            "awayTeamCode",
        },
    )

    result_data["_gameInfoNormalized"] = {
        "homeName": first_value(
            result_data.get("hName"),
            result_data.get("homeTeamName"),
            game_info.get("hName"),
            game_info.get("homeTeamName"),
        ),
        "awayName": first_value(
            result_data.get("aName"),
            result_data.get("awayTeamName"),
            game_info.get("aName"),
            game_info.get("awayTeamName"),
        ),
        "homeCode": first_value(
            result_data.get("hCode"),
            result_data.get("homeTeamCode"),
            game_info.get("hCode"),
            game_info.get("homeTeamCode"),
        ),
        "awayCode": first_value(
            result_data.get("aCode"),
            result_data.get("awayTeamCode"),
            game_info.get("aCode"),
            game_info.get("awayTeamCode"),
        ),
    }

    result_data["_normalized"] = {
        "hasHomeTeamMetrics": bool(
            result_data[
                "homeTeamMetrics"
            ]
        ),
        "hasAwayTeamMetrics": bool(
            result_data[
                "awayTeamMetrics"
            ]
        ),
        "hasAbsences": bool(
            result_data[
                "analysisAbsences"
            ]
        ),
        "hasComments": bool(
            result_data[
                "analysisComments"
            ]
        ),
    }

    return result_data
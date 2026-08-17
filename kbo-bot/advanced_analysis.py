import io
import os
import asyncio

import discord
from discord.ui import Button, View

from player_data import get_player_data
from team_style import get_team_color

_CACHE = {}


# ============================================================
# 기본 유틸
# ============================================================

def _text(v, default="-"):
    return default if v is None or v == "" else str(v)


def _num(v):
    if v in (None, "", "-"):
        return None

    try:
        return float(
            str(v)
            .replace(",", "")
            .replace("%", "")
            .strip()
        )
    except (TypeError, ValueError):
        return None


def _first(*values):
    for v in values:
        if v not in (None, "", "-"):
            return v
    return None


def _side_name(side):
    standings = side.get("standings", {}) or {}
    return _text(standings.get("name"), "팀")


def _team_code(report, side_key):
    info = report.get("gameInfo", {}) or {}
    return str(
        info.get(
            "aCode" if side_key == "away" else "hCode"
        ) or ""
    )


# ============================================================
# 선수 ID
# ============================================================

def _player_id(player):
    if not isinstance(player, dict):
        return None

    return _first(
        player.get("playerId"),
        player.get("playerID"),
        player.get("playerNo"),
        player.get("id"),
        player.get("playerCode"),
        player.get("pCode"),
    )


def _get_data(player):
    pid = _player_id(player)

    if pid is None:
        return None

    pid = str(pid)

    if pid in _CACHE:
        return _CACHE[pid]

    try:
        data = get_player_data(pid)

        if isinstance(data, dict):
            _CACHE[pid] = data
            return data

    except Exception as e:
        print(f"[심화 분석] 선수 데이터 조회 실패 {pid}: {e}")

    return None


def clear_cache():
    _CACHE.clear()


# ============================================================
# 선수 데이터 접근
# ============================================================

def _basic(data):
    if not isinstance(data, dict):
        return {}

    return (
        data.get("basicRecord", {}) or {}
    ).get(
        "basic",
        {},
    ) or {}


def _games(data, limit=10):
    if not isinstance(data, dict):
        return []

    record = data.get("record", {}) or {}
    games = record.get("game", []) or []

    if not isinstance(games, list):
        return []

    def key(game):
        return str(
            _first(
                game.get("gday"),
                game.get("gdate"),
                game.get("date"),
                "",
            )
        )[:10]

    valid = [
        game
        for game in games
        if isinstance(game, dict)
    ]

    valid.sort(
        key=key,
        reverse=True,
    )

    if limit is None:
        return valid

    return valid[:limit]


# ============================================================
# OPS
# ============================================================

def _ops(v):
    n = _num(v)

    if n is None:
        return None

    if n > 2:
        n /= 100

    return n


def _calculate_ops_from_games(games):
    total_ab = 0.0
    total_h = 0.0
    total_bb = 0.0
    total_hbp = 0.0
    total_sf = 0.0
    total_tb = 0.0
    used_games = 0

    for game in games:
        ab = _num(
            _first(
                game.get("ab"),
                game.get("atBat"),
            )
        )

        h = _num(
            _first(
                game.get("hit"),
                game.get("hits"),
                game.get("h"),
            )
        )

        bb = _num(
            _first(
                game.get("bb"),
                game.get("walk"),
                game.get("walks"),
            )
        )

        if ab is None or h is None or bb is None:
            continue

        doubles = (
            _num(
                _first(
                    game.get("double"),
                    game.get("doubles"),
                    game.get("2b"),
                )
            )
            or 0.0
        )

        triples = (
            _num(
                _first(
                    game.get("triple"),
                    game.get("triples"),
                    game.get("3b"),
                )
            )
            or 0.0
        )

        home_runs = (
            _num(
                _first(
                    game.get("hr"),
                    game.get("homeRun"),
                )
            )
            or 0.0
        )

        hbp = (
            _num(
                _first(
                    game.get("hbp"),
                    game.get("hitByPitch"),
                    game.get("hp"),
                )
            )
            or 0.0
        )

        sf = (
            _num(
                _first(
                    game.get("sf"),
                    game.get("sacFly"),
                )
            )
            or 0.0
        )

        tb = _num(
            _first(
                game.get("tb"),
                game.get("totalBases"),
            )
        )

        if tb is None:
            singles = h - doubles - triples - home_runs
            tb = (
                singles
                + 2 * doubles
                + 3 * triples
                + 4 * home_runs
            )

        total_ab += ab
        total_h += h
        total_bb += bb
        total_hbp += hbp
        total_sf += sf
        total_tb += tb
        used_games += 1

    if used_games <= 0 or total_ab <= 0:
        return None

    obp_den = (
        total_ab
        + total_bb
        + total_hbp
        + total_sf
    )

    if obp_den <= 0:
        return None

    obp = (
        total_h
        + total_bb
        + total_hbp
    ) / obp_den

    slg = total_tb / total_ab

    return obp + slg


def _season_ops(player):
    data = _get_data(player)

    if not data:
        return None

    basic = _basic(data)

    direct_ops = _first(
        basic.get("ops"),
        basic.get("OPS"),
        data.get("ops"),
        data.get("OPS"),
    )

    if direct_ops is not None:
        value = _ops(direct_ops)

        if value is not None:
            return value

    obp = _ops(
        _first(
            basic.get("obp"),
            basic.get("onbase"),
            basic.get("onBase"),
        )
    )

    slg = _ops(
        _first(
            basic.get("slg"),
            basic.get("slugging"),
            basic.get("slug"),
        )
    )

    if obp is not None and slg is not None:
        return obp + slg

    games = _games(
        data,
        None,
    )

    return _calculate_ops_from_games(games)


def _recent_ops(
    player,
    limit=10,
):
    data = _get_data(player)

    if not data:
        return None, "데이터 없음"

    games = _games(data, limit)

    if not games:
        return None, "데이터 없음"

    cumulative = _calculate_ops_from_games(games)

    if cumulative is not None:
        return cumulative, f"최근 {limit}경기 누적"

    direct_values = []

    for game in games:
        value = _ops(
            _first(
                game.get("ops"),
                game.get("OPS"),
            )
        )

        if value is not None:
            direct_values.append(value)

    if direct_values:
        return (
            sum(direct_values) / len(direct_values),
            f"최근 {limit}경기 OPS 평균",
        )

    return None, "데이터 부족"


# ============================================================
# KBO 이닝 → 아웃 수 변환
# ============================================================

def _outs_from_innings(v):
    n = _num(v)

    if n is None:
        return None

    whole = int(n)
    frac = round(n - whole, 3)

    if abs(frac - 0.1) < 0.01:
        return whole * 3 + 1

    if abs(frac - 0.2) < 0.01:
        return whole * 3 + 2

    return int(round(n * 3))


# ============================================================
# WHIP
# ============================================================

def _season_whip(player):
    data = _get_data(player)

    if not data:
        return None

    basic = _basic(data)

    direct_whip = _num(
        _first(
            basic.get("whip"),
            basic.get("WHIP"),
            data.get("whip"),
            data.get("WHIP"),
        )
    )

    if direct_whip is not None:
        return direct_whip

    hits = _num(
        _first(
            basic.get("hit"),
            basic.get("hits"),
            basic.get("h"),
        )
    )

    walks = _num(
        _first(
            basic.get("bb"),
            basic.get("walk"),
            basic.get("walks"),
        )
    )

    innings = _first(
        basic.get("inn"),
        basic.get("inning"),
        basic.get("innings"),
        basic.get("ip"),
        basic.get("IP"),
    )

    outs = _outs_from_innings(innings)

    if (
        hits is not None
        and walks is not None
        and outs
        and outs > 0
    ):
        return (hits + walks) / (outs / 3)

    games = _games(
        data,
        None,
    )

    return _calculate_whip_from_games(games)


def _calculate_whip_from_games(games):
    total_hits = 0.0
    total_walks = 0.0
    total_outs = 0

    for game in games:
        outs = _outs_from_innings(
            _first(
                game.get("inn"),
                game.get("inning"),
                game.get("innings"),
                game.get("ip"),
                game.get("IP"),
            )
        )

        if not outs or outs <= 0:
            continue

        hits = _num(
            _first(
                game.get("hit"),
                game.get("hits"),
                game.get("h"),
            )
        )

        walks = _num(
            _first(
                game.get("bb"),
                game.get("walk"),
                game.get("walks"),
            )
        )

        if hits is None or walks is None:
            continue

        total_hits += hits
        total_walks += walks
        total_outs += outs

    if total_outs <= 0:
        return None

    return (
        total_hits + total_walks
    ) / (total_outs / 3)


def _recent_whip(
    player,
    limit=10,
):
    data = _get_data(player)

    if not data:
        return None, "데이터 없음"

    games = _games(data, limit)

    if not games:
        return None, "데이터 없음"

    cumulative = _calculate_whip_from_games(games)

    if cumulative is not None:
        return cumulative, f"최근 {limit}경기 누적"

    weighted_whip = 0.0
    weighted_outs = 0

    for game in games:
        outs = _outs_from_innings(
            _first(
                game.get("inn"),
                game.get("inning"),
                game.get("innings"),
                game.get("ip"),
                game.get("IP"),
            )
        )

        if not outs:
            continue

        whip = _num(
            _first(
                game.get("whip"),
                game.get("WHIP"),
            )
        )

        if whip is None:
            continue

        weighted_whip += whip * outs
        weighted_outs += outs

    if weighted_outs > 0:
        return (
            weighted_whip / weighted_outs,
            f"최근 {limit}경기 이닝가중 평균",
        )

    return None, "데이터 부족"


# ============================================================
# 타자 목록
# ============================================================

def _hitters(side):
    players = []

    lineup = side.get("lineup", {}) or {}

    raw = (
        lineup.get("fullLineUp")
        or lineup.get("lineup")
        or []
    )

    for player in raw:
        if not isinstance(player, dict):
            continue

        order = _num(
            player.get("batorder")
        )

        if order is None:
            continue

        order = int(order)

        if not 1 <= order <= 9:
            continue

        item = dict(player)
        item["_order"] = order
        item["_name"] = _text(
            player.get("playerName"),
            "선수",
        )

        players.append(item)

    players.sort(
        key=lambda x: (
            x["_order"],
            x["_name"],
        )
    )

    return players[:9]


# ============================================================
# 중계투수 목록
# ============================================================

def _pitchers(side):
    """
    심화 투수 분석용 중계투수 목록.
    선발투수는 제외한다.
    """

    result = []
    seen = set()

    bullpen = (
        side.get(
            "bullpenAnalysis",
            [],
        )
        or []
    )

    for player in bullpen[:12]:
        if not isinstance(player, dict):
            continue

        if player.get("isStarter"):
            continue

        pid = str(
            _player_id(player)
            or player.get("playerName")
            or player.get("name")
            or ""
        ).strip()

        if not pid:
            continue

        if pid in seen:
            continue

        seen.add(pid)

        item = dict(player)
        item["_isBullpen"] = True
        item["playerType"] = "PITCHER"

        result.append(item)

    return result


# ============================================================
# matplotlib 폰트
# ============================================================

def _font_property():
    try:
        from matplotlib import font_manager

        candidates = [
            r"C:\Windows\Fonts\malgun.ttf",
            r"C:\Windows\Fonts\malgunbd.ttf",
            r"C:\Windows\Fonts\NanumGothic.ttf",
            r"C:\Windows\Fonts\NanumGothicBold.ttf",
            "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        ]

        for path in candidates:
            if not os.path.exists(path):
                continue

            try:
                font_manager.fontManager.addfont(path)
                return font_manager.FontProperties(
                    fname=path
                )
            except Exception:
                continue

        return font_manager.FontProperties(
            family="DejaVu Sans"
        )

    except Exception:
        return None


# ============================================================
# 그래프/Embed 생성
# ============================================================

def _analysis_embed(
    report,
    kind,
    recent=False,
):
    import matplotlib.pyplot as plt
    import numpy as np

    prop = _font_property()

    period = (
        "최근 10경기"
        if recent
        else "시즌 전체"
    )

    bar_labels = []
    bar_values = []
    bar_colors = []
    summaries = []

    for side_key in ("away", "home"):
        side = report.get(side_key, {}) or {}

        team = _side_name(side)
        team_code = _team_code(
            report,
            side_key,
        )
        color = get_team_color(team_code)

        values = []

        if kind == "hitter":
            players = _hitters(side)

            for player in players:
                if recent:
                    value, _method = _recent_ops(
                        player,
                        10,
                    )
                else:
                    value = _season_ops(player)

                if value is None:
                    values.append(np.nan)
                    continue

                values.append(value)

                bar_labels.append(
                    f"{player['_order']}번 "
                    f"{player['_name']}({team_code})"
                )
                bar_values.append(value)
                bar_colors.append(color)

            valid = [
                value
                for value in values
                if not np.isnan(value)
            ]

            upper = [
                value
                for value in values[:3]
                if not np.isnan(value)
            ]

            middle = [
                value
                for value in values[3:5]
                if not np.isnan(value)
            ]

            lower = [
                value
                for value in values[5:9]
                if not np.isnan(value)
            ]

            ranked = sorted(
                [
                    (
                        player["_order"],
                        value,
                    )
                    for player, value in zip(
                        players,
                        values,
                    )
                    if not np.isnan(value)
                ],
                key=lambda item: item[1],
                reverse=True,
            )

            summaries.append({
                "team": team,
                "values": valid,
                "upper": upper,
                "middle": middle,
                "lower": lower,
                "best": ranked[:3],
            })

        else:
            players = _pitchers(side)

            for player in players:
                if recent:
                    value, _method = _recent_whip(
                        player,
                        10,
                    )
                else:
                    value = _season_whip(player)

                if value is None:
                    values.append(np.nan)
                    continue

                values.append(value)

                name = str(
                    player.get("playerName")
                    or player.get("name")
                    or "투수"
                )

                bar_labels.append(
                    f"{name}({team_code})"
                )
                bar_values.append(value)
                bar_colors.append(color)

            valid = [
                value
                for value in values
                if not np.isnan(value)
            ]

            ranked = sorted(
                [
                    (
                        str(
                            player.get("playerName")
                            or player.get("name")
                            or "투수"
                        ),
                        value,
                    )
                    for player, value in zip(
                        players,
                        values,
                    )
                    if not np.isnan(value)
                ],
                key=lambda item: item[1],
            )

            summaries.append({
                "team": team,
                "values": valid,
                "bullpen": valid,
                "best": ranked[:3],
            })

    # ========================================================
    # 데이터 부족
    # ========================================================

    if not bar_values:
        title = (
            "타자 분석"
            if kind == "hitter"
            else "중계투수 분석"
        )

        embed = discord.Embed(
            title=f"{title} · {period}",
            description=(
                "현재 확보된 선수 상세 데이터로 "
                "그래프를 생성할 수 없습니다."
            ),
            color=discord.Color.dark_gray(),
        )

        embed.add_field(
            name="안내",
            value=(
                "실제 선수 데이터가 확보된 경우에만 "
                "OPS/WHIP을 계산합니다. "
                "데이터가 없는 값을 0으로 처리하지 않습니다."
            ),
            inline=False,
        )

        return embed, None

    # ========================================================
    # 그래프
    # ========================================================

    fig_width = max(
        11,
        len(bar_labels) * 0.75,
    )

    fig, ax = plt.subplots(
        figsize=(fig_width, 6.2),
        dpi=160,
    )

    xs = list(range(len(bar_labels)))

    ax.bar(
        xs,
        bar_values,
        color=bar_colors,
    )

    for x, value in zip(xs, bar_values):
        label = (
            f"{value:.3f}"
            if kind == "hitter"
            else f"{value:.2f}"
        )

        ax.annotate(
            label,
            (x, value),
            xytext=(0, 6),
            textcoords="offset points",
            ha="center",
            fontsize=8,
            fontproperties=prop,
        )

    ax.set_xticks(xs)

    ax.set_xticklabels(
        bar_labels,
        rotation=45,
        ha="right",
        fontproperties=prop,
    )

    if kind == "hitter":
        ax.set_ylabel(
            f"OPS ({period})",
            fontproperties=prop,
        )

        ax.set_title(
            f"타자 OPS 비교 · {period}",
            fontproperties=prop,
            fontsize=15,
            fontweight="bold",
        )

    else:
        ax.set_ylabel(
            f"WHIP ({period}) · 낮을수록 좋음",
            fontproperties=prop,
        )

        ax.set_title(
            f"중계투수 WHIP 비교 · {period}",
            fontproperties=prop,
            fontsize=15,
            fontweight="bold",
        )

    ax.grid(
        axis="y",
        alpha=0.2,
    )

    if prop is not None:
        for label in (
            ax.get_yticklabels()
            + ax.get_xticklabels()
        ):
            label.set_fontproperties(prop)

    fig.tight_layout()

    buf = io.BytesIO()

    fig.savefig(
        buf,
        format="png",
        bbox_inches="tight",
        dpi=160,
    )

    plt.close(fig)
    buf.seek(0)

    # ========================================================
    # 타자 설명
    # ========================================================

    if kind == "hitter":
        lines = []

        for summary in summaries:
            values = summary["values"]

            average = (
                sum(values) / len(values)
                if values
                else None
            )

            if average is not None:
                lines.append(
                    f"**{summary['team']}** "
                    f"평균 OPS {average:.3f}"
                )
            else:
                lines.append(
                    f"**{summary['team']}** OPS 데이터 부족"
                )

            if summary["upper"]:
                lines.append(
                    f"{summary['team']} 1~3번 평균 "
                    f"{sum(summary['upper']) / len(summary['upper']):.3f}"
                )

            if summary["middle"]:
                lines.append(
                    f"{summary['team']} 4~5번 평균 "
                    f"{sum(summary['middle']) / len(summary['middle']):.3f}"
                )

            if summary["lower"]:
                lines.append(
                    f"{summary['team']} 6~9번 평균 "
                    f"{sum(summary['lower']) / len(summary['lower']):.3f}"
                )

            if summary["best"]:
                lines.append(
                    f"{summary['team']} 상위 OPS: "
                    + ", ".join(
                        f"{order}번 {value:.3f}"
                        for order, value in summary["best"]
                    )
                )

        if (
            len(summaries) == 2
            and all(
                summary["values"]
                for summary in summaries
            )
        ):
            averages = [
                sum(summary["values"]) / len(summary["values"])
                for summary in summaries
            ]

            if averages[0] == averages[1]:
                better = "동률"
            elif averages[0] > averages[1]:
                better = summaries[0]["team"]
            else:
                better = summaries[1]["team"]

            lines.append(
                f"→ 평균 OPS 우위: **{better}**"
            )

        embed = discord.Embed(
            title=f"타자 분석 · {period}",
            description="\n".join(lines),
            color=discord.Color.dark_gray(),
        )

        embed.add_field(
            name="해석",
            value=(
                "OPS가 높을수록 공격 생산력이 좋습니다. "
                "상위 1~3번, 중심 4~5번, 하위 6~9번의 "
                "타선 강점 위치도 함께 비교합니다."
            ),
            inline=False,
        )

        if recent:
            embed.add_field(
                name="최근 10경기 계산",
                value=(
                    "최근 10경기의 타수·안타·볼넷 등 "
                    "확보된 누적 기록을 이용해 계산합니다."
                ),
                inline=False,
            )

    # ========================================================
    # 중계투수 설명
    # ========================================================

    else:
        lines = []

        for summary in summaries:
            values = summary["values"]

            average = (
                sum(values) / len(values)
                if values
                else None
            )

            if average is not None:
                lines.append(
                    f"**{summary['team']}** "
                    f"중계투수 평균 WHIP {average:.2f}"
                )
            else:
                lines.append(
                    f"**{summary['team']}** "
                    "중계투수 WHIP 데이터 부족"
                )

            if summary["bullpen"]:
                lines.append(
                    f"{summary['team']} "
                    f"불펜 평균 WHIP "
                    f"{sum(summary['bullpen']) / len(summary['bullpen']):.2f}"
                )

            if summary["best"]:
                lines.append(
                    f"{summary['team']} 안정적인 중계투수: "
                    + ", ".join(
                        f"{name} {value:.2f}"
                        for name, value in summary["best"]
                    )
                )

        if (
            len(summaries) == 2
            and all(
                summary["values"]
                for summary in summaries
            )
        ):
            averages = [
                sum(summary["values"]) / len(summary["values"])
                for summary in summaries
            ]

            if averages[0] == averages[1]:
                better = "동률"
            elif averages[0] < averages[1]:
                better = summaries[0]["team"]
            else:
                better = summaries[1]["team"]

            lines.append(
                f"→ 중계투수 평균 WHIP 우위: **{better}**"
            )

        embed = discord.Embed(
            title=f"중계투수 분석 · {period}",
            description="\n".join(lines),
            color=discord.Color.dark_gray(),
        )

        embed.add_field(
            name="해석",
            value=(
                "WHIP은 낮을수록 좋습니다. "
                "선발투수는 제외하고 중계투수/불펜만 "
                "분석합니다."
            ),
            inline=False,
        )

        if recent:
            embed.add_field(
                name="최근 10경기 계산",
                value=(
                    "최근 등판 경기의 피안타·볼넷·이닝을 "
                    "누적하여 WHIP을 계산합니다."
                ),
                inline=False,
            )

    return embed, buf


# ============================================================
# Discord용 타자 분석 View
# ============================================================

class HitterAnalysisView(View):
    def __init__(self, report):
        super().__init__(timeout=None)
        self.report = report

    async def _show(
        self,
        interaction,
        recent,
    ):
        try:
            await interaction.response.defer()

            embed, buf = await asyncio.to_thread(
                _analysis_embed,
                self.report,
                "hitter",
                recent,
            )

            if buf is None:
                await interaction.edit_original_response(
                    content=(
                        "현재 확보된 타자 데이터가 부족하여 "
                        "그래프를 생성할 수 없습니다."
                    ),
                    embed=embed,
                    view=self,
                    attachments=[],
                )
                return

            file = discord.File(
                buf,
                filename="kbo_hitter_analysis.png",
            )

            embed.set_image(
                url="attachment://kbo_hitter_analysis.png"
            )

            await interaction.edit_original_response(
                content=None,
                embed=embed,
                view=self,
                attachments=[file],
            )

        except discord.NotFound:
            print("[타자 분석] Interaction 만료")

        except discord.HTTPException as e:
            print(
                f"[타자 분석] Discord 오류: "
                f"{type(e).__name__}: {e}"
            )

        except Exception as e:
            print(
                f"[타자 분석] 오류: "
                f"{type(e).__name__}: {e}"
            )

            try:
                await interaction.edit_original_response(
                    content=(
                        f"타자 분석 생성 실패: "
                        f"{type(e).__name__}: {e}"
                    ),
                    embed=None,
                    view=self,
                    attachments=[],
                )
            except Exception:
                pass

    @discord.ui.button(
        label="시즌 전체",
        style=discord.ButtonStyle.secondary,
        custom_id="kbo_hitter_season",
        row=0,
    )
    async def season(
        self,
        interaction: discord.Interaction,
        button: Button,
    ):
        await self._show(
            interaction,
            False,
        )

    @discord.ui.button(
        label="최근 10경기",
        style=discord.ButtonStyle.secondary,
        custom_id="kbo_hitter_recent10",
        row=0,
    )
    async def recent(
        self,
        interaction: discord.Interaction,
        button: Button,
    ):
        await self._show(
            interaction,
            True,
        )


# ============================================================
# Discord용 투수 분석 View
# ============================================================

class PitcherAnalysisView(View):
    def __init__(self, report):
        super().__init__(timeout=None)
        self.report = report

    async def _show(
        self,
        interaction,
        recent,
    ):
        try:
            await interaction.response.defer()

            embed, buf = await asyncio.to_thread(
                _analysis_embed,
                self.report,
                "pitcher",
                recent,
            )

            if buf is None:
                await interaction.edit_original_response(
                    content=(
                        "현재 확보된 중계투수 데이터가 부족하여 "
                        "그래프를 생성할 수 없습니다."
                    ),
                    embed=embed,
                    view=self,
                    attachments=[],
                )
                return

            file = discord.File(
                buf,
                filename="kbo_pitcher_analysis.png",
            )

            embed.set_image(
                url="attachment://kbo_pitcher_analysis.png"
            )

            await interaction.edit_original_response(
                content=None,
                embed=embed,
                view=self,
                attachments=[file],
            )

        except discord.NotFound:
            print("[투수 분석] Interaction 만료")

        except discord.HTTPException as e:
            print(
                f"[투수 분석] Discord 오류: "
                f"{type(e).__name__}: {e}"
            )

        except Exception as e:
            print(
                f"[투수 분석] 오류: "
                f"{type(e).__name__}: {e}"
            )

            try:
                await interaction.edit_original_response(
                    content=(
                        f"투수 분석 생성 실패: "
                        f"{type(e).__name__}: {e}"
                    ),
                    embed=None,
                    view=self,
                    attachments=[],
                )
            except Exception:
                pass

    @discord.ui.button(
        label="시즌 전체",
        style=discord.ButtonStyle.secondary,
        custom_id="kbo_pitcher_season",
        row=0,
    )
    async def season(
        self,
        interaction: discord.Interaction,
        button: Button,
    ):
        await self._show(
            interaction,
            False,
        )

    @discord.ui.button(
        label="최근 10경기",
        style=discord.ButtonStyle.secondary,
        custom_id="kbo_pitcher_recent10",
        row=0,
    )
    async def recent(
        self,
        interaction: discord.Interaction,
        button: Button,
    ):
        await self._show(
            interaction,
            True,
        )


# ============================================================
# 호환용 함수
# ============================================================

def build_hitter_analysis(report):
    return _analysis_embed(
        report,
        "hitter",
        recent=False,
    )


def build_recent_hitter_analysis(report):
    return _analysis_embed(
        report,
        "hitter",
        recent=True,
    )


def build_pitcher_analysis(report):
    return _analysis_embed(
        report,
        "pitcher",
        recent=False,
    )


def build_recent_pitcher_analysis(report):
    return _analysis_embed(
        report,
        "pitcher",
        recent=True,
    )


def build_hitter_embed(report):
    return build_hitter_analysis(report)


def build_recent_hitter_embed(report):
    return build_recent_hitter_analysis(report)


def build_pitcher_embed(report):
    return build_pitcher_analysis(report)


def build_recent_pitcher_embed(report):
    return build_recent_pitcher_analysis(report)


# ============================================================
# 핵심 매치업
# ============================================================

def build_core_matchup_embed(report):
    away = report.get("away", {}) or {}
    home = report.get("home", {}) or {}
    info = report.get("gameInfo", {}) or {}

    def lineup_ops(side):
        values = [
            _season_ops(player)
            for player in _hitters(side)
        ]

        values = [
            value
            for value in values
            if value is not None
        ]

        if not values:
            return None

        return sum(values) / len(values)

    def starter_values(side):
        starter = side.get(
            "starter",
            {},
        ) or {}

        basic = (
            starter.get(
                "currentSeasonStats",
                {},
            )
            or {}
        )

        era = _num(
            basic.get("era")
        )

        whip = _num(
            basic.get("whip")
        )

        return era, whip

    away_ops = lineup_ops(away)
    home_ops = lineup_ops(home)

    away_era, away_whip = starter_values(away)
    home_era, home_whip = starter_values(home)

    embed = discord.Embed(
        title="핵심 매치업",
        description=(
            f"{_text(info.get('aName'))} "
            f"@ "
            f"{_text(info.get('hName'))}"
        ),
        color=discord.Color.dark_gray(),
    )

    for (
        team,
        side,
        ops,
        opponent_era,
        opponent_whip,
    ) in (
        (
            _side_name(away),
            away,
            away_ops,
            home_era,
            home_whip,
        ),
        (
            _side_name(home),
            home,
            home_ops,
            away_era,
            away_whip,
        ),
    ):
        top = _hitters(side)[:5]

        names = ", ".join(
            f"{player['_order']}번 {player['_name']}"
            for player in top
        )

        lines = [
            f"상위 5타자: {names or '라인업 데이터 부족'}"
        ]

        if ops is not None:
            lines.append(
                f"라인업 평균 OPS: {ops:.3f}"
            )
        else:
            lines.append(
                "라인업 평균 OPS: 데이터 부족"
            )

        if opponent_whip is not None:
            lines.append(
                f"상대 선발 WHIP: {opponent_whip:.2f}"
            )

        if opponent_era is not None:
            lines.append(
                f"상대 선발 ERA: {opponent_era:.2f}"
            )

        embed.add_field(
            name=team,
            value="\n".join(lines),
            inline=False,
        )

    if (
        away_ops is not None
        and home_ops is not None
    ):
        if away_ops == home_ops:
            attack_team = "동률"
        elif away_ops > home_ops:
            attack_team = _side_name(away)
        else:
            attack_team = _side_name(home)

        embed.add_field(
            name="공격",
            value=(
                "시즌 라인업 OPS 평균은 "
                f"**{attack_team}**가 우위입니다."
            ),
            inline=True,
        )

    if (
        away_whip is not None
        and home_whip is not None
    ):
        if away_whip == home_whip:
            pitching_team = "동률"
        elif away_whip < home_whip:
            pitching_team = _side_name(away)
        else:
            pitching_team = _side_name(home)

        embed.add_field(
            name="선발",
            value=(
                "선발 WHIP은 "
                f"**{pitching_team}**가 더 낮습니다."
            ),
            inline=True,
        )

    embed.set_footer(
        text=(
            "직접 상대전 데이터가 없는 부분은 "
            "과도하게 해석하지 않습니다."
        )
    )

    return embed
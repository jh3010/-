import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import io

plt.rcParams["font.family"] = "Malgun Gothic"
plt.rcParams["axes.unicode_minus"] = False


def make_starter_comparison_chart(report: dict) -> io.BytesIO:
    away = report["away"]
    home = report["home"]

    away_name = away["starter"]["playerInfo"].get("name", "원정선발")
    home_name = home["starter"]["playerInfo"].get("name", "홈선발")

    away_stats = away["starter"].get("currentSeasonStats", {})
    home_stats = home["starter"].get("currentSeasonStats", {})

    metrics = ["ERA", "WHIP", "탈삼진"]
    away_values = [
        float(away_stats.get("era", 0) or 0),
        float(away_stats.get("whip", 0) or 0),
        float(away_stats.get("kk", 0) or 0),
    ]
    home_values = [
        float(home_stats.get("era", 0) or 0),
        float(home_stats.get("whip", 0) or 0),
        float(home_stats.get("kk", 0) or 0),
    ]

    fig, axes = plt.subplots(1, 3, figsize=(9, 3.5))
    fig.suptitle(f"선발투수 비교: {away_name} vs {home_name}", fontsize=13, fontweight="bold")

    colors = ["#4A90D9", "#D94A4A"]

    for i, metric in enumerate(metrics):
        ax = axes[i]
        bars = ax.bar([away_name, home_name], [away_values[i], home_values[i]], color=colors)
        ax.set_title(metric, fontsize=11)
        for bar in bars:
            height = bar.get_height()
            ax.text(bar.get_x() + bar.get_width() / 2, height, f"{height:g}",
                    ha="center", va="bottom", fontsize=9)
        ax.set_ylim(0, max(away_values[i], home_values[i]) * 1.3 + 0.1)

    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


def make_recent_games_chart(report: dict) -> io.BytesIO:
    fig, ax = plt.subplots(figsize=(7, 4))

    for side_key, color in [("away", "#4A90D9"), ("home", "#D94A4A")]:
        side = report[side_key]
        team_name = side["standings"].get("name", side_key)
        games = list(reversed(side["previousGames"]))

        dates = []
        runs = []
        for g in games:
            if g["hName"] == team_name:
                runs.append(g["hScore"])
            else:
                runs.append(g["aScore"])
            dates.append(str(g["gdate"])[-4:])

        ax.plot(dates, runs, marker="o", label=team_name, color=color, linewidth=2)

    ax.set_title("최근 5경기 득점 흐름", fontsize=13, fontweight="bold")
    ax.set_ylabel("득점")
    ax.legend()
    ax.grid(alpha=0.3)

    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf
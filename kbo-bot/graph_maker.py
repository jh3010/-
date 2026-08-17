import os
import io  # <-- 이 부분이 반드시 있어야 합니다!
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

# 1. 폰트 강제 지정 (매번 그래프를 그릴 때 확실하게 밀어넣기)
font_path = os.path.join(os.path.dirname(__file__), 'NanumGothic.ttf')
if os.path.exists(font_path):
    fm.fontManager.addfont(font_path)
    font_prop = fm.FontProperties(fname=font_path)
    plt.rcParams['font.family'] = font_prop.get_name()
else:
    # 혹시 상위 폴더에 있는 경우 대비
    parent_font_path = os.path.join(os.path.dirname(__file__), '..', 'NanumGothic.ttf')
    if os.path.exists(parent_font_path):
        fm.fontManager.add_font(parent_font_path)
        font_prop = fm.FontProperties(fname=parent_font_path)
        plt.rcParams['font.family'] = font_prop.get_name()
    else:
        font_prop = None

plt.rcParams['axes.unicode_minus'] = False

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
    
    title_kwargs = {"fontproperties": font_prop} if font_prop else {}
    fig.suptitle(f"선발투수 비교: {away_name} vs {home_name}", fontsize=13, **title_kwargs)

    colors = ["#4A90D9", "#D94A4A"]

    for i, metric in enumerate(metrics):
        ax = axes[i]
        bars = ax.bar([away_name, home_name], [away_values[i], home_values[i]], color=colors)
        ax.set_title(metric, fontsize=11, **title_kwargs)
        
        if font_prop:
            ax.set_xticks([0, 1])
            ax.set_xticklabels([away_name, home_name], fontproperties=font_prop)
            
        for bar in bars:
            height = bar.get_height()
            ax.text(bar.get_x() + bar.get_width() / 2, height, f"{height:g}",
                    ha="center", va="bottom", fontsize=9, **title_kwargs)
        ax.set_ylim(0, max(away_values[i], home_values[i]) * 1.3 + 0.1)

    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf
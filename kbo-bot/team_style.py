"""
팀별 컬러/로고 관리 모듈
"""

import requests
from PIL import Image
import io

# 네이버 스포츠가 쓰는 팀 로고 URL 패턴 (팀코드만 바꾸면 됨)
LOGO_URL = "https://sports-phinf.pstatic.net/team/kbo/default/{team_code}.png?type=f92_88"

# KBO 10개 구단 상징색 (그래프/embed 색상에 사용)
TEAM_COLORS = {
    "NC": "#1D467F",
    "OB": "#131230",  # 두산
    "LG": "#C30452",
    "HH": "#FF6600",  # 한화
    "SS": "#074CA1",  # 삼성
    "LT": "#041E42",  # 롯데
    "HT": "#EA0029",  # KIA
    "WO": "#570514",  # 키움
    "SK": "#CE0E2D",  # SSG
    "KT": "#000000",
}

DEFAULT_COLOR = "#4A90D9"


def get_team_color(team_code: str) -> str:
    return TEAM_COLORS.get(team_code, DEFAULT_COLOR)


def get_logo_url(team_code: str) -> str:
    return LOGO_URL.format(team_code=team_code)


def make_matchup_banner(away_code: str, home_code: str) -> io.BytesIO | None:
    """
    양 팀 로고를 나란히 붙인 대진 배너 이미지를 만든다.
    로고 다운로드에 실패하면 None을 반환한다 (호출부에서 조용히 건너뛰도록).
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        away_resp = requests.get(get_logo_url(away_code), headers=headers, timeout=5)
        home_resp = requests.get(get_logo_url(home_code), headers=headers, timeout=5)
        away_resp.raise_for_status()
        home_resp.raise_for_status()

        away_logo = Image.open(io.BytesIO(away_resp.content)).convert("RGBA")
        home_logo = Image.open(io.BytesIO(home_resp.content)).convert("RGBA")

        size = 140
        away_logo = away_logo.resize((size, size))
        home_logo = home_logo.resize((size, size))

        banner_w, banner_h = 500, 160
        banner = Image.new("RGBA", (banner_w, banner_h), (255, 255, 255, 0))

        banner.paste(away_logo, (30, (banner_h - size) // 2), away_logo)
        banner.paste(home_logo, (banner_w - size - 30, (banner_h - size) // 2), home_logo)

        buf = io.BytesIO()
        banner.save(buf, format="PNG")
        buf.seek(0)
        return buf
    except Exception as e:
        print(f"[배너 생성 실패] {e}")
        return None
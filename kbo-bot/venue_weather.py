import asyncio
import datetime
from typing import Any, Dict, Optional
import requests

STADIUMS = {
    "잠실": {"name": "잠실야구장", "city": "서울", "home_team": "LG 트윈스 / 두산 베어스", "lat": 37.5121, "lon": 127.0719},
    "잠실야구장": {"name": "잠실야구장", "city": "서울", "home_team": "LG 트윈스 / 두산 베어스", "lat": 37.5121, "lon": 127.0719},
    "고척": {"name": "고척스카이돔", "city": "서울", "home_team": "키움 히어로즈", "lat": 37.4982, "lon": 126.8671},
    "고척스카이돔": {"name": "고척스카이돔", "city": "서울", "home_team": "키움 히어로즈", "lat": 37.4982, "lon": 126.8671},
    "문학": {"name": "인천 SSG 랜더스필드", "city": "인천", "home_team": "SSG 랜더스", "lat": 37.4368, "lon": 126.6933},
    "인천": {"name": "인천 SSG 랜더스필드", "city": "인천", "home_team": "SSG 랜더스", "lat": 37.4368, "lon": 126.6933},
    "수원": {"name": "수원KT위즈파크", "city": "수원", "home_team": "KT 위즈", "lat": 37.2999, "lon": 127.0097},
    "대전": {"name": "대전 한화생명 볼파크", "city": "대전", "home_team": "한화 이글스", "lat": 36.3170, "lon": 127.4287},
    "대구": {"name": "대구삼성라이온즈파크", "city": "대구", "home_team": "삼성 라이온즈", "lat": 35.8410, "lon": 128.6810},
    "광주": {"name": "광주-기아 챔피언스 필드", "city": "광주", "home_team": "KIA 타이거즈", "lat": 35.1681, "lon": 126.8890},
    "사직": {"name": "사직야구장", "city": "부산", "home_team": "롯데 자이언츠", "lat": 35.1942, "lon": 129.0617},
    "창원": {"name": "창원NC파크", "city": "창원", "home_team": "NC 다이노스", "lat": 35.2229, "lon": 128.5823},
}

WMO = {
    0: "맑음", 1: "대체로 맑음", 2: "부분적 흐림", 3: "흐림", 45: "안개", 48: "안개",
    51: "이슬비", 53: "이슬비", 55: "강한 이슬비", 61: "약한 비", 63: "비", 65: "강한 비",
    71: "약한 눈", 73: "눈", 75: "강한 눈", 80: "소나기", 81: "소나기", 82: "강한 소나기",
    95: "뇌우", 96: "우박 동반 뇌우", 99: "우박 동반 뇌우",
}


def get_stadium_info(stadium_name: str) -> Dict[str, Any]:
    raw = str(stadium_name or "").strip()
    for key, value in STADIUMS.items():
        if key in raw or raw in key:
            return dict(value)
    return {"name": raw or "구장 정보 없음", "city": "정보 없음", "home_team": "정보 없음"}


def _parse_hour(time_text: Optional[str]) -> int:
    try:
        return int(str(time_text).strip().split(":")[0]) if time_text else 18
    except Exception:
        return 18


def get_venue_weather_sync(stadium_name: str, game_date: Optional[str], game_time: Optional[str]) -> Dict[str, Any]:
    info = get_stadium_info(stadium_name)
    lat, lon = info.get("lat"), info.get("lon")
    if lat is None or lon is None:
        return {"available": False, "reason": "구장 좌표 정보 없음"}
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "hourly": "temperature_2m,apparent_temperature,precipitation_probability,precipitation,weather_code,wind_speed_10m",
                "timezone": "Asia/Seoul",
                "forecast_days": 3,
            },
            timeout=8,
        )
        r.raise_for_status()
        hourly = r.json().get("hourly", {})
        times = hourly.get("time", [])
        if not times:
            return {"available": False, "reason": "날씨 데이터 없음"}
        target_date = str(game_date or datetime.date.today())[:10]
        target_hour = _parse_hour(game_time)
        candidates = [i for i, t in enumerate(times) if str(t).startswith(target_date)]
        if not candidates:
            return {"available": False, "reason": "해당 날짜 예보 없음"}
        idx = min(candidates, key=lambda i: abs(int(str(times[i])[11:13]) - target_hour))
        code = (hourly.get("weather_code") or [None])[idx]
        return {
            "available": True,
            "time": times[idx],
            "condition": WMO.get(int(code), "기상 정보 없음") if code is not None else "기상 정보 없음",
            "temperature": (hourly.get("temperature_2m") or [None])[idx],
            "apparent": (hourly.get("apparent_temperature") or [None])[idx],
            "precip_prob": (hourly.get("precipitation_probability") or [None])[idx],
            "precip": (hourly.get("precipitation") or [None])[idx],
            "wind": (hourly.get("wind_speed_10m") or [None])[idx],
        }
    except Exception as e:
        return {"available": False, "reason": str(e)}


async def get_venue_weather(stadium_name: str, game_date: Optional[str], game_time: Optional[str]) -> Dict[str, Any]:
    return await asyncio.to_thread(get_venue_weather_sync, stadium_name, game_date, game_time)


def format_weather_line(weather: Dict[str, Any]) -> str:
    if not weather.get("available"):
        return f"날씨: 확인 불가 ({weather.get('reason', '데이터 없음')})"
    parts = [f"날씨: {weather.get('condition', '정보 없음')}"]
    if weather.get("temperature") is not None:
        parts.append(f"기온 {weather['temperature']}°C")
    if weather.get("apparent") is not None:
        parts.append(f"체감 {weather['apparent']}°C")
    if weather.get("precip_prob") is not None:
        parts.append(f"강수확률 {weather['precip_prob']}%")
    if weather.get("wind") is not None:
        parts.append(f"풍속 {weather['wind']}km/h")
    return " · ".join(parts)
"""OpenWeatherMap current conditions — 10-min in-memory cache, no DB."""
from __future__ import annotations

import logging
import time

import httpx

from .config import OPENWEATHER_API_KEY, OPENWEATHER_CITY

log = logging.getLogger("dashboard.weather")

_cache: dict | None = None
_cache_ts = 0.0
_CACHE_TTL = 600.0  # 10 minutes

_forecast_cache: list | None = None
_forecast_ts = 0.0
_FORECAST_TTL = 1800.0  # 30 minutes


def is_configured() -> bool:
    return bool(OPENWEATHER_API_KEY)


async def get_weather() -> dict:
    global _cache, _cache_ts

    if _cache is not None and time.monotonic() - _cache_ts < _CACHE_TTL:
        return _cache

    if not OPENWEATHER_API_KEY:
        return {}

    try:
        async with httpx.AsyncClient(timeout=6) as client:
            resp = await client.get(
                "https://api.openweathermap.org/data/2.5/weather",
                params={
                    "q": OPENWEATHER_CITY,
                    "appid": OPENWEATHER_API_KEY,
                    "units": "metric",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        _cache = {
            "temp": round(data["main"]["temp"]),
            "feels_like": round(data["main"]["feels_like"]),
            "temp_min": round(data["main"]["temp_min"]),
            "temp_max": round(data["main"]["temp_max"]),
            "description": data["weather"][0]["description"].capitalize(),
            "icon": data["weather"][0]["icon"],
            "humidity": data["main"]["humidity"],
            "wind_kph": round(data["wind"]["speed"] * 3.6),
            "city": data["name"],
        }
        _cache_ts = time.monotonic()
        log.debug("Weather fetched: %s°C %s", _cache["temp"], _cache["description"])
    except Exception as e:
        log.warning("Weather fetch failed: %s", e)
        if _cache is None:
            return {}

    return _cache or {}


async def get_forecast() -> list[dict]:
    """Return a daily forecast for the next 5 days (OWM 5-day/3-hour API)."""
    global _forecast_cache, _forecast_ts

    if _forecast_cache is not None and time.monotonic() - _forecast_ts < _FORECAST_TTL:
        return _forecast_cache

    if not OPENWEATHER_API_KEY:
        return []

    try:
        async with httpx.AsyncClient(timeout=6) as client:
            resp = await client.get(
                "https://api.openweathermap.org/data/2.5/forecast",
                params={
                    "q": OPENWEATHER_CITY,
                    "appid": OPENWEATHER_API_KEY,
                    "units": "metric",
                    "cnt": 40,
                },
            )
            resp.raise_for_status()
            raw = resp.json()

        # Group 3-hour intervals by calendar date
        from collections import defaultdict
        days: dict[str, list] = defaultdict(list)
        for item in raw.get("list", []):
            days[item["dt_txt"][:10]].append(item)

        result = []
        for date, items in sorted(days.items()):
            temps = [i["main"]["temp"] for i in items]
            winds = [i["wind"]["speed"] for i in items]
            # Use midday (12:00) slot for representative icon/description, else middle item
            mid = next(
                (i for i in items if "12:00:00" in i["dt_txt"]),
                items[len(items) // 2],
            )
            pop = round(max(i.get("pop", 0) for i in items) * 100)
            result.append({
                "date": date,
                "temp_max": round(max(temps)),
                "temp_min": round(min(temps)),
                "description": mid["weather"][0]["description"].capitalize(),
                "icon": mid["weather"][0]["icon"],
                "wind_kph": round(max(winds) * 3.6),
                "humidity": round(sum(i["main"]["humidity"] for i in items) / len(items)),
                "rain_chance": pop,
            })

        _forecast_cache = result
        _forecast_ts = time.monotonic()
        log.debug("Forecast fetched: %d days", len(result))
    except Exception as e:
        log.warning("Forecast fetch failed: %s", e)
        if _forecast_cache is None:
            return []

    return _forecast_cache or []

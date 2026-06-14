#!/usr/bin/env python3
"""
NOUS Internet Proxy - kører på Pi 5
Jetson kalder denne service for tid, vejr, søgning og web-fetch.
Pi 5 går på internettet, Jetson forbliver air-gapped.
"""
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
import httpx
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional

app = FastAPI(title="NOUS Internet Proxy", version="1.0")

# Config
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:8080")
DEFAULT_TIMEZONE = os.environ.get("TZ", "Europe/Copenhagen")
_allowed_raw = os.environ.get("NOUS_ALLOWED_CLIENTS", "")
ALLOWED_CLIENTS = set(filter(None, _allowed_raw.split(","))) if _allowed_raw else set()
HTTP_TIMEOUT = 10.0
MAX_FETCH_SIZE = 500_000  # 500KB max fra web-fetch


@app.get("/health")
def health():
    return {"status": "ok", "service": "nous-proxy"}


@app.get("/time")
def get_time(tz: str = DEFAULT_TIMEZONE):
    """Returnerer aktuel tid i specificeret timezone."""
    try:
        now = datetime.now(ZoneInfo(tz))
        return {
            "iso": now.isoformat(),
            "human_da": now.strftime("%A den %d. %B %Y kl %H %M"),
            "timezone": tz,
            "weekday": now.strftime("%A"),
        }
    except Exception as e:
        raise HTTPException(400, f"Invalid timezone: {e}")


@app.get("/weather")
async def get_weather(
    location: str = Query(..., description="Bynavn, f.eks. 'Aarhus'"),
    lang: str = "da",
):
    """Henter vejr via Open-Meteo (gratis, ingen API key)."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        # Step 1: Geocode
        geo = await client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": location, "count": 1, "language": lang}
        )
        geo_data = geo.json()
        if not geo_data.get("results"):
            raise HTTPException(404, f"Location ikke fundet: {location}")

        loc = geo_data["results"][0]
        lat, lon = loc["latitude"], loc["longitude"]

        # Step 2: Hent vejr
        wx = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
                "timezone": "auto",
            }
        )
        wx_data = wx.json()
        cur = wx_data.get("current", {})

        return {
            "location": loc["name"],
            "country": loc.get("country", ""),
            "temperature_c": cur.get("temperature_2m"),
            "humidity_pct": cur.get("relative_humidity_2m"),
            "wind_kmh": cur.get("wind_speed_10m"),
            "weather_code": cur.get("weather_code"),
            "observed_at": cur.get("time"),
        }


@app.get("/search")
async def search(
    q: str = Query(..., description="Søgeord"),
    n: int = Query(5, ge=1, le=20),
):
    """Søger via lokal SearXNG (som selv anonymiserer mod Google/Bing)."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        r = await client.get(
            f"{SEARXNG_URL}/search",
            params={"q": q, "format": "json"},
            headers={"User-Agent": "nous-proxy/1.0"}
        )
        if r.status_code != 200:
            raise HTTPException(502, f"SearXNG fejlede: {r.status_code}")
        data = r.json()
        results = []
        for hit in data.get("results", [])[:n]:
            results.append({
                "title": hit.get("title"),
                "url": hit.get("url"),
                "content": hit.get("content", "")[:500],
            })
        return {"query": q, "count": len(results), "results": results}


@app.get("/fetch")
async def fetch(url: str = Query(..., description="URL at hente")):
    """Henter en specifik URL og returnerer rå tekst (max 500KB)."""
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "URL skal starte med http:// eller https://")

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        try:
            r = await client.get(url, headers={"User-Agent": "nous-proxy/1.0"})
        except Exception as e:
            raise HTTPException(502, f"Fetch fejlede: {e}")

        content = r.content[:MAX_FETCH_SIZE]
        is_truncated = len(r.content) > MAX_FETCH_SIZE

        return {
            "url": str(r.url),
            "status": r.status_code,
            "content_type": r.headers.get("content-type", ""),
            "content": content.decode("utf-8", errors="replace"),
            "truncated": is_truncated,
            "size_bytes": len(r.content),
        }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)

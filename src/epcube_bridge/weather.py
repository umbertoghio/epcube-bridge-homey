"""Optional Open-Meteo current conditions, cached across all dashboard clients."""

import asyncio
import math
import time
from datetime import UTC, datetime

import httpx


class Weather:
    def __init__(self, location: str, *, transport=None):
        self.location = location.strip()
        self.transport = transport
        self.place = None
        self.cache = None
        self.failed = False
        self.next_fetch = 0.0
        self.lock = asyncio.Lock()

    async def read(self):
        if not self.location:
            return {"configured": False}
        async with self.lock:
            if time.monotonic() >= self.next_fetch:
                # Failures are cached too: many open browsers cannot cause a request storm.
                self.next_fetch = time.monotonic() + 900
                try:
                    async with httpx.AsyncClient(
                        timeout=8, trust_env=False, transport=self.transport
                    ) as client:
                        if self.place is None:
                            response = await client.get(
                                "https://geocoding-api.open-meteo.com/v1/search",
                                params={
                                    "name": self.location,
                                    "count": 1,
                                    "language": "en",
                                    "format": "json",
                                },
                            )
                            response.raise_for_status()
                            place = response.json()["results"][0]
                            lat, lon = float(place["latitude"]), float(place["longitude"])
                            if not -90 <= lat <= 90 or not -180 <= lon <= 180:
                                raise ValueError("Invalid location")
                            self.place = (
                                lat,
                                lon,
                                str(place["name"]) + ", " + str(place.get("country", "")),
                            )
                        lat, lon, label = self.place
                        response = await client.get(
                            "https://api.open-meteo.com/v1/forecast",
                            params={
                                "latitude": lat,
                                "longitude": lon,
                                "current": "temperature_2m,weather_code,is_day",
                                "temperature_unit": "celsius",
                                "timezone": "UTC",
                                "timeformat": "unixtime",
                            },
                        )
                        response.raise_for_status()
                        current = response.json()["current"]
                        temperature = float(current["temperature_2m"])
                        if not math.isfinite(temperature):
                            raise ValueError("Invalid temperature")
                        self.cache = {
                            "location": label,
                            "temperature_c": temperature,
                            "weather_code": int(current["weather_code"]),
                            "is_day": current["is_day"] == 1,
                            "timestamp": datetime.fromtimestamp(current["time"], UTC).isoformat(),
                        }
                        self.failed = False
                except Exception:
                    # Do not expose upstream responses, URLs or configuration in exceptions/logs.
                    self.failed = True
        stale = self.failed or (
            self.cache is not None
            and (
                datetime.now(UTC) - datetime.fromisoformat(self.cache["timestamp"])
            ).total_seconds()
            > 1800
        )
        return {
            "configured": True,
            "available": self.cache is not None,
            "stale": stale,
            **(self.cache or {}),
        }

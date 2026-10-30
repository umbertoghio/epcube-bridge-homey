import asyncio
from datetime import UTC, datetime

import httpx

from epcube_bridge.weather import Weather


async def test_weather_cache_geocoding_and_failure_retry(monkeypatch):
    calls = []
    fail = False
    clock = [100.0]
    monkeypatch.setattr("epcube_bridge.weather.time.monotonic", lambda: clock[0])

    def handler(request):
        calls.append(request)
        if request.url.host.startswith("geocoding"):
            assert request.url.params["name"] == "Borgo San Dalmazzo"
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "latitude": 44.33,
                            "longitude": 7.49,
                            "name": "Borgo San Dalmazzo",
                            "country": "Italy",
                        }
                    ]
                },
            )
        if fail:
            return httpx.Response(503, text="private upstream details")
        return httpx.Response(
            200,
            json={
                "current": {
                    "temperature_2m": 12.3,
                    "weather_code": 2,
                    "is_day": 1,
                    "time": datetime.now(UTC).timestamp(),
                }
            },
        )

    weather = Weather("Borgo San Dalmazzo", transport=httpx.MockTransport(handler))
    results = await asyncio.gather(*(weather.read() for _ in range(5)))
    assert len(calls) == 2
    assert all(r["available"] and not r["stale"] for r in results)
    assert results[0]["temperature_c"] == 12.3
    clock[0] += 901
    fail = True
    result = await weather.read()
    assert result["stale"] and result["available"]
    await weather.read()
    assert len(calls) == 3
    assert "private" not in str(result)


async def test_weather_disabled_and_unknown_location():
    assert await Weather("").read() == {"configured": False}
    unknown = Weather(
        "unknown",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"results": []})),
    )
    assert await unknown.read() == {"configured": True, "available": False, "stale": True}


async def test_weather_routes_follow_dashboard_auth(settings):
    from epcube_bridge.app import create_app

    for disabled in (False, True):
        app = create_app(
            settings.model_copy(update={"web_disable_auth": disabled}), start_workers=False
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/weather")).status_code == 401
            assert (await client.get("/web/weather")).status_code == (200 if disabled else 401)
            response = await client.get(
                "/weather", headers={"Authorization": "Bearer local-api-secret"}
            )
            assert response.json() == {"configured": False}

import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from epcube_bridge.app import create_app
from epcube_bridge.bridge import Poller, Runtime
from epcube_bridge.errors import BridgeError
from epcube_bridge.history import History


def test_history_restart_retention_order_nulls_and_isolation(settings, snapshot, tmp_path):
    path = tmp_path / "history.sqlite3"
    store = History(settings, path)
    now = datetime.now(UTC)
    for delta in (30, 0, 60, 90000):
        store.append(
            snapshot.model_copy(
                update={"timestamp": now - timedelta(seconds=delta), "pv_power": None}
            )
        )
    store.append(snapshot.model_copy(update={"timestamp": now, "pv_power": None}))
    restored = History(settings, path)
    result = restored.read("123", now=now)
    assert len(result["points"]) == 3
    assert [p["timestamp"] for p in result["points"]] == sorted(
        p["timestamp"] for p in result["points"]
    )
    assert all(p["pv_power"] is None and p["home_power"] == 1800 for p in result["points"])
    assert restored.read("other", now=now)["points"] == []
    other = History(settings.model_copy(update={"epcube_region": "US"}), path)
    assert other.read("123", now=now)["points"] == []
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 3
    assert settings.epcube_email.get_secret_value().encode() not in path.read_bytes()


async def test_poll_records_only_success_and_disk_failure_preserves_live(
    settings, tmp_path, info, mode, stats
):
    history = History(settings, tmp_path / "history.sqlite3")
    runtime = Runtime(device={"id": "123", "serial": "serial"}, history=history)
    cloud = AsyncMock()
    cloud.get.side_effect = [info, mode, stats]
    poller = Poller(settings, cloud, runtime)
    await poller.poll_once()
    assert len(history.read("123")["points"]) == 1
    cloud.get.side_effect = [{**info, "isOnline": "0"}]
    with pytest.raises(BridgeError, match="device_offline"):
        await poller.poll_once()
    assert len(history.read("123")["points"]) == 1
    runtime.history = History(settings, tmp_path / "missing" / "history.sqlite3")
    cloud.get.side_effect = [info, mode]
    await poller.poll_once()
    assert runtime.available(30)
    assert runtime.history_error
    runtime.history = history
    cloud.get.side_effect = [info, mode]
    await poller.poll_once()
    assert not runtime.history_error


@pytest.mark.parametrize("disabled", [False, True])
async def test_history_api_auth_empty_errors_and_device_scope(
    settings, tmp_path, snapshot, disabled
):
    runtime = Runtime(device={"id": "123"}, history=History(settings, tmp_path / "history.sqlite3"))
    app = create_app(
        settings.model_copy(update={"web_disable_auth": disabled}),
        runtime=runtime,
        start_workers=False,
    )
    headers = {"Authorization": "Bearer " + settings.api_token.get_secret_value()}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/devices/123/history")).status_code == 401
        assert (await client.get("/web/devices/123/history")).status_code == (
            200 if disabled else 401
        )
        result = await client.get("/devices/123/history", headers=headers)
        assert result.status_code == 200
        assert result.json()["points"] == []
        assert result.json()["timezone"] == "Europe/Rome"
        assert result.json()["gap_threshold_s"] == 90
        runtime.history.append(snapshot.model_copy(update={"timestamp": datetime.now(UTC)}))
        assert (
            len((await client.get("/web/devices/123/history", headers=headers)).json()["points"])
            == 1
        )
        assert (await client.get("/devices/wrong/history", headers=headers)).status_code == 404
        assert (await client.post("/devices/123/history", headers=headers)).status_code == 405
        runtime.history = History(settings, tmp_path / "absent" / "bad.sqlite3")
        result = await client.get("/devices/123/history", headers=headers)
        assert result.status_code == 503
        assert result.json() == {"detail": "History unavailable"}


def test_daily_home_integration_midnight_gaps_and_nulls():
    from epcube_bridge.history import today_home_energy

    def point(t, power):
        return {"timestamp": t, "home_power": power}

    points = [
        point("2026-10-29T22:59:30+00:00", 1000),
        point("2026-10-29T23:00:30+00:00", 3000),
        point("2026-10-29T23:01:30+00:00", 3000),
        point("2026-10-29T23:30:00+00:00", 9000),
        point("2026-10-29T23:30:30+00:00", None),
    ]
    result = today_home_energy(points, "2026-10-29T23:30:30+00:00", "Europe/Rome", 90)
    # Midnight clips the first interval: 2500 W * 30 s + 3000 W * 60 s.
    assert result["energy_kwh"] == 0.071
    assert result["coverage_percent"] == 4.9
    assert (
        today_home_energy([], "2026-10-30T00:00:00+00:00", "Europe/Rome", 90)["energy_kwh"] is None
    )


def test_daily_home_dst_uses_elapsed_seconds_not_fixed_24_hours():
    from epcube_bridge.history import today_home_energy

    start = datetime(2026, 10, 24, 22, tzinfo=UTC)  # Midnight before autumn clock change.
    points = [
        {"timestamp": (start + timedelta(hours=i)).isoformat(), "home_power": 1000}
        for i in range(26)
    ]
    # 23:59 local on the 25-hour day.
    result = today_home_energy(points, "2026-10-25T22:59:00+00:00", "Europe/Rome", 3600)
    assert result["energy_kwh"] == 24.983
    assert result["coverage_percent"] == 100

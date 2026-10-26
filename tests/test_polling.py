import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest

from epcube_bridge.bridge import Poller, Runtime, backoff
from epcube_bridge.cloud import Cloud
from epcube_bridge.errors import BridgeError
from epcube_bridge.state import normalize
from epcube_bridge.storage import TokenStore


def test_normalization(snapshot):
    assert snapshot.soc == 64
    assert snapshot.pv_power == 1200
    assert snapshot.grid_power == -200
    assert snapshot.home_power == 1800
    assert snapshot.battery_power == 800  # Positive DISCHARGE, opposite upstream.
    assert snapshot.daily_grid_import_energy == 2.1
    assert snapshot.reserve_soc == 20


def test_missing_values_and_charge_sign(info, mode, stats):
    now = datetime.now(UTC)

    def convert():
        return normalize(
            "123",
            info,
            mode,
            stats,
            timestamp=now,
            stats_timestamp=now,
            day="2026-10-26",
            timezone="Europe/Rome",
        )

    info["gridPower"] = 200
    assert convert().battery_power == -1400
    info["gridPower"] = "NaN"
    assert convert().grid_power is None
    assert convert().battery_power is None
    info.pop("nonBackUpPower")
    assert convert().home_power is None
    stats["gridElectricityFrom"] = None
    assert convert().daily_grid_import_energy is None
    info["batterySoc"] = -1
    with pytest.raises(BridgeError):
        convert()


async def test_poll_http_cache_and_midnight(settings, tmp_path, info, mode, stats):
    seen = []

    def handler(request):
        seen.append(request)
        path = request.url.path.split("/")[-1]
        data = {
            "deviceList": [{"devId": 123, "sgSn": "serial"}],
            "homeDeviceInfo": info,
            "getSwitchMode": mode,
            "queryDataElectricityV2": stats,
        }[path]
        return httpx.Response(200, json={"status": 200, "data": data})

    store = TokenStore(settings, tmp_path)
    store.save("cached")
    runtime = Runtime()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        poller = Poller(settings, Cloud(settings, http, store), runtime)
        await poller.poll_once()
        assert runtime.available(30)
        assert runtime.snapshot.battery_power == 800
        await poller.poll_once()
        assert len(seen) == 6  # discovery + three initial reads + two cached-stat reads
        poller.stats_day = "2000-01-01"
        await poller.poll_once()
        assert len(seen) == 9
    assert all(r.method == "GET" for r in seen)
    stat_request = seen[-1]
    assert stat_request.url.params["scopeType"] == "1"
    assert stat_request.url.params["queryDateStr"] == runtime.snapshot.daily_energy_date


async def test_error_backoff_preserves_sample(settings, snapshot, monkeypatch, caplog):
    runtime = Runtime(snapshot=snapshot, cloud_online=True)
    poller = Poller(settings, AsyncMock(), runtime)
    poller.poll_once = AsyncMock(
        side_effect=[
            BridgeError("cloud_unavailable", retry_after=120),
            RuntimeError("secret-password bearer-token"),
            RuntimeError("secret-password bearer-token"),
        ]
    )
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr("epcube_bridge.bridge.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await poller.run()
    assert sleeps[0] >= 120
    assert sleeps[1] >= 60
    assert sleeps[2] >= 120
    assert runtime.snapshot is snapshot
    assert not runtime.cloud_online
    assert "secret-password" not in caplog.text
    assert "bearer-token" not in caplog.text


def test_backoff_is_bounded_and_never_below_interval():
    for failure in range(1, 100):
        assert 30 <= backoff(failure, 30) <= 900
    assert backoff(100, 30, 1800) == 1800


async def test_offline_device_does_not_refresh_sample(settings, snapshot, info):
    info["isOnline"] = "0"
    cloud = AsyncMock()
    cloud.get.return_value = info
    runtime = Runtime(device={"id": "123", "serial": "serial"}, snapshot=snapshot)
    with pytest.raises(BridgeError, match="device_offline"):
        await Poller(settings, cloud, runtime).poll_once()
    assert runtime.snapshot is snapshot
    assert cloud.get.await_count == 1


async def test_success_resets_backoff(settings, monkeypatch):
    poller = Poller(settings, AsyncMock(), Runtime())
    poller.poll_once = AsyncMock(
        side_effect=[BridgeError("network_error"), None, BridgeError("network_error")]
    )
    delays = []

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr("epcube_bridge.bridge.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await poller.run()
    assert 30 <= delays[0] <= 36
    assert delays[1] == 30
    assert 30 <= delays[2] <= 36

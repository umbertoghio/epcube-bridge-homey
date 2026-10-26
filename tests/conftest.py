import base64
import socket
from datetime import UTC, datetime
from io import BytesIO

import httpx
import numpy as np
import pytest
from PIL import Image

from epcube_bridge.config import Settings
from epcube_bridge.state import normalize


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Real network calls are forbidden in tests")

    async def forbidden_http(*args, **kwargs):
        forbidden()

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_http)


@pytest.fixture
def settings():
    return Settings(
        epcube_email="private@example.test",
        epcube_password="secret-password",
        mqtt_host="mqtt.test",
        api_token="local-api-secret",
    )


@pytest.fixture
def captcha_data():
    array = np.random.default_rng(42).integers(0, 255, (24, 40), dtype=np.uint8)

    def encoded(arr):
        stream = BytesIO()
        Image.fromarray(arr).save(stream, format="PNG")
        return base64.b64encode(stream.getvalue()).decode()

    return {
        "originalImageBase64": encoded(array),
        "jigsawImageBase64": encoded(array[:, 12:20]),
        "secretKey": "0123456789abcdef",
        "token": "puzzle-token",
    }


@pytest.fixture
def info():
    return {
        "batterySoc": "64",
        "solarPower": "120",
        "gridPower": "-20",
        "backUpPower": "150",
        "nonBackUpPower": "10",
        "evPower": "20",
        "solarDcElectricity": "3.4",
        "solarAcElectricity": "3.2",
        "evElectricity": "0.3",
    }


@pytest.fixture
def mode():
    return {"workStatus": "1", "selfConsumptioinReserveSoc": "20", "backupPowerReserveSoc": "100"}


@pytest.fixture
def stats():
    return {
        "solarElectricity": "3.2",
        "backUpElectricity": "4.1",
        "gridElectricityFrom": "2.1",
        "gridElectricityTo": "0.5",
    }


@pytest.fixture
def snapshot(info, mode, stats):
    now = datetime(2026, 10, 26, 12, tzinfo=UTC)
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

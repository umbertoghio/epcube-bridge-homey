import base64
import json
import os

import httpx
import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from epcube_bridge.captcha import _solve_captcha_image
from epcube_bridge.cloud import Cloud
from epcube_bridge.errors import BridgeError
from epcube_bridge.storage import TokenStore


def envelope(data):
    return httpx.Response(200, json={"status": 200, "data": data})


def test_local_solver(captcha_data):
    assert (
        _solve_captcha_image(captcha_data["originalImageBase64"], captcha_data["jigsawImageBase64"])
        == 12
    )


async def test_authentication_and_cached_restart(settings, tmp_path, captcha_data):
    requests = []

    def handler(request):
        requests.append(request)
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        if path.endswith("captcha/get"):
            assert "authorization" not in request.headers
            assert body["clientUid"]
            return envelope({"repData": captcha_data})
        if path.endswith("captcha/check"):
            clear = unpad(
                AES.new(b"0123456789abcdef", AES.MODE_ECB).decrypt(
                    base64.b64decode(body["pointJson"])
                ),
                16,
            ).decode()
            assert json.loads(clear) == {"x": 12.0, "y": 5.0}
            assert body["clientUid"] == json.loads(requests[0].content)["clientUid"]
            return envelope({"repCode": "0000"})
        if path.endswith("/login"):
            clear = unpad(
                AES.new(b"0123456789abcdef", AES.MODE_ECB).decrypt(
                    base64.b64decode(body["captchaVerification"])
                ),
                16,
            ).decode()
            assert clear == 'puzzle-token---{"x":12.0,"y":5.0}'
            assert body["password"] == "secret-password"
            return envelope({"token": "bearer-secret"})
        assert request.headers["authorization"] == "Bearer bearer-secret"
        return envelope([{"devId": 123, "sgSn": "serial"}])

    store = TokenStore(settings, tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await Cloud(settings, http, store).discover() == {"id": "123", "serial": "serial"}
        assert await Cloud(settings, http, store).discover() == {"id": "123", "serial": "serial"}
    assert [r.method for r in requests] == ["POST", "POST", "POST", "GET", "GET"]
    assert os.stat(store.path).st_mode & 0o777 == 0o600
    assert "secret-password" not in store.path.read_text()


@pytest.mark.parametrize("rejection", [401, 403, "envelope", "inner"])
async def test_reauth_once(settings, tmp_path, captcha_data, rejection):
    store = TokenStore(settings, tmp_path)
    store.save("old")
    counts = {"login": 0, "get": 0}

    def handler(request):
        path = request.url.path
        if path.endswith("captcha/get"):
            return envelope({"repData": captcha_data})
        if path.endswith("captcha/check"):
            return envelope({"repData": {"result": True}})
        if path.endswith("/login"):
            counts["login"] += 1
            return envelope({"token": "new"})
        counts["get"] += 1
        if request.headers["authorization"] == "Bearer old":
            if rejection == "envelope":
                return httpx.Response(200, json={"status": 401, "message": "private"})
            if rejection == "inner":
                return envelope({"status": 403})
            return httpx.Response(rejection)
        return envelope([{"devId": "123", "sgSn": "serial"}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await Cloud(settings, http, store).discover()
    assert counts == {"login": 1, "get": 2}
    assert store.load() == "new"


async def test_second_rejection_stops(settings, tmp_path, captcha_data):
    store = TokenStore(settings, tmp_path)
    store.save("old")
    logins = []

    def handler(request):
        if request.url.path.endswith("captcha/get"):
            return envelope({"repData": captcha_data})
        if request.url.path.endswith("captcha/check"):
            return envelope({"repCode": "0000"})
        if request.url.path.endswith("/login"):
            logins.append(1)
            return envelope({"token": "new"})
        return httpx.Response(403, text="secret-password old new")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(BridgeError, match="^auth_rejected$"):
            await Cloud(settings, http, store).discover()
    assert len(logins) == 1


async def test_false_captcha_success_is_not_login(settings, tmp_path, captcha_data, monkeypatch):
    requests = []

    async def sleep(_):
        pass

    monkeypatch.setattr("epcube_bridge.cloud.asyncio.sleep", sleep)

    def handler(request):
        requests.append(request.url.path)
        if request.url.path.endswith("captcha/get"):
            return envelope({"repData": captcha_data})
        return httpx.Response(200, json={"message": "Success", "data": {"repCode": "6111"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(BridgeError, match="captcha_failed"):
            await Cloud(settings, http, TokenStore(settings, tmp_path)).discover()
    assert len(requests) == 6
    assert not any(p.endswith("/login") for p in requests)


@pytest.mark.parametrize(
    "response,code",
    [
        (httpx.Response(429, headers={"Retry-After": "120"}), "cloud_unavailable"),
        (httpx.Response(500, text="secret-password"), "cloud_unavailable"),
        (httpx.Response(200, text="secret-password"), "invalid_json"),
        (httpx.Response(302, headers={"Location": "https://evil.test"}), "http_error"),
        (envelope({"status": 500, "message": "secret-password"}), "cloud_envelope_error"),
    ],
)
async def test_errors_are_safe(settings, tmp_path, response, code):
    store = TokenStore(settings, tmp_path)
    store.save("token-secret")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as http:
        with pytest.raises(BridgeError) as error:
            await Cloud(settings, http, store).discover()
    assert str(error.value) == code
    assert error.value.retry_after == (120 if response.status_code == 429 else 0)


@pytest.mark.parametrize("devices", [[], [{"devId": 1, "sgSn": "x"}] * 2])
async def test_single_unit_enforced(settings, tmp_path, devices):
    store = TokenStore(settings, tmp_path)
    store.save("token")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: envelope(devices))
    ) as http:
        with pytest.raises(BridgeError, match="expected_exactly_one_device"):
            await Cloud(settings, http, store).discover()


def test_cache_account_binding_and_corruption(settings, tmp_path):
    store = TokenStore(settings, tmp_path)
    store.save("secret-token")
    different = settings.model_copy(update={"epcube_region": "US"})
    assert TokenStore(different, tmp_path).load() is None
    store.path.write_text("not json")
    assert store.load() is None


async def test_no_control_or_dynamic_routes(settings, tmp_path):
    store = TokenStore(settings, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("network"))
    ) as http:
        cloud = Cloud(settings, http, store)
        for method, path in [("POST", "/device/switchMode"), ("GET", "/dynamicPrice/calcuPrice")]:
            with pytest.raises(BridgeError, match="route_not_allowed"):
                await cloud._request(method, path)


async def test_simultaneous_expired_requests_share_login(settings, tmp_path, captcha_data):
    import asyncio

    store = TokenStore(settings, tmp_path)
    store.save("old")
    old_count = 0
    logins = 0
    both = asyncio.Event()

    async def handler(request):
        nonlocal old_count, logins
        if request.url.path.endswith("captcha/get"):
            return envelope({"repData": captcha_data})
        if request.url.path.endswith("captcha/check"):
            return envelope({"repCode": "0000"})
        if request.url.path.endswith("/login"):
            logins += 1
            return envelope({"token": "new"})
        if request.headers["authorization"] == "Bearer old":
            old_count += 1
            if old_count == 2:
                both.set()
            await both.wait()
            return httpx.Response(403)
        return envelope({"workStatus": "1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        cloud = Cloud(settings, http, store)
        await asyncio.wait_for(
            asyncio.gather(cloud.get("/device/getSwitchMode"), cloud.get("/device/getSwitchMode")),
            2,
        )
    assert logins == 1


def test_retry_after_dates_and_invalid_values():
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    from epcube_bridge.cloud import retry_seconds

    value = format_datetime(datetime.now(UTC) + timedelta(minutes=2))
    assert 118 <= retry_seconds(value) <= 120
    for value in (None, "garbage", "inf", "nan", "-2"):
        assert retry_seconds(value) == 0

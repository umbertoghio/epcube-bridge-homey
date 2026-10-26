import asyncio
import math
import uuid
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from .captcha import _build_captcha_verification, _encrypt_point, _solve_captcha_image
from .config import REGIONS, Settings
from .errors import BridgeError
from .storage import TokenStore

HEADERS = {
    "User-Agent": "ReservoirMonitoring/2.1.0 (iPhone; iOS 18.3.2; Scale/3.00)",
    "Accept": "*/*",
    "Content-Type": "application/json",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "en-GB",
}
READ_ROUTES = frozenset(
    {
        "/device/deviceList",
        "/device/homeDeviceInfo",
        "/device/getSwitchMode",
        "/device/queryDataElectricityV2",
    }
)
AUTH_ROUTES = frozenset(
    {
        "/open/common/captcha/get",
        "/open/common/captcha/check",
        "/open/common/login",
    }
)


def retry_seconds(value: str | None) -> float:
    if not value:
        return 0
    try:
        seconds = float(value)
        return max(0, seconds) if math.isfinite(seconds) else 0
    except ValueError:
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


class Cloud:
    def __init__(self, settings: Settings, http: httpx.AsyncClient, store: TokenStore):
        self.settings, self.http, self.store = settings, http, store
        self.root = REGIONS[settings.epcube_region]
        self._token = store.load()
        self._lock = asyncio.Lock()

    async def _request(self, method: str, path: str, *, token=None, **kwargs):
        if not (
            (method == "GET" and path in READ_ROUTES) or (method == "POST" and path in AUTH_ROUTES)
        ):
            raise BridgeError("route_not_allowed")
        headers = dict(HEADERS)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = await self.http.request(
                method, self.root + path, headers=headers, follow_redirects=False, **kwargs
            )
        except httpx.HTTPError:
            raise BridgeError("network_error") from None
        if response.status_code in (401, 403):
            raise BridgeError("auth_rejected")
        if response.status_code == 429 or response.status_code >= 500:
            raise BridgeError(
                "cloud_unavailable", retry_seconds(response.headers.get("Retry-After"))
            )
        if not 200 <= response.status_code < 300:
            raise BridgeError("http_error")
        try:
            body = response.json()
        except ValueError:
            raise BridgeError("invalid_json") from None
        if not isinstance(body, dict):
            raise BridgeError("invalid_envelope")
        # Some firmware embeds auth errors under HTTP 200.
        for envelope in (body, body.get("data")):
            if not isinstance(envelope, dict):
                continue
            try:
                code = int(envelope.get("status", 200))
            except (ValueError, TypeError):
                continue
            if code in (401, 403):
                raise BridgeError("auth_rejected")
            if code >= 400:
                raise BridgeError("cloud_envelope_error")
        if body.get("status", 200) not in (200, "200"):
            raise BridgeError("cloud_envelope_error")
        return body

    async def _login(self) -> str:
        for _ in range(3):
            uid = str(uuid.uuid4())
            body = await self._request("POST", "/open/common/captcha/get", json={"clientUid": uid})
            data = body.get("data", body)
            try:
                captcha = data.get("repData") or data
                x = await asyncio.to_thread(
                    _solve_captcha_image,
                    captcha["originalImageBase64"],
                    captcha["jigsawImageBase64"],
                )
                if x is None:
                    await asyncio.sleep(1)
                    continue
                point = _encrypt_point(x, 5, captcha["secretKey"])
                verification = _build_captcha_verification(
                    captcha["token"], x, 5, captcha["secretKey"]
                )
            except Exception:
                raise BridgeError("invalid_captcha") from None
            checked = await self._request(
                "POST",
                "/open/common/captcha/check",
                json={
                    "clientUid": uid,
                    "token": captcha["token"],
                    "pointJson": point,
                },
            )
            check = checked.get("data") or {}
            rep = (check.get("repData") or {}) if isinstance(check, dict) else {}
            if not isinstance(check, dict) or not (
                check.get("repCode") == "0000"
                or (isinstance(rep, dict) and rep.get("result") is True)
            ):
                # Bound puzzle retries and pace even within one login cycle.
                await asyncio.sleep(1)
                continue
            logged = await self._request(
                "POST",
                "/open/common/login",
                json={
                    "userName": self.settings.epcube_email.get_secret_value(),
                    "password": self.settings.epcube_password.get_secret_value(),
                    "captchaVerification": verification,
                },
            )
            result = logged.get("data", logged)
            token = result.get("token") if isinstance(result, dict) else None
            if not isinstance(token, str) or not token:
                raise BridgeError("login_rejected")
            try:
                await asyncio.to_thread(self.store.save, token)
            except OSError:
                raise BridgeError("token_storage_error") from None
            return token
        raise BridgeError("captcha_failed")

    async def authenticate(self, rejected: str | None = None):
        async with self._lock:
            if self._token is None or self._token == rejected:
                # Invalidate before attempting so a failed re-login cannot reuse it.
                self._token = None
                self._token = await self._login()

    async def get(self, path: str, **params):
        await self.authenticate()
        token = self._token
        try:
            body = await self._request("GET", path, token=token, params=params)
        except BridgeError as exc:
            if exc.code != "auth_rejected":
                raise
            await self.authenticate(rejected=token)
            body = await self._request("GET", path, token=self._token, params=params)
        return body.get("data")

    async def discover(self) -> dict:
        devices = await self.get("/device/deviceList")
        if not isinstance(devices, list) or len(devices) != 1:
            raise BridgeError("expected_exactly_one_device")
        device = devices[0]
        if not isinstance(device, dict):
            raise BridgeError("invalid_device")
        dev_id, serial = device.get("devId", device.get("id")), device.get("sgSn")
        if dev_id is None or not serial:
            raise BridgeError("invalid_device")
        dev_id = str(dev_id)
        if not dev_id or any(c in dev_id for c in "/+#\0"):
            raise BridgeError("invalid_device_id")
        return {"id": dev_id, "serial": str(serial)}

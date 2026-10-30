import asyncio
import hmac
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from .bridge import Poller, Runtime
from .cloud import Cloud
from .config import Settings
from .history import History, today_home_energy
from .mqtt import MqttWorker
from .storage import TokenStore
from .weather import Weather


def create_app(
    settings: Settings, *, runtime: Runtime | None = None, start_workers=True
) -> FastAPI:
    state = runtime if runtime is not None else Runtime()
    weather = Weather(settings.weather_location)

    @asynccontextmanager
    async def lifespan(app):
        if not start_workers:
            yield
            return
        if state.history is None:
            state.history = History(settings)
        async with httpx.AsyncClient(timeout=20, trust_env=False) as http:
            cloud = Cloud(settings, http, TokenStore(settings))
            tasks = [
                asyncio.create_task(Poller(settings, cloud, state).run()),
                asyncio.create_task(MqttWorker(settings, state).run()),
            ]
            try:
                yield
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    # Public dashboard shell contains no data; all device APIs still require a bearer token.
    app = FastAPI(
        title="epcube-bridge", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.runtime = state

    web_root = Path(__file__).parent / "web"

    @app.middleware("http")
    async def response_security(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    @app.get("/", include_in_schema=False)
    async def dashboard():
        html = (web_root / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(
            html.replace(
                'data-auth-required="true"',
                f'data-auth-required="{str(not settings.web_disable_auth).lower()}"',
            )
        )

    @app.get("/assets/{name}", include_in_schema=False)
    async def dashboard_asset(name: str):
        # A fixed allowlist exposes no filesystem paths, config or token store.
        assets = {
            "dashboard.css": "text/css",
            "dashboard.js": "text/javascript",
            "format.mjs": "text/javascript",
            "history.mjs": "text/javascript",
        }
        if name not in assets:
            raise HTTPException(404, "Not found")
        return FileResponse(web_root / name, media_type=assets[name])

    async def authenticate(authorization: str | None = Header(default=None)):
        scheme, _, token = (authorization or "").partition(" ")
        expected = settings.api_token.get_secret_value().encode()
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), expected):
            raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})

    async def authenticate_web(authorization: str | None = Header(default=None)):
        if not settings.web_disable_auth:
            await authenticate(authorization)

    @app.get("/weather", dependencies=[Depends(authenticate)])
    async def current_weather():
        return await weather.read()

    app.add_api_route(
        "/web/weather",
        current_weather,
        methods=["GET"],
        dependencies=[Depends(authenticate_web)],
        include_in_schema=False,
    )

    @app.get("/health")
    async def health():
        # Liveness, not upstream readiness: outages must not cause container restart loops.
        return {
            "status": "ok",
            "cloud_online": state.available(settings.poll_interval_s),
            "mqtt_connected": state.mqtt_connected,
        }

    @app.get("/devices", dependencies=[Depends(authenticate)])
    async def devices():
        return (
            [{"id": state.device["id"], "available": state.available(settings.poll_interval_s)}]
            if state.device
            else []
        )

    @app.get("/devices/{device_id}/state", dependencies=[Depends(authenticate)])
    async def device_state(device_id: str):
        if state.device is None or device_id != state.device["id"]:
            raise HTTPException(404, "Device not found")
        if state.snapshot is None:
            raise HTTPException(503, "No sample available")
        return {
            **state.snapshot.model_dump(mode="json"),
            "available": state.available(settings.poll_interval_s),
            "age_s": round(time.monotonic() - state.last_success, 1)
            if state.last_success
            else None,
        }

    @app.get("/devices/{device_id}/history", dependencies=[Depends(authenticate)])
    async def device_history(device_id: str):
        if state.device is None or device_id != state.device["id"]:
            raise HTTPException(404, "Device not found")
        if state.history is None:
            raise HTTPException(503, "History unavailable")
        try:
            result = await asyncio.to_thread(state.history.read, device_id)
        except Exception:
            raise HTTPException(503, "History unavailable") from None
        return {
            **result,
            "timezone": settings.epcube_timezone,
            "gap_threshold_s": max(90, 3 * settings.poll_interval_s),
            "recording_available": not state.history_error,
            "today_home": today_home_energy(
                result["points"],
                result["end"],
                settings.epcube_timezone,
                max(90, 3 * settings.poll_interval_s),
            ),
        }

    app.add_api_route(
        "/web/devices/{device_id}/history",
        device_history,
        methods=["GET"],
        dependencies=[Depends(authenticate_web)],
        include_in_schema=False,
    )
    # Dashboard reads have an explicit opt-out; the original REST API stays protected.
    app.add_api_route(
        "/web/devices",
        devices,
        methods=["GET"],
        dependencies=[Depends(authenticate_web)],
        include_in_schema=False,
    )
    app.add_api_route(
        "/web/devices/{device_id}/state",
        device_state,
        methods=["GET"],
        dependencies=[Depends(authenticate_web)],
        include_in_schema=False,
    )
    return app

import asyncio
import hmac
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException

from .bridge import Poller, Runtime
from .cloud import Cloud
from .config import Settings
from .mqtt import MqttWorker
from .storage import TokenStore


def create_app(
    settings: Settings, *, runtime: Runtime | None = None, start_workers=True
) -> FastAPI:
    state = runtime if runtime is not None else Runtime()

    @asynccontextmanager
    async def lifespan(app):
        if not start_workers:
            yield
            return
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

    # Disable documentation/OpenAPI routes; /health is the only unauthenticated route.
    app = FastAPI(
        title="epcube-bridge", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.runtime = state

    async def authenticate(authorization: str | None = Header(default=None)):
        scheme, _, token = (authorization or "").partition(" ")
        expected = settings.api_token.get_secret_value().encode()
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), expected):
            raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})

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

    return app

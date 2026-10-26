import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .cloud import Cloud
from .config import Settings
from .errors import BridgeError
from .state import Snapshot, normalize

LOG = logging.getLogger(__name__)


def backoff(failures: int, base: float, retry_after: float = 0) -> float:
    ceiling = max(900, base)
    delay = min(ceiling, base * (2 ** min(max(failures - 1, 0), 16)))
    return max(base, min(ceiling, delay * random.uniform(1, 1.2)), retry_after)


@dataclass
class Runtime:
    device: dict | None = None
    snapshot: Snapshot | None = None
    cloud_online: bool = False
    mqtt_connected: bool = False
    last_success: float | None = None
    cloud_error: str | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def available(self, interval: float) -> bool:
        return (
            self.cloud_online
            and self.last_success is not None
            and time.monotonic() - self.last_success <= max(90, interval * 3)
        )


class Poller:
    def __init__(self, settings: Settings, cloud: Cloud, runtime: Runtime):
        self.settings, self.cloud, self.runtime = settings, cloud, runtime
        self.stats = None
        self.stats_day = None
        self.stats_timestamp = None
        self.stats_monotonic = 0.0
        self.zone = ZoneInfo(settings.epcube_timezone)

    async def poll_once(self):
        if self.runtime.device is None:
            self.runtime.device = await self.cloud.discover()
            self.runtime.changed.set()
        device = self.runtime.device
        # Sequential requests avoid bursts and competing re-auth cycles.
        info = await self.cloud.get(
            "/device/homeDeviceInfo", sgSn=device["serial"], dayMonthYearFormat="YYYY-MM-DD"
        )
        if isinstance(info, dict) and str(info.get("isOnline", "1")).lower() in ("0", "false"):
            raise BridgeError("device_offline")
        mode = await self.cloud.get("/device/getSwitchMode", devId=device["id"])
        now = datetime.now(UTC)
        day = now.astimezone(self.zone).date().isoformat()
        if (
            self.stats is None
            or day != self.stats_day
            or time.monotonic() - self.stats_monotonic >= 300
        ):
            stats = await self.cloud.get(
                "/device/queryDataElectricityV2", devId=device["id"], queryDateStr=day, scopeType=1
            )
            if not isinstance(stats, dict):
                raise BridgeError("invalid_statistics")
            self.stats, self.stats_day = stats, day
            self.stats_timestamp, self.stats_monotonic = datetime.now(UTC), time.monotonic()
        snapshot = normalize(
            device["id"],
            info,
            mode,
            self.stats,
            timestamp=datetime.now(UTC),
            stats_timestamp=self.stats_timestamp,
            day=day,
            timezone=self.settings.epcube_timezone,
        )
        self.runtime.snapshot = snapshot
        self.runtime.last_success = time.monotonic()
        self.runtime.cloud_online = True
        self.runtime.cloud_error = None
        self.runtime.changed.set()

    async def run(self):
        failures = 0
        while True:
            try:
                await self.poll_once()
            except Exception as exc:
                failures += 1
                self.runtime.cloud_online = False
                # Never interpolate untrusted exception messages or response bodies.
                code = exc.code if isinstance(exc, BridgeError) else "poll_failed"
                self.runtime.cloud_error = code
                self.runtime.changed.set()
                delay = backoff(
                    failures,
                    self.settings.poll_interval_s,
                    exc.retry_after if isinstance(exc, BridgeError) else 0,
                )
                LOG.warning("Cloud poll failed (%s); retry in %.0fs", code, delay)
            else:
                failures = 0
                delay = self.settings.poll_interval_s
            # Delay after completion, so slow/failing calls cannot create a tight loop.
            await asyncio.sleep(delay)

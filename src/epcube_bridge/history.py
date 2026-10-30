"""Local rolling telemetry history. SQLite work runs off the asyncio event loop."""

import hashlib
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings
from .state import Snapshot

WINDOW_S = 24 * 60 * 60


class History:
    def __init__(self, settings: Settings, path: Path = Path("/data/history.sqlite3")):
        self.path = path
        # Account/region separation without storing the email or credentials.
        identity = settings.epcube_region + "\0" + settings.epcube_email.get_secret_value()
        self.scope = hashlib.sha256(identity.encode()).hexdigest()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=2)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS samples ("
            "scope TEXT NOT NULL, device TEXT NOT NULL, ts REAL NOT NULL, "
            "soc REAL, pv_power REAL, home_power REAL, "
            "PRIMARY KEY(scope, device, ts)) WITHOUT ROWID"
        )
        connection.execute("CREATE INDEX IF NOT EXISTS samples_time ON samples(ts)")
        return connection

    def append(self, sample: Snapshot):
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "DELETE FROM samples WHERE ts < ?", (datetime.now(UTC).timestamp() - WINDOW_S,)
            )
            connection.execute(
                "INSERT OR REPLACE INTO samples VALUES (?, ?, ?, ?, ?, ?)",
                (
                    self.scope,
                    sample.device_id,
                    sample.timestamp.timestamp(),
                    sample.soc,
                    sample.pv_power,
                    sample.home_power,
                ),
            )

    def read(self, device_id: str, *, now: datetime | None = None) -> dict:
        end = (now or datetime.now(UTC)).timestamp()
        start = end - WINDOW_S
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM samples WHERE ts < ?", (start,))
            rows = connection.execute(
                "SELECT ts, soc, pv_power, home_power FROM samples "
                "WHERE scope = ? AND device = ? AND ts >= ? AND ts <= ? ORDER BY ts",
                (self.scope, device_id, start, end),
            ).fetchall()
        return {
            "device_id": device_id,
            "start": datetime.fromtimestamp(start, UTC).isoformat(),
            "end": datetime.fromtimestamp(end, UTC).isoformat(),
            "points": [
                {
                    "timestamp": datetime.fromtimestamp(ts, UTC).isoformat(),
                    "soc": soc,
                    "pv_power": pv,
                    "home_power": home,
                }
                for ts, soc, pv, home in rows
            ],
        }


def today_home_energy(points: list[dict], end: str, timezone: str, gap_s: float) -> dict:
    """Trapezoidal estimate over recorded intervals only; never fill outages."""
    from zoneinfo import ZoneInfo

    now = datetime.fromisoformat(end)
    midnight = now.astimezone(ZoneInfo(timezone)).replace(hour=0, minute=0, second=0, microsecond=0)
    start_s, end_s = midnight.timestamp(), now.timestamp()
    watt_seconds = coverage = 0.0
    for left, right in zip(points, points[1:]):
        a, b = (
            datetime.fromisoformat(left["timestamp"]).timestamp(),
            datetime.fromisoformat(right["timestamp"]).timestamp(),
        )
        pa, pb = left["home_power"], right["home_power"]
        if pa is None or pb is None or pa < 0 or pb < 0 or not 0 < b - a <= gap_s:
            continue
        lo, hi = max(a, start_s), min(b, end_s)
        if hi <= lo:
            continue
        p_lo = pa + (pb - pa) * (lo - a) / (b - a)
        p_hi = pa + (pb - pa) * (hi - a) / (b - a)
        watt_seconds += (p_lo + p_hi) / 2 * (hi - lo)
        coverage += hi - lo
    elapsed = max(0, end_s - start_s)
    return {
        "energy_kwh": round(watt_seconds / 3_600_000, 3) if coverage else None,
        "estimated": True,
        "coverage_percent": round(100 * coverage / elapsed, 1) if elapsed else 0,
    }

import math
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from .errors import BridgeError


class Snapshot(BaseModel):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False)
    device_id: str
    timestamp: datetime  # UTC timestamp of successful telemetry retrieval
    daily_energy_date: str
    daily_energy_timestamp: datetime
    timezone: str
    soc: float | None
    pv_power: float | None
    grid_power: float | None
    battery_power: float | None
    home_power: float | None
    mode: str
    reserve_soc: float | None
    daily_pv_energy: float | None
    daily_backup_energy: float | None
    daily_grid_import_energy: float | None
    daily_grid_export_energy: float | None
    daily_solar_dc_energy: float | None
    daily_solar_ac_energy: float | None
    daily_ev_energy: float | None

    def metrics(self) -> dict:
        return self.model_dump(
            mode="json",
            exclude={
                "device_id",
                "timestamp",
                "daily_energy_date",
                "daily_energy_timestamp",
                "timezone",
            },
        )


def number(value, *, scale=1, low=None, high=None):
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        value = float(value) * scale
        if not math.isfinite(value) or (low is not None and value < low):
            return None
        if high is not None and value > high:
            return None
        return round(value, 6)
    except (ValueError, TypeError, OverflowError):
        return None


def normalize(
    device_id: str,
    info: dict,
    mode: dict,
    stats: dict,
    *,
    timestamp: datetime,
    stats_timestamp: datetime,
    day: str,
    timezone: str,
) -> Snapshot:
    if not all(isinstance(x, dict) for x in (info, mode, stats)):
        raise BridgeError("invalid_telemetry")
    # A malformed success response must not masquerade as a successful sample.
    if number(info.get("batterySoc"), low=0, high=100) is None:
        raise BridgeError("invalid_telemetry")
    pv = number(info.get("solarPower"), scale=10)
    grid = number(info.get("gridPower"), scale=10)
    backup = number(info.get("backUpPower"), scale=10)
    nonbackup = number(info.get("nonBackUpPower"), scale=10)
    has_ev = any(str(info.get(k, "")).lower() in ("1", "true") for k in ("hasEv", "hasEvse"))
    # Optional EV channel is absent on older installations without a charger.
    ev = number(info.get("evPower", None if has_ev else 0), scale=10)
    home = (
        round(backup + nonbackup + ev, 6)
        if all(x is not None for x in (backup, nonbackup, ev))
        else None
    )
    battery = round(home - pv - grid, 6) if all(x is not None for x in (home, pv, grid)) else None
    code = str(mode.get("workStatus", info.get("workStatus", "")))
    mode_name = {"1": "self_consumption", "2": "time_of_use", "3": "backup"}.get(code, "unknown")
    reserve_key = "backupPowerReserveSoc" if code == "3" else "selfConsumptioinReserveSoc"
    rollup = {k.lower(): v for k, v in stats.items()}
    return Snapshot(
        device_id=device_id,
        timestamp=timestamp,
        daily_energy_date=day,
        daily_energy_timestamp=stats_timestamp,
        timezone=timezone,
        soc=number(info.get("batterySoc"), low=0, high=100),
        pv_power=pv,
        grid_power=grid,
        battery_power=battery,
        home_power=home,
        mode=mode_name,
        reserve_soc=number(mode.get(reserve_key), low=0, high=100)
        if code in ("1", "2", "3")
        else None,
        # Use rollup solar/backup totals so these share the explicit date bucket.
        daily_pv_energy=number(rollup.get("solarelectricity"), low=0),
        daily_backup_energy=number(rollup.get("backupelectricity"), low=0),
        daily_grid_import_energy=number(rollup.get("gridelectricityfrom"), low=0),
        daily_grid_export_energy=number(rollup.get("gridelectricityto"), low=0),
        daily_solar_dc_energy=number(info.get("solarDcElectricity"), low=0),
        daily_solar_ac_energy=number(info.get("solarAcElectricity"), low=0),
        daily_ev_energy=number(info.get("evElectricity"), low=0),
    )

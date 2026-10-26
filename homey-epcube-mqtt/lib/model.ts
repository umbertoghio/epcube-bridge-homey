export const STALE_MS = 10 * 60 * 1000;
export const MODES = ['self_consumption', 'time_of_use', 'backup', 'unknown'] as const;
export type Mode = typeof MODES[number];
export const METRICS = [
  'soc', 'pv_power', 'grid_power', 'battery_power', 'home_power', 'reserve_soc',
  'daily_pv_energy', 'daily_backup_energy', 'daily_grid_import_energy',
  'daily_grid_export_energy', 'daily_solar_dc_energy', 'daily_solar_ac_energy', 'daily_ev_energy',
] as const;
export type Metric = typeof METRICS[number];
export type Snapshot = Record<Metric, number | null> & {
  device_id: string; timestamp: string; mode: Mode;
};
export interface Reading {
  id: string;
  discovered: boolean;
  availability: 'online' | 'offline' | 'unknown';
  snapshot?: Snapshot;
  receivedAt?: number;
}

export function parseSnapshot(payload: Buffer, id: string, now: number): Snapshot | null {
  if (payload.length > 64 * 1024) return null;
  try {
    const raw = JSON.parse(payload.toString('utf8'));
    if (!raw || Array.isArray(raw) || typeof raw !== 'object' || raw.device_id !== id) return null;
    if (typeof raw.timestamp !== 'string' || !/^\d{4}-\d\d-\d\dT.*(?:Z|[+-]\d\d:\d\d)$/.test(raw.timestamp)) return null;
    const sampleTime = Date.parse(raw.timestamp);
    if (!Number.isFinite(sampleTime) || sampleTime > now + 60_000) return null;
    if (!MODES.includes(raw.mode)) return null;
    const result = { device_id: id, timestamp: raw.timestamp, mode: raw.mode } as Snapshot;
    for (const key of METRICS) {
      const value = raw[key];
      if (value === undefined || value === null) { result[key] = null; continue; }
      if (typeof value !== 'number' || !Number.isFinite(value)) return null;
      if ((key === 'soc' || key === 'reserve_soc') && (value < 0 || value > 100)) return null;
      if (key.startsWith('daily_') && value < 0) return null;
      result[key] = value;
    }
    return result;
  } catch { return null; }
}

export function unavailableReason(reading: Reading | undefined, connected: boolean, now: number): string | null {
  if (!connected) return 'MQTT broker disconnected';
  if (!reading || reading.availability === 'unknown') return 'Waiting for bridge availability';
  if (reading.availability === 'offline') return 'EP Cube bridge is offline';
  if (!reading.snapshot || reading.receivedAt === undefined) return 'Waiting for EP Cube state';
  if (now - reading.receivedAt >= STALE_MS || now - Date.parse(reading.snapshot.timestamp) >= STALE_MS) {
    return 'No fresh EP Cube state for 10 minutes';
  }
  return null;
}

export const CAPABILITIES: Record<string, Metric> = {
  measure_battery: 'soc', measure_power: 'battery_power',
  'measure_power.pv': 'pv_power', 'measure_power.grid': 'grid_power', 'measure_power.home': 'home_power',
  'meter_power.daily_pv': 'daily_pv_energy', 'meter_power.daily_backup': 'daily_backup_energy',
  'meter_power.daily_grid_import': 'daily_grid_import_energy', 'meter_power.daily_grid_export': 'daily_grid_export_energy',
  'meter_power.daily_solar_dc': 'daily_solar_dc_energy', 'meter_power.daily_solar_ac': 'daily_solar_ac_energy',
  'meter_power.daily_ev': 'daily_ev_energy',
};
export function capabilityValues(snapshot: Snapshot): Record<string, number | string | null> {
  const values: Record<string, number | string | null> = { epcube_mode: snapshot.mode };
  for (const [capability, metric] of Object.entries(CAPABILITIES)) {
    const value = snapshot[metric];
    // Homey batteries consume positive watts; bridge reports discharge-positive watts.
    values[capability] = capability === 'measure_power' && value !== null ? -value : value;
  }
  return values;
}

export type Trigger = 'soc_below' | 'soc_above' | 'mode_changed' | 'grid_import_above' | 'grid_export_above';
export function crosses(kind: Trigger, before: Snapshot, after: Snapshot, threshold: unknown): boolean {
  if (kind === 'mode_changed') return before.mode !== after.mode;
  if (typeof threshold !== 'number' || !Number.isFinite(threshold) || threshold < 0) return false;
  if (kind.startsWith('soc_') && threshold > 100) return false;
  let prev = kind.startsWith('soc_') ? before.soc : before.grid_power;
  let next = kind.startsWith('soc_') ? after.soc : after.grid_power;
  if (prev === null || next === null) return false;
  if (kind === 'grid_export_above') { prev = Math.max(0, -prev); next = Math.max(0, -next); }
  if (kind === 'grid_import_above') { prev = Math.max(0, prev); next = Math.max(0, next); }
  return kind === 'soc_below' ? prev >= threshold && next < threshold : prev <= threshold && next > threshold;
}

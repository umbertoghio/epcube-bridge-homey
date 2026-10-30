export const isNumber = value => typeof value === 'number' && Number.isFinite(value);
export function power(value) { return isNumber(value) ? `${(Math.abs(value) / 1000).toFixed(2)} kW` : '—'; }
export function energy(value) { return isNumber(value) ? `${value.toFixed(2)} kWh` : '—'; }
export function percent(value) { return isNumber(value) ? `${Math.round(value)}%` : '—'; }
export function gridDirection(value) {
  return !isNumber(value) ? 'Unavailable' : value > 0 ? 'Importing' : value < 0 ? 'Exporting' : 'No exchange';
}
export function batteryDirection(value) {
  return !isNumber(value) ? 'Unavailable' : value > 0 ? 'Discharging' : value < 0 ? 'Charging' : 'Idle';
}
export function elapsed(timestamp, now = Date.now()) {
  const parsed = Date.parse(timestamp);
  if (!Number.isFinite(parsed)) return 'Sample time unknown';
  const seconds = Math.max(0, Math.floor((now - parsed) / 1000));
  if (seconds < 5) return 'Updated just now';
  if (seconds < 60) return `Updated ${seconds}s ago`;
  if (seconds < 3600) return `Updated ${Math.floor(seconds / 60)}m ago`;
  return `Updated ${Math.floor(seconds / 3600)}h ago`;
}
export const modes = {
  self_consumption: ['Self-consumption', 'Solar energy powers your home first. Surplus energy can be stored for later.'],
  time_of_use: ['Time of use', 'Your battery follows the operating schedule configured on your EP Cube.'],
  backup: ['Backup', 'Your battery is configured to keep energy available for a grid outage.'],
  unknown: ['Unknown mode', 'The bridge has not reported a recognized operating mode.'],
};

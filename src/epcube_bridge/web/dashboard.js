import { isNumber, power, energy, percent, gridDirection, batteryDirection, elapsed } from './format.mjs';
import { renderHistory, readingLabel } from './history.mjs';

const $ = id => document.getElementById(id);
const authRequired = document.body.dataset.authRequired !== 'false';
const tokenKey = 'epcube.apiToken';
function savedToken() {
  try { return localStorage.getItem(tokenKey) || ''; } catch { return ''; }
}
function storeToken(value) {
  try {
    if (value) localStorage.setItem(tokenKey, value);
    else localStorage.removeItem(tokenKey);
  } catch { /* Storage can be blocked; the current tab still works. */ }
}
let token = authRequired ? savedToken() : '';
let deviceId = null;
let snapshot = null;
let history = null;
let historyRequest = null;
let historyAttempt = 0;
let weatherAttempt = 0;
let weatherRequest = null;
let timer = null;
let request = null;
let generation = 0;
let failures = 0;
let networkFailed = false;
let lastFetch = 0;
const initialText = new Map([
  'device-label', 'updated', 'grid-value', 'grid-direction', 'solar-value',
  'solar-direction', 'home-label', 'home-value', 'soc', 'battery-value',
  'battery-direction',
  'daily-solar', 'daily-import', 'daily-export', 'daily-home', 'daily-home-note',
  'day-label', 'rollup-note',
].map(id => [id, $(id).textContent]));

function text(id, value) { $(id).textContent = value; }
function historyNotice(message) { $('history-status').hidden = !message; text('history-status', message); }
function notice(message) { $('notice').hidden = !message; text('notice', message); }
function setStatus(label, stale = false) {
  text('status', label);
  $('status').className = `status${stale ? ' stale' : ''}`;
  $('flow-diagram').classList.toggle('stale-view', stale);
}
function line(id, value, reverse = false) {
  $(id).classList.toggle('active', !networkFailed && snapshot?.available === true && isNumber(value) && value !== 0);
  $(id).classList.toggle('reverse', reverse);
}
function render() {
  if (!snapshot) return;
  const s = snapshot;
  const stale = networkFailed || s.available !== true;
  setStatus(stale ? 'Last reading' : 'Live data', stale);
  text('device-label', `EP Cube ${s.device_id}`);
  text('updated', elapsed(s.timestamp));
  text('grid-value', power(s.grid_power));
  text('grid-direction', gridDirection(s.grid_power));
  text('solar-value', power(s.pv_power));
  text('solar-direction', 'Generation');
  text('home-label', 'Home');
  text('home-value', power(s.home_power));
  text('soc', percent(s.soc));
  $('soc-ring').setAttribute('stroke-dashoffset', String(isNumber(s.soc) ? 100 - Math.max(0, Math.min(100, s.soc)) : 100));
  text('battery-value', power(s.battery_power));
  text('battery-direction', batteryDirection(s.battery_power));
  text('daily-solar', energy(s.daily_pv_energy));
  text('daily-import', energy(s.daily_grid_import_energy));
  text('daily-export', energy(s.daily_grid_export_energy));
  text('day-label', typeof s.daily_energy_date === 'string' ? s.daily_energy_date : '—');
  text('rollup-note', `Energy totals: ${s.timezone || 'device time'} · ${elapsed(s.daily_energy_timestamp).toLowerCase()}`);
  line('grid-flow', s.grid_power, s.grid_power < 0);
  line('solar-flow', s.pv_power, s.pv_power < 0);
  line('home-flow', s.home_power, s.home_power < 0);
  if (!networkFailed) notice(stale ? 'The bridge is offline or its data is stale. Values show the last successful reading.' : '');
}
function resetView(message = '') {
  storeToken('');
  generation += 1;
  if (timer) clearTimeout(timer);
  timer = null;
  request?.abort(); request = null;
  token = ''; deviceId = null; snapshot = null; failures = 0; networkFailed = false;
  lastFetch = 0;
  weatherRequest?.abort(); weatherRequest = null; weatherAttempt = 0; $('weather').hidden = true;
  historyRequest?.abort(); historyRequest = null; history = null; historyAttempt = 0;
  $('soc-chart').replaceChildren(); $('power-chart').replaceChildren();
  $('history-inspect').hidden = true;
  historyNotice('');
  text('history-reading', '');
  for (const [id, value] of initialText) text(id, value);
  $('soc-ring').setAttribute('stroke-dashoffset', '100');
  for (const id of ['grid-flow', 'solar-flow', 'home-flow']) line(id, null);
  notice(''); setStatus('Connecting', true);
  $('login-panel').hidden = false; $('dashboard').hidden = true; $('logout').hidden = true;
  $('connect').disabled = false; text('connect', 'Connect ↗');
  $('api-token').value = ''; text('login-error', message);
  $('api-token').focus();
}
async function get(path, signal) {
  const response = await fetch(path, {
    headers: authRequired ? { Authorization: `Bearer ${token}` } : {}, cache: 'no-store',
    credentials: 'omit', redirect: 'error', signal,
  });
  if (!response.ok) {
    const error = new Error('Request failed'); error.status = response.status; throw error;
  }
  if (authRequired) storeToken(token);
  return response.json();
}
async function refresh() {
  if ((authRequired && !token) || request) return;
  const version = generation;
  request = new AbortController();
  const controller = request;
  const timeout = setTimeout(() => controller.abort(), 10_000);
  try {
    if (!deviceId) {
      const devices = await get('/web/devices', controller.signal);
      if (version !== generation) return;
      if (!Array.isArray(devices)) throw new Error('Invalid devices');
      if (!devices.length) {
        $('login-panel').hidden = true; $('dashboard').hidden = false; $('logout').hidden = !authRequired;
        setStatus('Waiting for device', true); notice('The bridge has not discovered an EP Cube yet. This page will retry automatically.');
        return;
      }
      if (typeof devices[0]?.id !== 'string') throw new Error('Invalid device ID');
      deviceId = devices[0].id;
    }
    const result = await get(`/web/devices/${encodeURIComponent(deviceId)}/state`, controller.signal);
    if (version !== generation) return;
    if (!result || result.device_id !== deviceId || !Number.isFinite(Date.parse(result.timestamp))) throw new Error('Invalid state');
    snapshot = result; networkFailed = false; failures = 0; lastFetch = Date.now();
    $('login-panel').hidden = true; $('dashboard').hidden = false; $('logout').hidden = !authRequired;
    render();
    void refreshHistory();
    void refreshWeather();
  } catch (error) {
    if (version !== generation) return;
    if (error.status === 401) { resetView('The API token was not accepted. Please try again.'); return; }
    failures += 1; networkFailed = true;
    if (history) historyNotice( 'Connection interrupted. Showing the last saved chart.');
    if (error.status === 404) deviceId = null;
    const message = error.status === 503 ? 'The bridge is connected, but no state sample is available yet. Retrying automatically.' : 'Cannot reach the bridge. Retrying automatically; any visible values are the last reading.';
    if (snapshot) { render(); notice(message); }
    else {
      $('login-panel').hidden = true; $('dashboard').hidden = false; $('logout').hidden = !authRequired;
      setStatus('Waiting for data', true); notice(message);
    }
  } finally {
    clearTimeout(timeout);
    if (version === generation) {
      request = null;
      $('connect').disabled = false; text('connect', 'Connect ↗');
      if ((!authRequired || token) && !document.hidden) timer = setTimeout(refresh, Math.min(60_000, 5000 * 2 ** Math.min(failures, 4)));
    }
  }
}
function weatherIcon(code, day) {
  if (code === 0) return day ? '☀️' : '🌙';
  if (code <= 2) return day ? '🌤️' : '☁️';
  if (code === 3) return '☁️';
  if (code === 45 || code === 48) return '🌫️';
  if ((code >= 71 && code <= 77) || code === 85 || code === 86) return '🌨️';
  if (code >= 95) return '⛈️';
  if (code >= 51 && code <= 82) return '🌧️';
  return '🌡️';
}
async function refreshWeather() {
  if (weatherRequest || Date.now() - weatherAttempt < 900_000) return;
  const version = generation;
  weatherAttempt = Date.now();
  const controller = new AbortController(); weatherRequest = controller;
  const timeout = setTimeout(() => controller.abort(), 20_000);
  try {
    const result = await get('/web/weather', controller.signal);
    if (version !== generation) return;
    $('weather').hidden = !result.configured;
    if (!result.configured) return;
    text('weather-icon', result.available ? weatherIcon(result.weather_code, result.is_day) : '—');
    text('weather-text', result.available ? `${result.temperature_c.toFixed(0)} °C${result.stale ? ' · stale' : ''}` : 'Weather unavailable');
    $('weather').title = result.available ? `${result.location} · ${elapsed(result.timestamp)} · Open-Meteo` : 'Open-Meteo · weather unavailable';
    $('weather').setAttribute('aria-label', $('weather').title + ' · ' + $('weather-text').textContent);
  } catch (error) {
    if (version !== generation) return;
    if (error.status === 401) { resetView('The API token was not accepted. Please try again.'); return; }
    text('weather-icon', '—'); text('weather-text', 'Weather unavailable');
  } finally {
    clearTimeout(timeout); if (version === generation) weatherRequest = null;
  }
}
async function refreshHistory() {
  if (!deviceId || historyRequest || Date.now() - historyAttempt < 60_000) return;
  const version = generation;
  historyAttempt = Date.now();
  const controller = new AbortController(); historyRequest = controller;
  const timeout = setTimeout(() => controller.abort(), 10_000);
  try {
    const result = await get(`/web/devices/${encodeURIComponent(deviceId)}/history`, controller.signal);
    if (version !== generation) return;
    if (!result || result.device_id !== deviceId || !Array.isArray(result.points)
        || !Number.isFinite(Date.parse(result.start)) || !Number.isFinite(Date.parse(result.end))
        || Date.parse(result.end) <= Date.parse(result.start) || !(result.gap_threshold_s > 0)) throw new Error('Invalid history');
    // Ignore malformed points; unavailable numeric channels remain null in the chart.
    result.points = result.points.filter(p => p && Number.isFinite(Date.parse(p.timestamp)));
    renderHistory(result); history = result;
    const home = result.today_home;
    text('daily-home', home && isNumber(home.energy_kwh) ? `≈ ${energy(home.energy_kwh)}` : '—');
    text('daily-home-note', home && isNumber(home.energy_kwh)
      ? `Estimate · ${home.coverage_percent}% of today recorded` : 'Estimate · waiting for history');
    const n = result.points.length;
    historyNotice( result.recording_available === false
      ? 'Recording interrupted. Showing saved readings.'
      : n ? '' : 'No history yet. Recording starts with the next successful poll.');
    $('history-inspect').hidden = !n;
    $('history-sample').max = String(Math.max(0, n - 1));
    $('history-sample').value = String(Math.max(0, n - 1));
    if (n) text('history-reading', readingLabel(result.points[n - 1], result.timezone));
  } catch (error) {
    if (version !== generation) return;
    if (error.status === 401) { resetView('The API token was not accepted. Please try again.'); return; }
    historyNotice( history ? 'History update unavailable. Showing the last saved chart.' : 'History unavailable. Retrying automatically.');
  } finally {
    clearTimeout(timeout);
    if (version === generation) historyRequest = null;
  }
}
$('history-sample').addEventListener('input', () => {
  const point = history?.points[Number($('history-sample').value)];
  if (point) text('history-reading', readingLabel(point, history.timezone));
});
$('login-form').addEventListener('submit', event => {
  event.preventDefault();
  const value = $('api-token').value.trim();
  if (!value) return;
  token = value; $('api-token').value = ''; text('login-error', '');
  $('connect').disabled = true; text('connect', 'Connecting…');
  void refresh();
});
$('logout').addEventListener('click', () => resetView());
document.addEventListener('visibilitychange', () => {
  if (timer) clearTimeout(timer);
  timer = null;
  if (!document.hidden && (!authRequired || token) && !request) void refresh();
});
setInterval(() => {
  if (!snapshot || document.hidden) return;
  text('updated', elapsed(snapshot.timestamp));
  // A suspended/disconnected tab must never keep its old live badge or animation.
  if (Date.now() - lastFetch > 30_000 && !networkFailed) {
    networkFailed = true; render(); notice('No recent response from the bridge. Showing the last reading.');
  }
}, 1000);

// The server embeds only this public boolean, never the configured API token.
if (!authRequired || token) {
  $('login-panel').hidden = true;
  $('dashboard').hidden = false;
  void refresh();
}

let resizeTimer;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (history) renderHistory(history); }, 150);
});

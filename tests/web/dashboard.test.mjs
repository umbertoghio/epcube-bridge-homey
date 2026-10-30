import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import * as format from '../../src/epcube_bridge/web/format.mjs';

const source = readFileSync(new URL('../../src/epcube_bridge/web/dashboard.js', import.meta.url), 'utf8').replace(/^import .*;\n/gm, '');
function harness({ storage = new Map(), disabled = false, queued = [], blocked = false, historyStatus = undefined } = {}) {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      textContent: '', value: '', hidden: false, disabled: false, attributes: {}, listeners: {},
      classList: { toggle() {} }, replaceChildren() {},
      setAttribute(key, value) { this.attributes[key] = value; },
      addEventListener(event, handler) { this.listeners[event] = handler; }, focus() {},
    });
    return elements.get(id);
  };
  const responses = [...queued]; const requests = []; const timers = new Map(); let timerId = 0;
  const context = vm.createContext({
    renderHistory() {}, readingLabel() { return 'Selected reading'; },
    localStorage: {
      getItem(key) { if (blocked) throw new Error('blocked'); return storage.get(key); },
      setItem(key, value) { if (blocked) throw new Error('blocked'); storage.set(key, value); },
      removeItem(key) { if (blocked) throw new Error('blocked'); storage.delete(key); },
    },
    ...format, document: { body: { dataset: { authRequired: String(!disabled) } }, getElementById: element, hidden: false, addEventListener() {} },
    window: { addEventListener() {} }, AbortController, Date, setInterval() {},
    setTimeout(fn, ms) { timers.set(++timerId, { fn, ms }); return timerId; },
    clearTimeout(id) { timers.delete(id); },
    fetch: async (path, options) => {
      requests.push({ path, options });
      const next = path === '/web/weather' ? {body:{configured:false}} : path.endsWith('/history') ? { status: historyStatus, body: {
        device_id: '123', start: new Date(Date.now() - 86400000).toISOString(), end: new Date().toISOString(),
        points: [], timezone: 'Europe/Rome', gap_threshold_s: 90, recording_available: true,
      } } : responses.shift();
      if (next instanceof Error) throw next;
      if (!next) throw new Error('Unexpected request');
      return { ok: next.status === undefined, status: next.status, json: async () => next.body };
    },
  });
  vm.runInContext(source, context);
  const flush = async () => { for (let i = 0; i < 32; i++) await Promise.resolve(); };
  return { storage, element, responses, requests, timers, flush,
    async login(token) {
      element('api-token').value = token;
      element('login-form').listeners.submit({ preventDefault() {} });
      await flush();
    },
    async retry() { const entry = [...timers].find(([, t]) => t.ms !== 10000); assert.ok(entry); timers.delete(entry[0]); entry[1].fn(); await flush(); },
  };
}
const sample = { device_id: '123', timestamp: new Date().toISOString(), available: true, soc: 11, battery_power: -1490, grid_power: -300, pv_power: 2220, home_power: 430, mode: 'self_consumption', daily_pv_energy: 7.3 };

test('authenticated readings and Lock erase values before a new empty account', async () => {
  const h = harness();
  h.responses.push({ body: [{ id: '123' }] }, { body: sample });
  await h.login('test-only-token');
  assert.equal(h.element('soc').textContent, '11%');
  assert.equal(h.element('battery-direction').textContent, 'Charging');
  assert.equal(h.element('grid-direction').textContent, 'Exporting');
  assert.equal(h.requests[1].options.headers.Authorization, 'Bearer test-only-token');
  assert.equal(h.element('api-token').value, '');
  assert.equal(h.element('solar-value').textContent, '2.22 kW');
  assert.equal(h.element('daily-solar').textContent, '7.30 kWh');
  h.element('logout').listeners.click();
  assert.equal(h.element('dashboard').hidden, true);
  assert.equal(h.element('soc').textContent, '');
  assert.equal(h.timers.size, 0);
  h.responses.push({ body: [] });
  await h.login('second-test-token');
  assert.equal(h.element('status').textContent, 'Waiting for device');
  assert.equal(h.element('soc').textContent, '');
  assert.equal(h.element('device-label').textContent, '');
});

test('401 returns to login without retaining a token or a retry loop', async () => {
  const h = harness(); h.responses.push({ status: 401 });
  await h.login('wrong-test-token');
  assert.equal(h.element('login-panel').hidden, false);
  assert.equal(h.element('dashboard').hidden, true);
  assert.match(h.element('login-error').textContent, /not accepted/);
  assert.equal(h.timers.size, 0);
});

test('network outage marks old data stale, backs off and recovers without cloud calls', async () => {
  const h = harness(); h.responses.push({ body: [{ id: '123' }] }, { body: sample });
  await h.login('test-only-token');
  h.responses.push(new Error('network unavailable'));
  await h.retry();
  assert.equal(h.element('status').textContent, 'Last reading');
  assert.equal(h.element('soc').textContent, '11%');
  assert.equal([...h.timers.values()][0].ms, 10000);
  // The only queued timer is the retry, even when its delay matches the request timeout.
  const [id, retry] = [...h.timers][0]; h.timers.delete(id);
  h.responses.push({ body: { ...sample, soc: 12 } }); retry.fn(); await h.flush();
  assert.equal(h.element('status').textContent, 'Live data');
  assert.equal(h.element('soc').textContent, '12%');
  assert.ok(h.requests.every(r => r.path === '/web/devices' || r.path === '/web/devices/123/state' || r.path === '/web/devices/123/history' || r.path === '/web/weather'));
});


test('accepted tokens survive reload, and Lock removes the persisted token', async () => {
  const h = harness(); h.responses.push({ body: [] });
  await h.login('remember-me');
  assert.equal(h.storage.get('epcube.apiToken'), 'remember-me');
  const reload = harness({ storage: h.storage, queued: [{ body: [] }] });
  await reload.flush();
  assert.equal(reload.requests[0].options.headers.Authorization, 'Bearer remember-me');
  assert.equal(reload.element('login-panel').hidden, true);
  reload.element('logout').listeners.click();
  assert.equal(h.storage.has('epcube.apiToken'), false);
});

test('rejected saved tokens are removed; blocked storage does not prevent manual login', async () => {
  const storage = new Map([['epcube.apiToken', 'expired']]);
  const h = harness({ storage, queued: [{ status: 401 }] });
  await h.flush();
  assert.equal(storage.size, 0);
  assert.equal(h.element('login-panel').hidden, false);
  const blocked = harness({ blocked: true });
  blocked.responses.push({ body: [] });
  await blocked.login('tab-only');
  assert.equal(blocked.element('login-panel').hidden, true);
});

test('auth-disabled dashboard starts automatically and never sends a saved credential', async () => {
  const h = harness({ disabled: true, storage: new Map([['epcube.apiToken', 'saved']]),
    queued: [{ body: [{ id: '123' }] }, { body: sample }] });
  await h.flush();
  assert.equal(h.element('soc').textContent, '11%');
  assert.equal(h.element('login-panel').hidden, true);
  assert.equal(h.element('logout').hidden, true);
  assert.ok(h.requests.every(r => !('Authorization' in r.options.headers)));
  h.responses.push({ body: sample }); await h.retry();
  assert.equal(h.requests.length, 5);
});


test('history failure does not hide live readings; auth failure locks the dashboard', async () => {
  const h = harness({historyStatus: 503});
  h.responses.push({body:[{id:'123'}]}, {body:sample}); await h.login('test-only');
  assert.equal(h.element('status').textContent, 'Live data');
  assert.match(h.element('history-status').textContent, /History unavailable/);
  h.responses.push({body:sample}); await h.retry();
  assert.equal(h.requests.filter(r => r.path.endsWith('/history')).length, 1);
  const rejected = harness({historyStatus:401});
  rejected.responses.push({body:[{id:'123'}]}, {body:sample}); await rejected.login('test-only');
  await rejected.flush();
  assert.equal(rejected.element('dashboard').hidden, true);
  assert.equal(rejected.storage.size, 0);
});

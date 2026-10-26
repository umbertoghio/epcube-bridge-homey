import assert from 'node:assert/strict';
import { test } from 'node:test';
import { capabilityValues, crosses, parseSnapshot, STALE_MS, type Snapshot } from '../lib/model';
import { parseSettings, Registry, retryDelay } from '../lib/broker';

const now = Date.parse('2026-10-26T12:00:00Z');
export const sample = {
  device_id: '123', timestamp: new Date(now).toISOString(), mode: 'self_consumption',
  soc: 64, pv_power: 1200, grid_power: -200, battery_power: 800, home_power: 1800,
  reserve_soc: 20, daily_pv_energy: 3.2, daily_grid_import_energy: 2.1,
};
const bytes = (value: unknown) => Buffer.from(JSON.stringify(value));
const parsed = () => parseSnapshot(bytes(sample), '123', now)!;

test('bridge snapshot maps signs, units, nulls and read-only enum', () => {
  const state = parsed();
  const values = capabilityValues(state);
  assert.equal(values.measure_battery, 64);
  assert.equal(values.measure_power, -800);
  assert.equal(values['measure_power.grid'], -200);
  assert.equal(values['measure_power.pv'], 1200);
  assert.equal(values['measure_power.home'], 1800);
  assert.equal(values['meter_power.daily_pv'], 3.2);
  assert.equal(values['meter_power.daily_ev'], null);
  assert.equal(values.epcube_mode, 'self_consumption');
  assert.equal(capabilityValues({ ...state, battery_power: -300 }).measure_power, 300);
});

test('malformed, oversized, wrong-device, invalid timestamp and invalid numbers are ignored', () => {
  for (const payload of [Buffer.from('{'), Buffer.alloc(65537), bytes([]), bytes(null),
    bytes({ ...sample, device_id: 'elsewhere' }), bytes({ ...sample, soc: 101 }),
    bytes({ ...sample, soc: '64' }), bytes({ ...sample, pv_power: {} }),
    bytes({ ...sample, timestamp: 'invalid' }), bytes({ ...sample, mode: 'evil' }),
    bytes({ ...sample, timestamp: '2026-10-26T12:30:00Z' }),
    bytes({ ...sample, daily_pv_energy: -3 }),
  ]) assert.equal(parseSnapshot(payload, '123', now), null);
});

test('discovery is availability-based, retained ordering independent, duplicates and old states safe', () => {
  const registry = new Registry('house/epcube', () => now);
  registry.connection(true);
  registry.ingest('house/epcube/123/state', bytes(sample));
  assert.deepEqual(registry.list(), []);
  registry.ingest('house/epcube/123/availability', Buffer.from('online'));
  assert.equal(registry.reason('123'), null);
  assert.deepEqual(registry.list(), [{ name: 'EP Cube 123', data: { id: '123' } }]);
  registry.ingest('house/epcube/123/state', bytes({ ...sample, timestamp: '2026-10-26T11:59:00Z', soc: 2 }));
  registry.ingest('house/epcube/123/state', bytes({ ...sample, soc: 4 }));
  assert.equal(registry.records.get('123')?.snapshot?.soc, 64);
  registry.ingest('house/epcube/123/soc', Buffer.from('0'));
  registry.ingest('other/123/availability', Buffer.from('online'));
  assert.equal(registry.records.size, 1);
  registry.ingest('house/epcube/123/availability', Buffer.from('offline'));
  assert.match(registry.reason('123')!, /offline/);
  registry.connection(false);
  registry.connection(true);
  assert.match(registry.reason('123')!, /availability/);
  registry.ingest('house/epcube/123/availability', Buffer.from('online'));
  assert.match(registry.reason('123')!, /state/);
});

test('arrival timeout and stale retained timestamps each make device unavailable', () => {
  let clock = now;
  const registry = new Registry('epcube', () => clock);
  registry.connection(true);
  registry.ingest('epcube/123/availability', Buffer.from('online'));
  registry.ingest('epcube/123/state', bytes(sample));
  assert.equal(registry.reason('123'), null);
  clock += STALE_MS;
  assert.match(registry.reason('123')!, /10 minutes/);
  registry.ingest('epcube/123/state', bytes(sample));
  assert.match(registry.reason('123')!, /10 minutes/);
  registry.ingest('epcube/123/state', bytes({ ...sample, timestamp: new Date(clock).toISOString() }));
  assert.equal(registry.reason('123'), null);
});

test('crossing thresholds handle equality, signs, repeated readings and nulls', () => {
  const before = parsed();
  const change = (fields: Partial<Snapshot>) => ({ ...before, ...fields });
  assert(crosses('soc_below', before, change({ soc: 59 }), 60));
  assert(!crosses('soc_below', before, change({ soc: 60 }), 60));
  assert(!crosses('soc_below', change({ soc: 59 }), change({ soc: 58 }), 60));
  assert(crosses('soc_above', change({ soc: 60 }), before, 60));
  assert(crosses('grid_import_above', before, change({ grid_power: 501 }), 500));
  assert(crosses('grid_export_above', before, change({ grid_power: -501 }), 500));
  assert(!crosses('grid_export_above', before, change({ grid_power: 501 }), 500));
  assert(!crosses('soc_above', before, change({ soc: null }), 50));
  assert(!crosses('soc_above', before, change({ soc: 80 }), '70'));
  assert(crosses('mode_changed', before, change({ mode: 'backup' }), undefined));
});

test('settings validation and bounded exponential reconnect delay', () => {
  const valid = { host: 'broker.lan', port: 8883, tls: true, username: 'user', password: 'secret', baseTopic: '/epcube/' };
  assert.equal(parseSettings(valid)?.baseTopic, 'epcube');
  for (const invalid of [{ host: 'mqtt://broker' }, { port: -1 }, { tls: 'yes' }, { baseTopic: '#' }]) {
    assert.equal(parseSettings({ ...valid, ...invalid }), null);
  }
  assert.equal(retryDelay(0, () => 0), 800);
  assert(retryDelay(3, () => 1) > retryDelay(2, () => 1));
  assert.equal(retryDelay(50, () => 1), 300_000);
});

import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { createRequire } from 'node:module';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import { Registry } from '../lib/broker';

// Homey supplies this module at runtime. Never load the CLI package in tests.
const require = createRequire(import.meta.url);
class Base extends EventEmitter {
  homey: any;
  values: Record<string, unknown> = {};
  available = false;
  errors: string[] = [];
  getData() { return { id: '123' }; }
  getCapabilityValue(id: string) { return this.values[id]; }
  async setCapabilityValue(id: string, value: unknown) { this.values[id] = value; }
  async setUnavailable(_message: string) { this.available = false; }
  async setAvailable() { this.available = true; }
  log(_message: string) {}
  error(message: string) { this.errors.push(message); }
}
require.cache[require.resolve('homey')] = { exports: { App: Base, Device: Base, Driver: Base } } as NodeModule;
const App = require('../app');
const Device = require('../drivers/epcube/device');
const Driver = require('../drivers/epcube/driver');
const flush = async () => { for (let n = 0; n < 15; n++) await new Promise(resolve => setImmediate(resolve)); };

test('Homey lifecycle: capabilities, pairing, crossing Flows, conditions, offline/reconnect, cleanup', async () => {
  const triggers = new Map<string, any>();
  const conditions = new Map<string, any>();
  const fired: string[] = [];
  let intervalCleared = false;
  const flow = {
    getDeviceTriggerCard(id: string) {
      if (!triggers.has(id)) triggers.set(id, {
        registerRunListener(fn: Function) { this.listener = fn; },
        async trigger(device: unknown, tokens: unknown, state: unknown) {
          if (await this.listener({ device, threshold: id.startsWith('soc') ? 60 : 500 }, state)) fired.push(id);
        },
      });
      return triggers.get(id);
    },
    getConditionCard(id: string) {
      if (!conditions.has(id)) conditions.set(id, { registerRunListener(fn: Function) { this.listener = fn; } });
      return conditions.get(id);
    },
  };
  const settings = Object.assign(new EventEmitter(), { get: () => null });
  const homey: any = { flow, settings,
    setInterval: () => 1, clearInterval: () => { intervalCleared = true; },
    setTimeout: (fn: Function) => { fn(); return 1; },
  };
  const app = new App(); app.homey = homey; homey.app = app;
  await app.onInit();
  let clock = Date.now();
  const broker = new Registry('epcube', () => clock);
  app.broker = broker;
  broker.on('update', () => app.notify());
  broker.connection(true);
  const device = new Device(); device.homey = homey;
  await device.onInit(); await flush();
  assert(!device.available);
  let state = { device_id: '123', timestamp: new Date(clock).toISOString(), mode: 'self_consumption', soc: 64,
    battery_power: 800, pv_power: 1200, grid_power: -200, home_power: 1800, daily_pv_energy: 3.2 };
  const send = () => broker.ingest('epcube/123/state', Buffer.from(JSON.stringify(state)));
  send(); broker.ingest('epcube/123/availability', Buffer.from('online'));
  await flush();
  assert(device.available);
  assert.equal(device.values.measure_power, -800);
  assert.equal(device.values['meter_power.daily_pv'], 3.2);
  assert.deepEqual(fired, []);
  const driver = new Driver(); driver.homey = homey;
  assert.deepEqual(await driver.onPairListDevices(), [{ name: 'EP Cube 123', data: { id: '123' } }]);
  clock += 1000;
  state = { ...state, timestamp: new Date(clock).toISOString(), soc: 59, mode: 'backup', grid_power: 600 };
  send(); await flush();
  assert.deepEqual(fired, ['soc_below', 'mode_changed', 'grid_import_above']);
  send(); await flush();
  assert.equal(fired.length, 3);
  assert.equal(await conditions.get('soc_is_above').listener({ device, threshold: 58 }), true);
  assert.equal(await conditions.get('mode_is').listener({ device, mode: 'backup' }), true);
  broker.ingest('epcube/123/availability', Buffer.from('offline')); await flush();
  assert(!device.available);
  assert.equal(await conditions.get('soc_is_above').listener({ device, threshold: 10 }), false);
  clock += 1000; state = { ...state, timestamp: new Date(clock).toISOString(), soc: 80 };
  send(); broker.ingest('epcube/123/availability', Buffer.from('online')); await flush();
  assert(device.available);
  assert.equal(fired.length, 3); // Recovery establishes a new baseline.
  // Even an offline/online gap in the same event-loop tick must reset the baseline.
  broker.ingest('epcube/123/availability', Buffer.from('offline'));
  clock += 1000; state = { ...state, timestamp: new Date(clock).toISOString(), soc: 40 };
  send(); broker.ingest('epcube/123/availability', Buffer.from('online')); await flush();
  assert.equal(fired.length, 3);
  await device.onDeleted();
  assert(intervalCleared);
  assert.equal(app.watchers.size, 0);
});

test('manifest has required read-only capabilities, home battery Energy and no cloud/control actions', () => {
  const manifest = JSON.parse(readFileSync(new URL('../app.json', import.meta.url), 'utf8'));
  const driver = manifest.drivers[0];
  assert.equal(manifest.sdk, 3);
  assert.deepEqual(manifest.platforms, ['local']);
  assert.equal(driver.class, 'battery');
  assert.deepEqual(driver.energy, { homeBattery: true });
  for (const name of ['measure_battery', 'measure_power', 'measure_power.grid', 'measure_power.pv',
    'measure_power.home', 'meter_power.daily_grid_import', 'meter_power.daily_grid_export', 'epcube_mode']) {
    assert(driver.capabilities.includes(name));
  }
  assert.equal(driver.capabilities.includes('meter_power'), false); // Never pass daily resets as lifetime battery energy.
  assert.equal(manifest.capabilities.epcube_mode.setable, false);
  assert.equal(manifest.flow.actions, undefined);
  assert.equal(manifest.flow.triggers.length, 5);
  assert.equal(manifest.flow.conditions.length, 2);
});

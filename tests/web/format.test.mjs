import assert from 'node:assert/strict';
import { test } from 'node:test';
import { power, energy, gridDirection, batteryDirection, elapsed } from '../../src/epcube_bridge/web/format.mjs';
test('display preserves meaning without exposing signed magnitudes as consumption', () => {
  assert.equal(power(-1490), '1.49 kW');
  assert.equal(batteryDirection(-1490), 'Charging');
  assert.equal(batteryDirection(800), 'Discharging');
  assert.equal(gridDirection(-200), 'Exporting');
  assert.equal(gridDirection(0), 'No exchange');
  assert.equal(energy(3.2), '3.20 kWh');
  assert.equal(power(null), '—');
  assert.equal(energy(NaN), '—');
  assert.equal(gridDirection(null), 'Unavailable');
});
test('sample age uses the reading timestamp, not a heartbeat or request time', () => {
  assert.equal(elapsed('2026-10-26T12:00:00Z', Date.parse('2026-10-26T12:11:00Z')), 'Updated 11m ago');
  assert.equal(elapsed('not a date'), 'Sample time unknown');
});

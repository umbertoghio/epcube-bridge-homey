import assert from 'node:assert/strict';
import test from 'node:test';
import { segments, powerDomain, readingLabel } from '../../src/epcube_bridge/web/history.mjs';
const start = Date.parse('2026-10-30T00:00:00Z');
const point = (seconds, soc) => ({timestamp:new Date(start + seconds * 1000).toISOString(), soc});
test('charts break at nulls, missing intervals, out-of-order samples, and clip to 24h', () => {
  const points = [point(-30,5), point(0,10), point(30,20), point(60,null), point(90,25), point(300,30), point(330,35), point(400,40)];
  assert.deepEqual(segments(points,'soc',90,start,start+330000).map(s => s.map(p => p[1])), [[10,20],[25],[30,35]]);
  assert.equal(segments([point(20,20),point(10,10)],'soc',90,start,start+30000).length, 2);
});
test('power scale uses W to kW, preserves signed readings and has a nonzero empty domain', () => {
  assert.deepEqual(powerDomain([]), [0,1]);
  assert.deepEqual(powerDomain([{pv_power:2410,home_power:5100},{pv_power:null,home_power:-120}]), [-1,6]);
  assert.match(readingLabel({...point(0,31),pv_power:2410,home_power:null},'Europe/Rome'), /2.41 kW.*Home —/);
});

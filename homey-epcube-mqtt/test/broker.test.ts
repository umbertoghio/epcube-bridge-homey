import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { test } from 'node:test';
import { type MqttClient, type IClientOptions } from 'mqtt';
import { Broker, type BrokerSettings } from '../lib/broker';

const settings: BrokerSettings = { host: 'localhost', port: 8883, tls: true, username: 'private-user', password: 'private-password', baseTopic: 'epcube' };
class FakeClient extends EventEmitter {
  ended = false;
  topics: string[] = [];
  deny = false;
  subscribe(topics: string[], _options: unknown, cb: (err: null, grants: unknown[]) => void): void {
    this.topics = topics;
    cb(null, topics.map(topic => ({ topic, qos: this.deny ? 128 : 1 })));
  }
  end(): void { this.ended = true; }
}

test('connects read-only over verified TLS, resubscribes, and backs off after failures', t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const clients: FakeClient[] = [];
  const options: IClientOptions[] = [];
  const logs: string[] = [];
  const broker = new Broker(settings, message => logs.push(message), config => {
    options.push(config);
    const client = new FakeClient(); clients.push(client);
    return client as unknown as MqttClient;
  });
  broker.start();
  clients[0].emit('connect');
  assert(broker.connected);
  assert.deepEqual(clients[0].topics, ['epcube/+/availability', 'epcube/+/state']);
  assert.equal(options[0].protocol, 'mqtts');
  assert.equal(options[0].rejectUnauthorized, true);
  assert.equal(options[0].reconnectPeriod, 0);
  clients[0].emit('message', 'epcube/123/availability', Buffer.from('online'));
  assert.equal(broker.list().length, 1);
  clients[0].emit('message', 'epcube/123/state', Buffer.from('{bad json'));
  clients[0].emit('error', new Error('private-user private-password'));
  clients[0].emit('close');
  assert(!broker.connected);
  assert(clients[0].ended);
  t.mock.timers.tick(1000);
  assert.equal(clients.length, 2);
  clients[1].emit('error', new Error('private-password'));
  t.mock.timers.tick(1000);
  assert.equal(clients.length, 2);
  t.mock.timers.tick(1000);
  assert.equal(clients.length, 3);
  clients[2].emit('connect');
  assert.deepEqual(clients[2].topics, clients[0].topics);
  broker.stop();
  assert(clients[2].ended);
  t.mock.timers.tick(300000);
  assert.equal(clients.length, 3);
  assert(!logs.join().includes('private'));
});

test('denied or missing subscription acknowledgement does not report connected', t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const fake = new FakeClient(); fake.deny = true;
  const broker = new Broker(settings, () => {}, () => fake as unknown as MqttClient);
  broker.start(); fake.emit('connect');
  assert(!broker.connected);
  assert(fake.ended);
  broker.stop();
  const silent = new FakeClient(); silent.subscribe = () => {};
  const waiting = new Broker(settings, () => {}, () => silent as unknown as MqttClient);
  waiting.start(); silent.emit('connect');
  t.mock.timers.tick(15000);
  assert(!waiting.connected);
  assert(silent.ended);
  waiting.stop();
});

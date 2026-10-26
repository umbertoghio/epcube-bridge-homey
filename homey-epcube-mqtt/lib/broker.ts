import { EventEmitter } from 'node:events';
import { randomUUID } from 'node:crypto';
import mqtt, { type MqttClient, type IClientOptions } from 'mqtt';
import { parseSnapshot, type Reading, unavailableReason } from './model';

export interface BrokerSettings {
  host: string; port: number; tls: boolean; username: string; password: string; baseTopic: string;
}
export function parseSettings(raw: unknown): BrokerSettings | null {
  if (!raw || typeof raw !== 'object') return null;
  const data = raw as Record<string, unknown>;
  if (typeof data.host !== 'string' || !data.host.trim() || /[\s/@?#]/.test(data.host)) return null;
  if (data.host.includes('://')) return null;
  if (typeof data.port !== 'number' || !Number.isInteger(data.port) || data.port < 1 || data.port > 65535) return null;
  if (typeof data.tls !== 'boolean' || typeof data.username !== 'string' || typeof data.password !== 'string') return null;
  if (typeof data.baseTopic !== 'string') return null;
  const baseTopic = data.baseTopic.replace(/^\/+|\/+$/g, '');
  if (!baseTopic || /[+#\0]/.test(baseTopic) || baseTopic.length > 200) return null;
  return { host: data.host.trim(), port: data.port, tls: data.tls, username: data.username, password: data.password, baseTopic };
}
export function retryDelay(attempt: number, random = Math.random): number {
  return Math.min(300_000, 1000 * 2 ** Math.min(Math.max(attempt, 0), 9)) * (0.8 + random() * 0.2);
}

// A bounded cache shared by pairing and devices. It never subscribes to scalar topics.
export class Registry extends EventEmitter {
  readonly records = new Map<string, Reading>();
  connected = false;
  generation = 0;
  constructor(public baseTopic: string, private readonly now = Date.now) { super(); }
  connection(connected: boolean): void {
    this.connected = connected;
    if (!connected) {
      this.generation += 1;
      for (const reading of this.records.values()) {
        reading.availability = 'unknown';
        reading.snapshot = undefined;
        reading.receivedAt = undefined;
      }
    }
    this.emit('update');
  }
  ingest(topic: string, payload: Buffer): void {
    const prefix = `${this.baseTopic}/`;
    if (!topic.startsWith(prefix)) return;
    const segments = topic.slice(prefix.length).split('/');
    if (segments.length !== 2) return;
    const [id, leaf] = segments;
    if (!id || id.length > 128 || /[+#\0]/.test(id) || !['availability', 'state'].includes(leaf)) return;
    if (!this.records.has(id) && this.records.size >= 100) return;
    const reading = this.records.get(id) ?? { id, discovered: false, availability: 'unknown' } as Reading;
    if (leaf === 'availability') {
      if (payload.length > 16) return;
      const status = payload.toString('utf8');
      if (status !== 'online' && status !== 'offline') return;
      if (status === 'offline' && reading.availability !== 'offline') this.generation += 1;
      reading.discovered = true;
      reading.availability = status;
    } else {
      const snapshot = parseSnapshot(payload, id, this.now());
      if (!snapshot) return;
      if (reading.snapshot && Date.parse(snapshot.timestamp) < Date.parse(reading.snapshot.timestamp)) return;
      // QoS retransmits and heartbeat copies may repeat an identical timestamp.
      if (!reading.snapshot || Date.parse(snapshot.timestamp) > Date.parse(reading.snapshot.timestamp)) {
        reading.snapshot = snapshot;
      }
      reading.receivedAt = this.now();
    }
    this.records.set(id, reading);
    this.emit('update', id);
  }
  list(): { name: string; data: { id: string } }[] {
    return [...this.records.values()].filter(r => r.discovered)
      .map(r => ({ name: `EP Cube ${r.id}`, data: { id: r.id } }));
  }
  reason(id: string): string | null { return unavailableReason(this.records.get(id), this.connected, this.now()); }
}

export type Connector = (options: IClientOptions) => MqttClient;
export class Broker extends Registry {
  private client?: MqttClient;
  private reconnectTimer?: ReturnType<typeof setTimeout>;
  private attempt = 0;
  private subscribeTimer?: ReturnType<typeof setTimeout>;
  private stopped = false;
  private readonly clientId = `homey-epcube-${randomUUID()}`;
  constructor(private readonly settings: BrokerSettings, private readonly logSafe: (message: string) => void,
    private readonly connector: Connector = options => mqtt.connect(options)) {
    super(settings.baseTopic);
  }
  start(): void { this.stopped = false; this.connect(); }
  stop(): void {
    this.stopped = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = undefined;
    if (this.subscribeTimer) clearTimeout(this.subscribeTimer);
    this.subscribeTimer = undefined;
    const client = this.client;
    this.client = undefined;
    client?.end(true);
    this.connection(false);
  }
  private connect(): void {
    if (this.stopped) return;
    let client: MqttClient;
    try {
      client = this.connector({
        host: this.settings.host, port: this.settings.port,
        protocol: this.settings.tls ? 'mqtts' : 'mqtt',
        username: this.settings.username || undefined, password: this.settings.password || undefined,
        rejectUnauthorized: true, reconnectPeriod: 0, resubscribe: false,
        connectTimeout: 15_000, keepalive: 30, clean: true, clientId: this.clientId,
      });
    } catch { this.retry(); return; }
    this.client = client;
    let connectedAt: number | undefined;
    const failed = () => {
      if (this.client !== client || this.stopped) return;
      this.client = undefined;
      if (this.subscribeTimer) clearTimeout(this.subscribeTimer);
      this.subscribeTimer = undefined;
      if (connectedAt !== undefined && Date.now() - connectedAt >= 60_000) this.attempt = 0;
      client.end(true);
      this.connection(false);
      this.retry();
    };
    client.on('error', failed); // Never log raw broker errors: they may contain credentials.
    client.on('close', failed);
    client.on('offline', failed);
    client.on('connect', () => {
      if (this.client !== client || this.stopped) return;
      this.subscribeTimer = setTimeout(failed, 15_000);
      client.subscribe([`${this.baseTopic}/+/availability`, `${this.baseTopic}/+/state`], { qos: 1 }, (error, grants) => {
        if (this.client !== client || this.stopped) return;
        if (error || !grants || grants.some(grant => grant.qos === 128)) { failed(); return; }
        if (this.subscribeTimer) clearTimeout(this.subscribeTimer);
        this.subscribeTimer = undefined;
        connectedAt = Date.now();
        this.connection(true);
      });
    });
    client.on('message', (topic, payload) => {
      if (this.client !== client || this.stopped) return;
      try { this.ingest(topic, payload); } catch { this.logSafe('Ignored invalid MQTT message'); }
    });
  }
  private retry(): void {
    if (this.stopped || this.reconnectTimer) return;
    const delay = retryDelay(this.attempt++);
    this.logSafe('MQTT disconnected; reconnect scheduled');
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = undefined;
      this.connect();
    }, delay);
  }
}

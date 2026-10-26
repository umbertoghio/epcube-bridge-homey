import Homey from 'homey';
import type EPCubeApp from '../../app';
import type { Broker } from '../../lib/broker';
import { capabilityValues, type Snapshot, type Trigger } from '../../lib/model';

class EPCubeDevice extends Homey.Device {
  private unsubscribe?: () => void;
  private timer?: ReturnType<typeof setInterval>;
  private previous?: Snapshot;
  private previousBroker?: Broker;
  private previousGeneration = -1;
  private running = false;
  private pending = false;
  private destroyed = false;
  private availableState?: string | null;

  async onInit(): Promise<void> {
    await this.setUnavailable('Waiting for MQTT state');
    const app = this.homey.app as EPCubeApp;
    this.unsubscribe = app.watch(() => this.schedule());
    this.timer = this.homey.setInterval(() => this.schedule(), 15_000);
    this.schedule();
  }
  currentSnapshot(): Snapshot | undefined {
    const broker = (this.homey.app as EPCubeApp).broker;
    const id = this.getData().id;
    return broker && broker.reason(id) === null ? broker.records.get(id)?.snapshot : undefined;
  }
  private schedule(): void {
    if (this.destroyed) return;
    this.pending = true;
    if (this.running) return;
    this.running = true;
    void this.drain().catch(() => this.error('Could not update EP Cube capabilities')).finally(() => {
      this.running = false;
      if (this.pending && !this.destroyed) this.schedule();
    });
  }
  private async drain(): Promise<void> {
    while (this.pending && !this.destroyed) {
      this.pending = false;
      const broker = (this.homey.app as EPCubeApp).broker;
      const reason = broker?.reason(this.getData().id) ?? (broker ? null : 'MQTT is not configured');
      const snapshot = this.currentSnapshot();
      if (reason !== null || !snapshot) {
        this.previous = undefined; // No threshold events across a gap/reconnection.
        const message = reason ?? 'Waiting for MQTT state';
        if (this.availableState !== message) {
          await this.setUnavailable(message);
          this.availableState = message;
        }
        continue;
      }
      const generation = broker!.generation;
      if (broker !== this.previousBroker || generation !== this.previousGeneration) this.previous = undefined;
      this.previousBroker = broker;
      this.previousGeneration = generation;
      const before = this.previous;
      if (!before || snapshot.timestamp !== before.timestamp) {
        for (const [capability, value] of Object.entries(capabilityValues(snapshot))) {
          if (this.destroyed) return;
          if (this.getCapabilityValue(capability) !== value) await this.setCapabilityValue(capability, value);
        }
        this.previous = snapshot;
      }
      // An offline event may have arrived while Homey capability writes were pending.
      if (this.currentSnapshot() !== snapshot || broker!.generation !== generation || this.destroyed) {
        this.previous = undefined;
        this.pending = true;
        continue;
      }
      if (this.availableState !== null) { await this.setAvailable(); this.availableState = null; }
      if (before && before.timestamp !== snapshot.timestamp) await this.triggerChanges(before, snapshot);
    }
  }
  private async triggerChanges(before: Snapshot, after: Snapshot): Promise<void> {
    const trigger = async (id: Trigger, tokens: Record<string, number | string>) => {
      try {
        await this.homey.flow.getDeviceTriggerCard(id).trigger(this, tokens, { before, after });
      } catch { this.error('EP Cube Flow trigger failed'); }
    };
    if (before.soc !== null && after.soc !== null && before.soc !== after.soc) {
      await trigger(after.soc < before.soc ? 'soc_below' : 'soc_above', { soc: after.soc });
    }
    if (before.mode !== after.mode) await trigger('mode_changed', { mode: after.mode, previous_mode: before.mode });
    if (before.grid_power !== null && after.grid_power !== null && before.grid_power !== after.grid_power) {
      await trigger('grid_import_above', { power: Math.max(0, after.grid_power) });
      await trigger('grid_export_above', { power: Math.max(0, -after.grid_power) });
    }
  }
  private cleanup(): void {
    this.destroyed = true;
    this.unsubscribe?.();
    if (this.timer) this.homey.clearInterval(this.timer);
  }
  async onDeleted(): Promise<void> { this.cleanup(); }
  async onUninit(): Promise<void> { this.cleanup(); }
}
export = EPCubeDevice;

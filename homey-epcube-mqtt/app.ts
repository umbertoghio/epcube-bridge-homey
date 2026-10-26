import Homey from 'homey';
import { Broker, parseSettings } from './lib/broker';
import { crosses, type Snapshot, type Trigger } from './lib/model';

interface FlowDevice extends Homey.Device { currentSnapshot(): Snapshot | undefined }
class EPCubeApp extends Homey.App {
  broker?: Broker;
  private readonly watchers = new Set<() => void>();
  private readonly settingsChanged = (key: string) => { if (key === 'broker') this.configureBroker(); };

  async onInit(): Promise<void> {
    this.registerFlows();
    this.homey.settings.on('set', this.settingsChanged);
    this.homey.settings.on('unset', this.settingsChanged);
    this.configureBroker();
  }
  watch(listener: () => void): () => void {
    this.watchers.add(listener);
    return () => this.watchers.delete(listener);
  }
  private notify = (): void => {
    for (const listener of this.watchers) {
      try { listener(); } catch { this.error('Device refresh failed'); }
    }
  };
  private configureBroker(): void {
    this.broker?.stop();
    this.broker?.removeAllListeners();
    this.broker = undefined;
    const settings = parseSettings(this.homey.settings.get('broker'));
    if (settings) {
      this.broker = new Broker(settings, message => this.log(message));
      this.broker.on('update', this.notify);
      this.broker.start();
    } else { this.log('Set valid MQTT broker details in app settings'); }
    this.notify();
  }
  private registerFlows(): void {
    const triggers: Trigger[] = ['soc_below', 'soc_above', 'mode_changed', 'grid_import_above', 'grid_export_above'];
    for (const id of triggers) {
      this.homey.flow.getDeviceTriggerCard(id).registerRunListener(async (args, state) => {
        if (!state?.before || !state?.after) return false;
        return crosses(id, state.before, state.after, args.threshold);
      });
    }
    this.homey.flow.getConditionCard('soc_is_above').registerRunListener(async args => {
      const value = (args.device as FlowDevice).currentSnapshot()?.soc;
      return typeof value === 'number' && typeof args.threshold === 'number'
        && Number.isFinite(args.threshold) && args.threshold >= 0 && args.threshold <= 100 && value > args.threshold;
    });
    this.homey.flow.getConditionCard('mode_is').registerRunListener(async args => {
      return (args.device as FlowDevice).currentSnapshot()?.mode === args.mode;
    });
  }
  async onUninit(): Promise<void> {
    this.homey.settings.removeListener('set', this.settingsChanged);
    this.homey.settings.removeListener('unset', this.settingsChanged);
    this.broker?.stop();
    this.broker?.removeAllListeners();
    this.watchers.clear();
  }
}
export = EPCubeApp;

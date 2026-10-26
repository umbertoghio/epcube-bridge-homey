import Homey from 'homey';
import type EPCubeApp from '../../app';

class EPCubeDriver extends Homey.Driver {
  async onPairListDevices(): Promise<{ name: string; data: { id: string } }[]> {
    const app = this.homey.app as EPCubeApp;
    if (!app.broker) throw new Error('Configure the MQTT broker in EP Cube MQTT app settings first.');
    // Retained discovery usually arrives immediately; allow a short subscription window.
    await new Promise<void>(resolve => this.homey.setTimeout(resolve, 1500));
    if (!app.broker?.connected) throw new Error('MQTT is not connected. Check the app settings and broker permissions.');
    return app.broker.list();
  }
}
export = EPCubeDriver;

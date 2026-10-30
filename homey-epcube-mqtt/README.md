# EP Cube MQTT for Homey Pro

A read-only Homey Apps SDK v3 app written in TypeScript. It subscribes to the MQTT broker used by [epcube-bridge](../README.md); it does not contact the EP Cube cloud, use its credentials, call the bridge REST API, or publish control messages.

## Install

Use Node.js 22 and a Homey Pro running Homey **12.4.5 or later**. The CLI and SDK typings are pinned in the local development dependencies; no global CLI installation is required.

```sh
cd homey-epcube-mqtt
npm ci
npm run typecheck
npm test
npm run validate
npx homey login
npx homey select
npx homey app install
```

If you already have the Homey CLI installed, `homey app install` from this directory is equivalent. Installation compiles TypeScript to `.homeybuild`. This is a local/development installation, not an App Store publication. No installation on physical Homey hardware was performed during development.

1. Start epcube-bridge and verify it is publishing retained state and availability.
2. In Homey, open **Apps → EP Cube MQTT → Settings**.
3. Enter broker host (without a URL scheme), port, TLS, username, password and base topic. The base topic defaults to `epcube`; port defaults to 1883. For TLS, set the broker's TLS port explicitly (usually 8883).
4. Save. Settings take effect immediately and the shared broker connection restarts.
5. Add a device, choose **EP Cube MQTT → EP Cube battery**, then select the discovered ID.

The broker must be reachable from **Homey**, not just from the bridge's container. Use its LAN hostname/address, not `localhost` or Docker's internal service name unless those resolve correctly from Homey.

## MQTT discovery and availability

The app subscribes at QoS 1 to exactly these filters:

```text
<base>/+/availability
<base>/+/state
```

Device discovery is based on `online`/`offline` availability messages, including retained ones. An offline device may be paired and remains unavailable until fresh state and online availability arrive. Homey's pairing view filters devices already paired using `data.id`. State may arrive before or after availability. The app does not subscribe to scalar metric topics because the full JSON snapshot provides a coherent set of values.

Devices become unavailable on broker disconnection, `offline`, or a missing/old state for **10 minutes**. Both the last valid message arrival and the snapshot's `timestamp` must be fresh: repeated retained copies cannot disguise stale cloud data. The stale timer runs every 15 seconds; state/availability changes are handled immediately. Invalid payloads do not reset freshness. App/broker restart requires fresh retained availability and state before becoming available again.

Reconnect delays grow exponentially from about one second to five minutes, with jitter. The failure counter resets after a connection stays healthy for at least one minute. TLS validates the broker certificate and hostname; there is no insecure TLS switch. Subscription denial and acknowledgement timeouts also reconnect with backoff. Errors never print credentials or raw MQTT payloads. Saved passwords reside in Homey's app settings; this app does not promise additional encryption.

## Capabilities and sign conventions

| Bridge field | Homey capability | Interpretation |
|---|---|---|
| `soc` | `measure_battery` | % |
| `battery_power` | `measure_power` | **Inverted:** Homey positive = charging, negative = discharging |
| `pv_power` | `measure_power.pv` | W, positive generation |
| `grid_power` | `measure_power.grid` | W, positive import / negative export |
| `home_power` | `measure_power.home` | W, whole-home load reported by the bridge |
| `daily_pv_energy` | `meter_power.daily_pv` | Today's solar kWh |
| `daily_backup_energy` | `meter_power.daily_backup` | Today's backup-load kWh, not necessarily the whole home |
| `daily_grid_import_energy` | `meter_power.daily_grid_import` | Today's imported kWh |
| `daily_grid_export_energy` | `meter_power.daily_grid_export` | Today's exported kWh |
| `daily_solar_dc_energy` | `meter_power.daily_solar_dc` | Today's DC solar kWh |
| `daily_solar_ac_energy` | `meter_power.daily_solar_ac` | Today's AC solar kWh |
| `daily_ev_energy` | `meter_power.daily_ev` | Today's EV kWh, if present |
| `mode` | `epcube_mode` | Read-only enum: self-consumption, time-of-use, backup, unknown |

The battery power value is an estimate calculated by the bridge; the main capability is marked approximated. Missing values become `null`, never zero. Bad JSON, wrong IDs, invalid numbers, unexpected modes, oversized messages and older/out-of-order samples are ignored. Duplicate timestamps do not retrigger Flows. The discovery cache is capped at 100 IDs.

### Homey Energy limits

The driver uses class `battery` and `energy.homeBattery: true`. Homey's battery convention is charging-positive, so the bridge's discharging-positive value is inverted. PV/grid/home sub-capabilities are informational; this driver does not also register itself as solar panels or a whole-home meter. [Homey Energy documentation](https://apps.developer.homey.app/the-basics/devices/energy#home-batteries).

The bridge has no lifetime **battery charged/discharged** kWh counters. Daily solar/grid counters are not valid replacements. Consequently, the app deliberately omits the primary `meter_power` and Energy's `meterPowerImportedCapability`/`meterPowerExportedCapability` mappings. CLI validation succeeds with two warnings about these missing battery meters. Energy features requiring cumulative battery totals will be limited; daily sub-capabilities remain visible in the device. No lifetime energy is invented or inferred from resetting daily values.

## Flow cards

Triggers:

- **SOC dropped below X %:** previous SOC ≥ X, new SOC < X.
- **SOC rose above X %:** previous SOC ≤ X, new SOC > X.
- **Operating mode changed:** provides current and previous mode tokens.
- **Grid import rose above X W:** import magnitude crosses from ≤ X to > X.
- **Grid export rose above X W:** export magnitude crosses from ≤ X to > X.

Conditions: **SOC is above X %** and **Operating mode is X**. Conditions return false while the device has no fresh available state.

Threshold cards are crossing events, not repeated notifications while a value remains above/below the limit. The first reading after startup, pairing, disconnection or a data gap establishes a baseline and triggers nothing. There are no action/control cards.

## Development

```sh
npm run typecheck
npm test
npm run build
npm run validate
```

Tests use fake MQTT clients and a Homey SDK stub. They cover discovery, stale data, bad payloads, sign conversion, pairing, capability updates, Flow conditions/crossings, duplicate/reconnect suppression and broker backoff. No real broker or cloud is contacted. `app.json` is the manifest source (this app does not use Homey Compose).

App ID: `com.epcube.mqtt`. This is an unofficial local integration, not a Canadian Solar product. Settings use the documented [Homey settings view](https://apps.developer.homey.app/advanced/custom-views/app-settings) and installation uses the [Homey CLI](https://apps.developer.homey.app/the-basics/getting-started/homey-cli).

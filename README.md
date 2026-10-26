# epcube-bridge

A Python 3.12 asyncio service that reads one Canadian Solar EP Cube account and publishes its single battery system to MQTT and a local REST API. It uses the mobile cloud API described in [docs/EPCUBE_API.md](docs/EPCUBE_API.md).

**Read-only:** the only cloud POSTs are captcha acquisition, captcha verification and login. Device reads are explicitly allowlisted. No setting, tariff, Dynamic pricing, firmware or control calls are made. `ENABLE_CONTROLS` is reserved and does not enable any write routes, even when true. Accounts containing zero or multiple devices are rejected rather than selecting one arbitrarily.

## Start with Docker Compose

You need Docker Compose, your EP Cube app credentials and an existing MQTT broker reachable from the container.

```sh
cp .env.example .env
# Edit .env with your credentials, broker address and a strong API_TOKEN.
# Generate a token, for example: openssl rand -hex 32
chmod 600 .env
docker compose up -d --build
curl http://127.0.0.1:8080/health
```

Compose reads `.env` and passes values as environment variables. The Python service itself does not load `.env`, configuration files, CLI options or secrets files. Do not commit `.env`. If a Compose `.env` value contains `$`, use single quotes to avoid interpolation.

The example binds REST to **127.0.0.1:8080** on the host. If other LAN hosts need access, change the published bind address deliberately and protect that connection (the service serves HTTP, not HTTPS). `MQTT_HOST=host.docker.internal` reaches a host broker on Docker Desktop; on Linux, use a reachable LAN address or add the appropriate host-gateway mapping. The compose file does not start or configure a broker.

Token state lives in the named `epcube-data` volume at `/data/token.json`. The image runs as UID/GID **10001**, with no capabilities and a read-only root filesystem; only `/data` is writable. The volume must be writable by UID 10001 if you replace it with a bind mount. Do not run `docker compose down -v` if you want to keep the cached token.

```sh
docker compose logs --tail=100 epcube-bridge
docker compose down              # keeps the token volume
```

One bridge instance per account/topic is supported. Do not run multiple replicas: they would compete over MQTT client identity and token state.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `EPCUBE_EMAIL` | Required | EP Cube mobile app login |
| `EPCUBE_PASSWORD` | Required | EP Cube mobile app password |
| `EPCUBE_REGION` | `EU` | `EU`, `US` or `JP`; EU is best documented, others have less verification |
| `EPCUBE_TIMEZONE` | `Europe/Rome` | IANA timezone of the battery installation, used for daily energy query dates; change for other locations |
| `POLL_INTERVAL_S` | `30` | Seconds after each completed poll; permitted range 10–86400 |
| `MQTT_HOST` | Required | Broker hostname/IP |
| `MQTT_PORT` | `1883` | Broker port; set `8883` explicitly if that is your TLS listener |
| `MQTT_USER` | Unset | Optional broker username |
| `MQTT_PASSWORD` | Unset | Optional broker password |
| `MQTT_TLS` | `false` | Enable TLS with CA and hostname verification; never uses insecure verification |
| `MQTT_BASE_TOPIC` | `epcube` | Topic prefix; MQTT wildcards and NUL are rejected |
| `API_TOKEN` | Required | Nonempty REST bearer token; use a long random secret |
| `ENABLE_CONTROLS` | `false` | Reserved; true emits a warning and still exposes no writes |

## Authentication and failure handling

The puzzle is solved locally with Pillow/NumPy and AES helpers; credentials are not sent to a third-party solver. A successful bearer token is atomically stored with mode **0600** in `/data`, bound by a fingerprint to account, region and credentials. Passwords are not saved in the token file. The bearer token is plaintext on disk, protected by permissions, not encryption: protect the volume and its backups.

A restart reuses the cached token. HTTP or embedded 401/403 triggers a serialized login and one retry of the failed request. Login has at most three paced puzzle attempts. The actual server token lifetime is unknown; no scheduled login or guessed TTL is used. Changing account, region or password invalidates the cache fingerprint.

Each cycle reads live telemetry and settings once. Energy rollups are fetched at most once every five minutes and immediately on local date rollover. No dynamic-price endpoints are called. The requested 30-second interval is the default, although upstream suggests cloud readings may only change approximately every 55 seconds; `POLL_INTERVAL_S=60` reduces unnecessary calls. At 30 seconds, ordinary traffic is about 120 telemetry/settings requests per hour plus 12 energy-rollup requests, excluding request duration, discovery and authentication.

Cloud failures use exponential delays starting at the configured poll interval, with 0–20% positive jitter and a 15-minute cap (or the configured interval if longer). A valid larger `Retry-After` is honored. There is no immediate retry loop except the one authentication retry. A successful poll resets backoff. MQTT reconnects have their own backoff and never cause extra cloud polls.

The last good sample survives an outage **in memory**. Availability becomes offline on a failed poll or an explicit offline device flag, and also when a sample exceeds `max(90 seconds, 3 × POLL_INTERVAL_S)`. On restart there is no sample until the first successful poll; only the auth token persists. Cloud responses are not transactional; a snapshot combines sequential reads and a cached daily rollup. `timestamp` records successful retrieval, not a vendor-provided measurement time.

## MQTT topics and payloads

All messages use QoS 1 and are retained. For device ID `123`, with the default prefix:

| Topic | Payload |
|---|---|
| `epcube/123/state` | Full JSON snapshot shown below |
| `epcube/123/availability` | `online` or `offline` |
| `epcube/123/soc` | SOC in percent |
| `epcube/123/pv_power` | Solar power in W |
| `epcube/123/grid_power` | W; **positive import, negative export** |
| `epcube/123/battery_power` | W; **positive discharge, negative charge** |
| `epcube/123/home_power` | Backup + non-backup + EV load in W |
| `epcube/123/mode` | `self_consumption`, `time_of_use`, `backup` or `unknown` |
| `epcube/123/reserve_soc` | Active reserve in percent; TOU uses the self-consumption reserve proxy |
| `epcube/123/daily_pv_energy` | Solar energy for the requested local day, kWh |
| `epcube/123/daily_backup_energy` | Backup-load energy for that day, kWh; not necessarily whole-home energy |
| `epcube/123/daily_grid_import_energy` | Imported energy for that day, kWh, positive magnitude |
| `epcube/123/daily_grid_export_energy` | Exported energy for that day, kWh, positive magnitude |
| `epcube/123/daily_solar_dc_energy` | Today's cloud DC solar counter, kWh |
| `epcube/123/daily_solar_ac_energy` | Today's cloud AC solar counter, kWh |
| `epcube/123/daily_ev_energy` | Today's cloud EV energy counter, kWh, if available |

The state topic changes only when metrics or the daily date change, or at a **five-minute heartbeat**. Each scalar independently follows change-or-five-minutes behavior. A new MQTT connection republishes everything. Timestamp-only changes do not generate a new publication every poll. REST always returns the latest successful in-memory sample.

Missing/invalid numeric readings are JSON `null` in the snapshot and the literal `null` on scalar topics, replacing any previous retained reading. They are never fabricated as zero. These scalar messages use plain numbers or plain mode strings, not JSON objects. During a cloud outage, heartbeat snapshots retain the **old sample timestamp** and availability stays offline; a heartbeat does not imply fresh telemetry.

The broker connection has a retained Last Will of `offline` at the per-device availability topic. Clean shutdown explicitly publishes offline. Availability becomes online only after state publication from a successful fresh sample; `/health` reports MQTT connectivity separately. Discovery must identify the device before a per-device Last Will can be registered. If discovery fails on startup, no MQTT connection is established.

### Units and sample

Power uses W; energy uses kWh as requested (rather than SI joules); SOC/reserve use %. The cloud's live power integers are multiplied by **10**, not 1000.

```text
home_power = backup_load + non_backup_load + EV_load
battery_power = home_power - pv_power - grid_power
```

Battery power is a balance estimate and can include measurement skew and conversion losses. It intentionally reverses the upstream integration's charging-positive convention. Import/export energies are separate nonnegative magnitudes; net import energy is import minus export. AC and DC solar energy are alternative diagnostic views, not independent generation to sum. No daily battery charge/discharge totals are invented from SOC deltas.

Example `epcube/123/state`:

```json
{
  "device_id": "123",
  "timestamp": "2026-10-26T12:00:00Z",
  "daily_energy_date": "2026-10-26",
  "daily_energy_timestamp": "2026-10-26T11:58:00Z",
  "timezone": "Europe/Rome",
  "soc": 64.0,
  "pv_power": 1200.0,
  "grid_power": -200.0,
  "battery_power": 800.0,
  "home_power": 1800.0,
  "mode": "self_consumption",
  "reserve_soc": 20.0,
  "daily_pv_energy": 3.2,
  "daily_backup_energy": 4.1,
  "daily_grid_import_energy": 2.1,
  "daily_grid_export_energy": 0.5,
  "daily_solar_dc_energy": 3.4,
  "daily_solar_ac_energy": 3.2,
  "daily_ev_energy": 0.3
}
```

Here the battery supplies 800 W, the PV supplies 1200 W, the home uses 1800 W, and 200 W is exported. `daily_energy_timestamp` is the rollup fetch time; solar DC/AC and EV counters instead arrive with live telemetry and reset on the cube's own clock. Set `EPCUBE_TIMEZONE` to match the installation to avoid querying the wrong calendar day around midnight. The cloud's timezone/DST accounting is not independently verified.

## REST API

No write endpoints, Swagger UI or unauthenticated OpenAPI schema are registered.

```sh
curl http://127.0.0.1:8080/health
# {"status":"ok","cloud_online":true,"mqtt_connected":true}

curl -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8080/devices
# [{"id":"123","available":true}]

curl -H "Authorization: Bearer $API_TOKEN" http://127.0.0.1:8080/devices/123/state
```

Export `API_TOKEN` in your shell for these commands; Compose's `.env` is not automatically exported to your shell. The state response is the snapshot plus `available` and `age_s`. During an outage it returns the last sample with `available: false`. Unknown IDs return 404; a discovered device without a sample returns 503; `/devices` is empty until discovery. Missing/incorrect bearer tokens return 401. Comparisons use constant-time token comparison.

`/health` is deliberately a **liveness** check: HTTP 200 means the REST process can respond, not that the upstream cloud or broker is ready. Its two flags report dependency status, avoiding container restart/login storms during outages. Docker's HEALTHCHECK calls this endpoint.

## Development and verification

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -c constraints.txt -e '.[test]'
pytest -q
ruff check src tests
ruff format --check src tests
```

`constraints.txt` records the versions used for validation and is used by Docker builds. For a native run, export the environment variables and ensure `/data` exists and is writable by the runtime user, then run `epcube-bridge`. Run one process/worker only; the executable starts a single Uvicorn worker.

```sh
docker build --target test -t epcube-bridge:test .
docker build --target runtime -t epcube-bridge:local .
```

Tests use `httpx.MockTransport` for cloud responses, an in-process ASGI transport for REST, and fake MQTT clients. An autouse fixture blocks real socket connections and the default HTTP transport, so tests cannot call the actual cloud. They exercise the local puzzle solver/encryption, login/cache/re-auth, failures/backoff, date-based rollup caching, sign normalization, retained change/heartbeat publishing, Last Will, shutdown and REST authentication/read-only routing.

Errors/logs contain fixed categories rather than raw cloud/broker exception strings or response bodies. Credentials, auth headers and tokens are not logged; HTTP client debug and Uvicorn access logs are disabled by the entry point. Configuration validation emits a generic error without environment values.

The solver is adapted from MIT-licensed `ha-ep-cube`; its original notice is retained in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and in the runtime image. API limitations and unverified region behavior are tracked in [docs/EPCUBE_API.md](docs/EPCUBE_API.md). No live cloud calls are required for development or tests.

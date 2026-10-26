# EP Cube mobile cloud API — repository study

Studied on **2026-10-26**, against [SkiLtY/ha-ep-cube](https://github.com/SkiLtY/ha-ep-cube), commit **`41b2b7c72e094d472463ee1494c51125823dac3a`**. This is a reverse-engineered client contract, not a Canadian Solar API specification. No live account calls or device changes were made for this study. “Observed” below means reported by upstream, not independently reproduced here. Schemas describe fields the client consumes or sends; unknown additional fields and regional differences remain possible. Mock responses and tests are implementation evidence, not independent cloud captures.

## 1. Regions and transport

| Region | Origin | Prefix | Full API root | Evidence / uncertainty |
|---|---|---|---|---|
| EU (primary) | `https://monitoring-eu.epcube.com` | `/api` | `https://monitoring-eu.epcube.com/api` | Upstream reports live use, including UK; EU host does not imply EUR or continental-European tariff semantics. |
| US | `https://epcube-monitoring.com` | `/app-api` | `https://epcube-monitoring.com/app-api` | README still labels experimental; newer code comments report US EV telemetry observations. Do not assume complete EU feature parity. |
| Japan | `https://monitoring-jp.epcube.com` | `/api` | `https://monitoring-jp.epcube.com/api` | Configured but README says untested. |
| Other | User supplied | User supplied (default `/api`) | Origin + prefix | No verified AU/CA host is established here. |

All paths below are **relative to the API root**, e.g. EU `/device/deviceList` means `https://monitoring-eu.epcube.com/api/device/deviceList`. Device identifiers are opaque strings: `devId` is the account device ID; `sgSn` is its serial number. Do not interchange them. [Sources: constants][const], [configuration][config], [README][readme].

### Common headers and envelopes

Every route in this document uses these headers in the client:

```http
User-Agent: ReservoirMonitoring/2.1.0 (iPhone; iOS 18.3.2; Scale/3.00)
Accept: */*
Content-Type: application/json
Accept-Encoding: gzip, deflate
Accept-Language: en-GB
```

All device, tariff and diagnostic requests also send `Authorization: Bearer <token>`. The three `/open/common/*` authentication routes do not. Whether the app-like headers are strictly required is unknown. No `JSESSIONID` cookie is required by this implementation. GET parameters are in the query string, with no application body; POST bodies are JSON. All ordinary responses below describe the **`data` payload** of the usual envelope:

```text
{ timestamp?: unknown, message?: string, status: 200, data?: T }
```

The transport rejects HTTP 401/403 (with one re-login attempt), 429, 5xx and other 4xx. An envelope `status != 200` is also an error, even under HTTP 200. A nested `data.status >= 400` is treated as another error envelope; a domain value such as `data.status: "1"` is not. Some write successes omit `data`; no stronger write-ack schema is established. Non-object response bodies are returned unchanged. Auth uses separate parsing described below. [Source: API transport][api].

## 2. Authentication and renewal

The actual implementation makes **three POST requests** per successful login; some comments call this “4 POSTs” because they count the local solving step.

| Step | Method and path | JSON request | Response fields used |
|---|---|---|---|
| Obtain puzzle | POST `/open/common/captcha/get` | `{clientUid: string}`; fresh UUID v4 for each attempt | Envelope `data.repData` (or fallback unwrapped object): `{originalImageBase64: string, jigsawImageBase64: string, secretKey: string, token: string}` |
| Verify solution | POST `/open/common/captcha/check` | `{clientUid: string, token: string, pointJson: string}` | Success only if `data.repData.result === true` **or** `data.repCode === "0000"`; upstream reports failure code `"6111"` and `repMsg`. Outer `message: "Success"` does not mean the puzzle passed. |
| Login | POST `/open/common/login` | `{userName: string, password: string, captchaVerification: string}` | Envelope `data.token: string` (or top-level `token` fallback). Missing token is a login error. Other response fields are not consumed. |

The password is sent as supplied in JSON over HTTPS; the client does not hash or encrypt it at application level. Captcha tokens and the eventual bearer token are distinct.

### How the captcha is solved locally

1. Base64-decode both PNG images using Pillow; turn them into NumPy arrays.
2. Preserve raw palette indices for palette-mode backgrounds. Upstream found conversion to ordinary grayscale destroyed matching. For RGB/RGBA arrays, discard alpha and use `0.114R + 0.587G + 0.299B`, matching this implementation's channel convention.
3. If the puzzle is up to four pixels taller than the background, pad the background vertically with its mean value. Reject unworkable dimensions and obtain a new puzzle.
4. Use normalized cross-correlation equivalent to `TM_CCOEFF_NORMED`, implemented in NumPy with FFT correlation and integral-image window statistics. The maximum gives horizontal position `x`; report fixed `y = 5`, rather than the matched vertical coordinate.
5. Serialize compact UTF-8 JSON with floating-point coordinates, e.g. `{"x":123.0,"y":5.0}`. `pointJson` is Base64 of AES-ECB encryption of that JSON, with PKCS#7 padding and the UTF-8 `secretKey` bytes as the key.
6. After successful `/check`, construct `captchaVerification` **locally** as Base64(AES-ECB-PKCS7(`token + "---" + plaintext_coordinate_JSON`)). It is not `token + "---" + pointJson`, nor a verification string extracted from `/check`.

Dependencies are NumPy, Pillow and PyCryptodome; no external captcha-solving service or OpenCV runtime is used. Default maximum is three puzzle attempts. Upstream reports roughly 80% acceptance per attempt; this is a small empirical result, not an SLA, and does not prove independent retry probabilities. Credential rejection ends the login rather than retrying credentials three times. [Source: executable auth implementation][captcha].

### Bearer lifetime, refresh and silent re-auth

After login, configuration calls `/device/deviceList`, chooses the device and stores credentials, bearer token and identifiers in the HA config entry. Runtime uses the cached token. On **HTTP** 401 or 403, for either reads or writes, the client calls a callback that repeats the full captcha/login flow with stored credentials, replaces the token, persists it back to the config entry and retries the original request **once**. Failed renewal or a second rejection raises an authentication error. A lock serializes login attempts; it does not guarantee concurrent failed requests share a single refreshed token. There is no implemented refresh-token endpoint, refresh-token field, proactive expiry timer or JWT-expiry parsing. Envelope-level 401/403 does **not** enter this HTTP-status renewal branch. [Sources: auth][captcha], [transport][api], [setup/persistence][init].

**Lifetime is unknown.** The historical spike repeats an anecdotal “days-to-weeks” estimate; it does not establish a measured TTL. Do not use it as a refresh schedule. Opening the official app was alleged elsewhere to revoke write access while preserving reads, but upstream's subsequent EU tests failed to reproduce that across four scenarios. Treat session concurrency/revocation as unresolved across accounts and firmware, not guaranteed behavior. [Source: spike findings and corrections][spike].

The repository comments claim HA encrypts credentials at rest, but the shown code merely stores them in `entry.data`; this study does not establish encryption. A future client needs its own credential-storage decision.

## 3. Implemented read endpoints

All use GET and the common **authenticated** headers. `N` below means a number or numeric string accepted by the client; `?` means optional/possibly absent, not a vendor guarantee of optionality.

| Path | Query schema | Response `data` schema / meaning |
|---|---|---|
| `/device/deviceList` | None | Array of `{devId?: string\|number, id?: string\|number, sgSn: string, systemCapacity?: string, ...}`. `id` is a fallback for `devId`; capacity may be `"20.0kWh"`. Historical capture also reports embedded `workParam`; complete nested schema unknown. |
| `/device/homeDeviceInfo` | `sgSn: string`, `dayMonthYearFormat: "YYYY-MM-DD"` (literal format, not today's date) | Telemetry object detailed below. |
| `/device/getSwitchMode` | `devId: string` | Settings object matching the fields in §4, plus optional `chargingLimitSoc`, `evChargerReserveSoc` and possibly others. Day arrays may return integers despite requiring strings on writes. |
| `/device/queryDataElectricityV2` | `devId: string`, `queryDateStr: string`, `scopeType: integer` | Energy-rollup object detailed below. |
| `/device/getSolarPvPower` | `devId: string` | Usually one-element array of objects with `pv1Power`, `pv1Voltage`, `pv1Current` through `pv4Power`, `pv4Voltage`, `pv4Current`, numeric strings. Parser also accepts a direct object. **Power kW**, voltage V, current A; multiply power by 1000 for W. Incomplete/non-numeric strings are omitted. |
| `/user/user/queryFirmwareInfo` | `devId: string` | `{firmwareVersion?: string, rtuVersion?: string, aotuUpdateFirmware?: bool\|string\|number, ...}`. Preserve the misspelling. Flag is read-only in this integration. |
| `/device/userDeviceInfo` | `devId: string` | `{modelType?: string, sgSn?: string, rtuSn?: string, ...}`. Upstream says owner/address information is also returned but deliberately not retained; complete personal-data schema unknown. |
| `/device/getWarranty` | `devId: string` | `{activationDate?: string, usageEndDate?: string, ...}`. Parser takes first ten characters as ISO `YYYY-MM-DD`. No usable date means no reading. |

Sources: [API methods][api], [device selection][config].

### Live telemetry schema and units

| Fields in `/device/homeDeviceInfo` | Interpretation |
|---|---|
| `batterySoc: N` | SOC, percent. |
| `batteryCurrentElectricity: N` | Stored battery energy, kWh. Integration treats zero, missing or invalid values as unavailable because upstream observed zero glitches around midnight. This is a client heuristic. |
| `batteryPackNum: integer` | Pack count; client assumes 5.0 kWh per pack, overriding cached `systemCapacity`. Applicability to every product variant is unverified. |
| `solarPower`, `gridPower`, `backUpPower`, `nonBackUpPower`, `evPower`: N | **0.01 kW per unit = 10 W per unit**. For example `gridPower: "-17"` means −170 W. |
| `solarElectricity`, `backUpElectricity`, `solarDcElectricity`, `solarAcElectricity`, `evElectricity`: N | Today's kWh counters. `backUpElectricity` covers backup loads, not necessarily the entire house. DC/AC solar channels are diagnostic, not additional generation to sum. |
| `selfHelpRate: N` | Percent self-sufficiency (load met by own generation), despite older naming calling it self-consumption. |
| `winterProtect: N` | Winter reserve floor, percent; read-only, write route unconfirmed. |
| `workStatus: string\|number` | Mode code; current parser prefers this over `getSwitchMode.workStatus` if present. |
| `earningYesterday: N`, `unitDefault?: string` | Yesterday's earnings; currency is region/tariff dependent. UK code labels GBP, which must not be generalized to all EU installations. |
| `gridPowerFailureNum: N`, `offGridPowerSupplyTime: N` | Lifetime outage count; cumulative seconds on backup. |
| `hasEv?`, `hasEvse?` | EV-presence flags. Either can activate EV sensors; EU and US names differ in reported payloads. |
| `isOnline?`, `isFault?`, `isAlert?` | Typically `"1"`/`"0"` flags; parser tolerates boolean/numeric forms. |
| `faultWarningType?`, `signalLevel?`, `systemStatus?`, `winterMode?`, `networking?` | Diagnostic codes. Signal 0–4; system status 4 reported as normal; networking 1 Wi-Fi / 3 Ethernet observed. Complete enums unknown. `faultWarningType: "1"` occurs on a healthy cube; meaning unknown. |
| `lppTimeDuration?`, `lpcTimeDuration?`, `backupLoadsMode?` | Parsed numerically; meanings and duration units **unknown**. |

Sign conventions and derived values:

- Grid power: **positive import, negative export**, from the home's perspective.
- Load W = backup W + non-backup W + EV W. EV is a separate channel; omitting it distorts battery power.
- Battery W = solar W + grid W − load W: **positive charging, negative discharging**. This is a power-balance estimate, not a measured battery-power field; losses and sampling skew can affect it.
- Battery charge/discharge daily and lifetime totals exposed by HA are client estimates from changes in stored kWh, with a 0.05 kWh noise threshold. They are not separate counters returned by this endpoint and can miss intervening charge/discharge between polls.
- Daily reset timing is assumed cube-local; client accumulators and query dates use HA-local time. No timezone field or DST contract is established for these counters.

[Sources: telemetry parsing and estimators][api], [sensor interpretation][sensor].

### Electricity rollups

`scopeType`: `1` daily with `YYYY-MM-DD`; `2` monthly with `YYYY-MM`; `3` annual with `YYYY`; `0` nominal lifetime/total with `YYYY`. Upstream cautions that total currently equals annual until a year rollover; true multi-year behavior is unverified.

Consumed fields, using wire casing: `solarElectricity`, `backUpElectricity`, `gridElectricityFrom`, `gridElectricityTo` (kWh), `coal`, `treeNum`. The API client lowercases every response key before sensors read it; mock casing is not an authoritative wire constraint. Import (`From`) and export (`To`) are separate magnitude counters; signed net import is `From - To`. No extra power scaling applies. `coal` is an environmental estimate, with **unit not stated by the cloud** (upstream assumes kg); `treeNum` is an estimated tree equivalent, not a measured count. Upstream reports factors of 0.4 coal and 0.055 trees per solar kWh. Complete response schema unknown.

The derived self-consumption percentage uses `(solar - export) / solar`; derived self-sufficiency uses `(backup load - import) / backup load`. These are client formulas with denominator/installation limitations, particularly where backup load excludes other loads; neither is a new endpoint. [Sources: scopes][const], [rollup scheduling][coordinator], [sensor formulas][sensor].

## 4. Settings writes

### POST `/device/switchMode`

Authenticated headers; JSON body is a **full settings snapshot**, not a three-field patch. Missing fields have produced validation errors. Preserve spelling and JSON types exactly:

| Request field(s) | Wire type / meaning |
|---|---|
| `devId` | string |
| `workStatus` | string: `"1"` self-consumption, `"2"` time-of-use, `"3"` backup |
| `allowChargingXiaGrid` | string `"1"` allow grid charging / `"0"` disallow; permission, not a direct watt setpoint |
| `selfConsumptioinReserveSoc` | string containing integer SOC percent; misspelling is required |
| `backupPowerReserveSoc` | string containing integer SOC percent; also used by upstream Dynamic charging as the charge-stop target |
| `weatherWatch`, `onlySave` | strings; builder always writes `"0"`. Weather watch conflicts with its optimizer; `onlySave` persists on the cube, so this is not a harmless per-request flag. |
| `touType` | JSON integer `0` fixed TOU, `1` Dynamic tariff (meaningful with mode `"2"`) |
| `dayLightSavingTime` | JSON boolean |
| `peakTimeList`, `midPeakTimeList`, `offPeakTimeList` | arrays of slot strings for ordinary weekdays |
| `peakTimeListNonWorkDay`, `midPeakTimeListNonWorkDay`, `offPeakTimeListNonWorkDay` | arrays for ordinary weekend/non-work days |
| `dayLightPeakTimeList`, `dayLightMidPeakTimeList`, `dayLightOffPeakTimeList` | arrays for DST weekdays |
| `dayLightPeakTimeListNonWorkDay`, `dayLightMidPeakTimeListNonWorkDay`, `dayLightOffPeakTimeListNonWorkDay` | arrays for DST weekend/non-work days |
| `activeWeek`, `activeWeekNonWorkDay`, `dayLightActiveWeek`, `dayLightActiveWeekNonWorkDay` | arrays of **strings**; defaults weekdays `"1"`–`"5"`, weekend `"6"`,`"7"` |
| `dynamicChargePrice?`, `dynamicDischargePrice?` | JSON numbers, Dynamic buy-below and sell-above prices; UK implementation interprets pence/kWh. Other currencies/scales unverified. |

Slot syntax is `HH:MM_HH:MM_price`, with price formatted to two decimals. The client uses price tiers to influence charging/holding/discharging; a slot is not a direct current or power command. **Fixed-slot price currency/scaling is not established as a universal contract**; synthetic optimizer prices should not be confused with measured tariffs. Exact timezone, overlap rules and midnight-boundary behavior require validation against the target system.

Response: ordinary envelope, with no required acknowledgement data consumed. `switch_mode()` then GETs `/device/getSwitchMode`, compares selected requested keys (default only `workStatus`) and returns that read-back object. A 200 alone does not prove a setting was applied; verify every field changed, not only the mode.

Operational caveats:

- A reserve for an inactive mode can be silently ignored. Current reserve entities allow self-consumption reserve edits in self-consumption and provisionally TOU; backup reserve edits only in backup. Dynamic optimizer behavior is an additional mode-specific case, not proof every reserve works in every mode.
- Current executable slider bounds are **0–100% self-consumption, 50–100% backup**, step 1. The older module docstring says 5–100% for self-consumption. Neither is an authoritative server limit; record the contradiction rather than assume a hard minimum.
- TOU “reserve” reported by the client is the self-consumption reserve as a fallback/proxy. Do not claim a separately measured TOU reserve.
- `chargingLimitSoc` is read from settings; upstream achieves its Dynamic target through `backupPowerReserveSoc`, rather than establishing a standalone `chargingLimitSoc` write endpoint.
- An empty schedule is sent through this same route to clear slots; no separate current mobile clear endpoint is implemented.
- Upstream reports Dynamic thresholds can cause grid export or grid charge, conditional on tariff, SOC and permission. Fixed TOU peak mode should not be assumed to force battery export to the grid.
- Historical notes conflict about whether malformed types cause 403 or only 500. Treat both authentication and validation as possibilities after a failed, correctly bounded re-login; avoid an endless re-auth loop.

[Sources: payload builder and verification][api], [reserve controls][number], [optimizer/control implementation][services], [historical findings][spike].

## 5. Dynamic tariff reads and binding write

All routes use authenticated headers. GET requests have no body. Identifier/name fields are strings unless upstream supplies an ID number; client normalizes relevant IDs to strings. Complete list-item schemas are not established beyond the fields below.

| Method and path | Request schema | Response `data` consumed |
|---|---|---|
| GET `/dynamicPrice/calcuPrice` | Query `devId`, optional `checkTime: YYYY-MM-DD` (absent = today) | `{prices?: Price[], pricesExport?: Price[], ...}`; `Price = {startTime: string, endTime: string, priceInc: N, ...}`. Import and export respectively. |
| GET `/dynamicPrice/getRelation` | Query `devId` | `{brandId?, productBrand?, supportCountryId?, productCode?, productCodeOut?, regionCode?, region?, ...}`; saved choice. |
| GET `/dynamicPrice/getCountry` | Query `devId` | Array of `{supportCountryId, supportCountryName?, currentCountry?, ...}` |
| GET `/dynamicPrice/getRegionList` | Query `devId`, `supportCountryId` | Array of `{regionCode, regionName?, ...}` |
| GET `/dynamicPrice/getBrands` | Query `devId`, `supportCountryId`, `regionCode` | Array of `{id, brandName, ...}` |
| GET `/dynamicPrice/getProducts` | Query `devId`, `brandName`, `region` (not `regionCode`), `productType: 0\|1` | Array of `{productCode, productFullName?, productName, ...}`. Type 0 import, 1 export. |
| POST `/dynamicPrice/saveBinding` | JSON object below | Ordinary envelope; method ignores data. Config flow confirms saved identifiers through subsequent `getRelation` reads. |

Price times are naive `YYYY-MM-DD HH:MM:SS` strings, interpreted upstream as UK local time; prices are half-hourly pence/kWh including VAT (`priceInc`) for its UK tariff. Continental EU timezone/currency and DST-day lengths are unverified. No published prices can mean empty lists. An envelope error 500 is interpreted by the client as no configured Dynamic tariff; real HTTP 5xx remains an outage. This heuristic does not prove every envelope 500 has that cause.

Binding body, with exact nesting:

```text
{
  devId: string,
  brandId: string,
  productBrand: string,
  supportCountryId: string,
  productCode: string,       // import product
  productCodeOut: string,    // export product
  regionCode: string,
  ratio: { fraunhofer: {
    standardImportRatio: 0, importServiceCharge: 0,
    standardExportRatio: 0, exportServiceCharge: 0
  }}
}
```

Zeros are the implementation's UK product-based configuration, **not verified settings for factor-based continental tariffs**; those fields' units and allowable values are unknown. Upstream reports this route changed the binding within about one minute without changing operating mode, thresholds or schedules on its tested cube. [Sources: API and binding builder][api], [picker and confirmation flow][config].

## 6. Diagnostic-only route inventory

These are included for completeness because the shipped debug services can call them. A whitelist entry does **not** establish that a route exists or works in every region. No discovery probing was performed for this study.

### GET probes

All use authenticated headers and the ordinary transport. The service passes a string-valued query map, adding `devId` by default. Allowed keys are `devId`, `sgSn`, `queryDateStr`, `scopeType`, `dateStr`, `supportCountryId`, `regionCode`, `region`, `brandName`, `productType`, `brandId`, `dynamicChargePrice`, `checkTime`. **Route-specific required parameters, response schemas and units are unknown for the additional routes below.** These are explicitly read-intended candidates, not verified read contracts.

```text
/device/getSettingsData
/device/getEarningsConfig
/device/queryPriceDataGraphV2
/device/getNewestModeInfo
/device/queryDataGraphV2
/device/getDevPowerCutLog
/device/netWorkInfo
/vpp/allPrograms
/vpp/enrollments
/vpp/events
/vpp/event
/vpp/globalEnrollmentStatus
/vpp/site/programs
/vpp/site/program
/vpp/energyhub/program
/vpp/energyhub/events
/vpp/energyhub/enrollment
/vpp/flip/getGlobalSoc
```

The same whitelist also includes already documented `/device/getSolarPvPower`, `/device/getWarranty` and all six Dynamic GET routes in §5. The generic debug transport returns unwrapped data; output redacts personal-looking keys and truncates lists unless full output is requested.

### POST probes — potentially state-changing

The write whitelist includes the established `/dynamicPrice/saveBinding` and `/device/switchMode`, plus these **speculative/unconfirmed** routes:

```text
/device/setDynamicPrice
/device/saveDynamicPrice
/device/setDynamicPriceBinding
/dynamicPrice/setRelation
/dynamicPrice/saveRelation
/dynamicPrice/setDynamicPrice
/dynamicPrice/save
/dynamicPrice/bind
```

Every candidate uses authenticated headers and JSON. For the eight speculative routes, the diagnostic body is:

```text
{
  devId: string,
  dynamicChargePrice: number,
  dynamicDischargePrice: number,
  backupPowerReserveSoc: string,
  selfConsumptioinReserveSoc: string,
  dynamicPriceBinding: <the binding object in §5>
}
```

For `saveBinding`, it sends the binding object alone. For `switchMode`, it sends the full §4 settings body plus `dynamicPriceBinding`. Values come from current settings/relation; an optional export-product override can change the selected tariff. Intended no-op re-saving is still a write. Response schemas, successful persistence and additional effects of these speculative payloads are **unknown**. [Source: exact whitelists and body construction][services].

Historical `/v1/api/home/*` cookie endpoints and failed mobile candidates in spike notes are not used by the current production client. They must not be mixed into this bearer contract. In particular, the historical mobile `getSellingConfig` probes returned 404. [Source: migration endpoint map][spike].

## 7. Read-only versus writable summary

Telemetry, statistics, identity, firmware versions/update flag, warranty, PV channels, winter protection and diagnostic values are **read-only through this integration**. This does not assert that the vendor has no other write APIs. Derived battery flow, energy estimates, cycle estimates and percentages are local computations.

Operating mode, mode reserves, grid-charge permission, TOU schedules/DST and Dynamic thresholds are changed through `POST /device/switchMode`. Tariff country/region/supplier/import/export choice is changed through `POST /dynamicPrice/saveBinding`. No independent mode/reserve/grid-charge endpoints are needed in the implemented mobile contract. The diagnostic POST candidates in §6 must be treated as writes of uncertain support.

## 8. Rate limits and recommended polling

**No numeric EP Cube quota, window, burst allowance or published limit is established by this repository.** The client handles HTTP 429, but tests injecting 429 are not evidence of a measured production threshold. Do not mistake the Solcast quota mentioned in optimizer documentation for an EP Cube quota.

Upstream's configured cadences provide a practical starting point:

| Data | Cadence |
|---|---|
| Live status/settings and per-string PV | 60 seconds; comment says cloud data updates approximately every 55 seconds |
| Today's energy rollup | 300 seconds |
| Yesterday | Startup and local date rollover |
| Month | 1 hour |
| Year | 6 hours |
| Nominal lifetime rollup | 12 hours |
| Dynamic prices | 900 seconds |
| Warranty, device details, firmware | Daily |
| Device discovery/tariff picker lists | Setup or explicit user action |

At this revision the main coordinator makes **four calls per successful normal tick**: `homeDeviceInfo`, `getSwitchMode` inside `get_status`, another `getSwitchMode`, and `getSolarPvPower`. Thus “60-second polling” is not one request/minute; it is roughly 240 requests/hour/device before stats, prices, metadata, writes and retries. Startup also fetches several buckets. Stats are fetched sequentially. [Sources: intervals][const], [actual call scheduling][coordinator].

**Recommendation, not vendor policy:** start at 60 seconds for telemetry, reuse one settings snapshot per cycle, retain slower cadences above, cache static lists, stagger devices/startup, and issue writes only on changes with targeted read-back. On 429 honor `Retry-After` if present; otherwise use bounded exponential backoff with jitter, likewise for transient 5xx/network faults. The upstream transport does not implement that rate-limit policy; its main coordinator retains stale data for up to three transient failures. Track sample age so stale data is not mistaken for fresh telemetry. Do not aggressively retry captcha/login or a repeatedly rejected write.

## 9. License and reuse

The pinned repository's [LICENSE][license] is **MIT**, copyright **2026 Michael Skilton**. It expressly permits use, copying, modification, merging, publication, distribution, sublicensing and sale of the software and associated documentation. We can reuse its API-client logic, local captcha solver, payload builders and documentation, including in a proprietary/commercial integration, subject to retaining the copyright and permission notice in copies or substantial portions. It does not require publication of our source or impose copyleft. Retain the full MIT notice, including its warranty/liability disclaimer, with redistributed substantial upstream material.

This describes the repository's license text, not a license audit of its dependencies or externally referenced projects. Check those separately if copied or distributed. MIT licensing of this client does not itself grant Canadian Solar cloud-service access rights or settle the service's terms. No application code was copied into this workspace as part of this study.

## 10. Open questions before implementation

- Actual token TTL, captcha expiry, concurrent-session policy and non-HTTP auth error semantics.
- Target EU account's currency, tariff units, timezone/DST rules and factor-based tariff configuration.
- Complete real response fixtures, optionality and types across firmware/regions; absence must not silently imply zero.
- Hard reserve bounds and reserve/charging-limit behavior in each mode, verified by read-back.
- Numeric rate limits, backoff headers and a measured safe multi-device request budget.
- Whether nominal lifetime stats span years; exact diagnostic enums and unknown duration units.
- Any successful contract for speculative probe endpoints; do not build product functionality on their names alone.

## Pinned sources

All links below point to the studied commit so future upstream changes do not silently change the evidence.

[api]: https://github.com/SkiLtY/ha-ep-cube/blob/41b2b7c72e094d472463ee1494c51125823dac3a/custom_components/ep_cube/api.py
[captcha]: https://github.com/SkiLtY/ha-ep-cube/blob/41b2b7c72e094d472463ee1494c51125823dac3a/custom_components/ep_cube/captcha.py
[const]: https://github.com/SkiLtY/ha-ep-cube/blob/41b2b7c72e094d472463ee1494c51125823dac3a/custom_components/ep_cube/const.py
[config]: https://github.com/SkiLtY/ha-ep-cube/blob/41b2b7c72e094d472463ee1494c51125823dac3a/custom_components/ep_cube/config_flow.py
[init]: https://github.com/SkiLtY/ha-ep-cube/blob/41b2b7c72e094d472463ee1494c51125823dac3a/custom_components/ep_cube/__init__.py
[coordinator]: https://github.com/SkiLtY/ha-ep-cube/blob/41b2b7c72e094d472463ee1494c51125823dac3a/custom_components/ep_cube/coordinator.py
[sensor]: https://github.com/SkiLtY/ha-ep-cube/blob/41b2b7c72e094d472463ee1494c51125823dac3a/custom_components/ep_cube/sensor.py
[number]: https://github.com/SkiLtY/ha-ep-cube/blob/41b2b7c72e094d472463ee1494c51125823dac3a/custom_components/ep_cube/number.py
[services]: https://github.com/SkiLtY/ha-ep-cube/blob/41b2b7c72e094d472463ee1494c51125823dac3a/custom_components/ep_cube/services.py
[spike]: https://github.com/SkiLtY/ha-ep-cube/blob/41b2b7c72e094d472463ee1494c51125823dac3a/docs/PHASE_3_2.md
[readme]: https://github.com/SkiLtY/ha-ep-cube/blob/41b2b7c72e094d472463ee1494c51125823dac3a/README.md
[license]: https://github.com/SkiLtY/ha-ep-cube/blob/41b2b7c72e094d472463ee1494c51125823dac3a/LICENSE

# EMS-1000 build reference

Exact strings, examples and commands behind the *EMS-1000 Device–Cloud Interface Spec* (the
pre-read). Section numbers match the spec: A1 goes with section 1, A2 with section 2, and so on.

![EMS-1000 state machine: every start and reset passes the same pre-check; faults latch until reset](img/state-machine.png)

## A1. Connection details

| Setting | Value |
|---|---|
| Host | `iot-ems1000-stg.azure-devices.net`, port 8883 |
| Client ID | the device ID |
| Username | `iot-ems1000-stg.azure-devices.net/<device id>/?api-version=2021-04-12` |
| Password | `SharedAccessSignature sr=iot-ems1000-stg.azure-devices.net%2Fdevices%2F<device id>&sig=<signature>&se=<expiry epoch s>` |
| TLS | 1.2 minimum; validate against DigiCert Global Root G2 |
| Keep-alive | 60–240 s |
| After every connect | re-subscribe to the method and twin topics |

## A2. Telemetry

**Topic:** `devices/<device id>/messages/events/$.ct=application%2Fjson&$.ce=utf-8&traceparent=<value>`, QoS 1.

`traceparent` = `00-<32 hex trace id>-<16 hex parent id>-01`.

```json
{"sv":1,"id":"ems-sim-01","ts":1794666969792,"boot":12,"seq":348,"v":218.5,"i":3.41,"f":51.12,"fuel":84.9,"oil":0,"t":68.7,"vb":13.8,"flt":0,"run":2,"rssi":-68}
```

## A4. Direct methods

| Purpose | Topic |
|---|---|
| Subscribe | `$iothub/methods/POST/#` |
| Request arrives | `$iothub/methods/POST/<method name>/?$rid=<request id>` |
| Reply | `$iothub/methods/res/<status>/?$rid=<request id>` |

Request body from the API: `{"command_id":"<8 hex>"}`. Reply example:

```json
{"result":"accepted","state":"CRANKING","cmd_id":"9f3a01c2"}
```

The `get_status` reply adds `state`, `run`, `flt` and `readings` (the telemetry readings).

## A5. Device twin

| Purpose | Topic |
|---|---|
| Subscribe: twin replies | `$iothub/twin/res/#` |
| Subscribe: desired changes | `$iothub/twin/PATCH/properties/desired/#` |
| Read the whole twin | publish an empty body to `$iothub/twin/GET/?$rid=<n>` |
| Update reported | publish JSON to `$iothub/twin/PATCH/properties/reported/?$rid=<n>` |

Reported example:

```json
{"device":{"model":"Sumec Firman ECO4990ES","fw_version":"1.0.0","boot":12,"started_at":"2026-11-15T09:12:03Z"},
 "state":"RUNNING","run":2,"flt":0,
 "last_fault":{"bits":2,"reason":"Out of fuel: engine stalled","at":"2026-11-15T10:01:44Z"},
 "settings_status":{"load_pct":{"status":"rejected","reason":"not supported on hardware","desired_version":7}}}
```

## A7. Message budget detail

The free tier meters every operation in 0.5 KB blocks ([IoT Hub billing](https://learn.microsoft.com/azure/iot-hub/iot-hub-devguide-pricing)).

| Traffic | Messages | Per day, one device |
|---|---|---|
| Heartbeat every 60 s (~155 bytes) | 1 each | 1,440 |
| On-change telemetry | 1 each | depends on events |
| Each command | 1 request + 1 reply | small |
| Twin read and reported updates | 1 per 0.5 KB | small if patches stay short |

Cloud processing: the route `telemetry-to-builtin` feeds the Function `telemetry_ingest`, which
writes `DeviceState` (latest row per device) and `DeviceTelemetry` (history, 9 days). The route
`telemetry-to-archive` copies raw messages to blob storage; malformed JSON goes to a dead-letter blob.

## A9. Testing against staging

1. Register the unit (cloud owner), then share its connection string securely:
   ```bash
   az iot hub device-identity create --hub-name iot-ems1000-stg --device-id <device id>
   az iot hub device-identity connection-string show --hub-name iot-ems1000-stg --device-id <device id>
   ```
2. Watch messages arrive:
   ```bash
   az iot hub monitor-events --hub-name iot-ems1000-stg --device-id <device id> --properties all
   ```
3. Send a command the way the API does:
   ```bash
   az iot hub invoke-device-method --hub-name iot-ems1000-stg --device-id <device id> --method-name remote_start --method-payload '{"command_id":"test0001"}'
   ```
4. Check the cloud's view at https://app-ems1000-stg-4s1y.azurewebsites.net/docs (`/state`, `/twin`).
5. Compare with the simulator: [TESTING.md](TESTING.md) (tests M1-01 to M6-07); `python cli.py run --dry-run` prints the exact messages.

## Sources

- Firmware spec: EMS-1000 Wokwi ESP32 firmware and simulation spec (wokwi-proj-09)
- Reference device: this repository
- [IoT Hub billing and metering](https://learn.microsoft.com/azure/iot-hub/iot-hub-devguide-pricing) · [IoT Hub quotas and throttling](https://learn.microsoft.com/azure/iot-hub/iot-hub-devguide-quotas-throttling)

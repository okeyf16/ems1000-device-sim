# Live test guide

Manual end-to-end tests against the staging environment. IDs match the **Test log** tab of the
*EMS1000 Simulator Milestones* sheet; record each result there.

The automated tests (`pytest -q`, run by CI on every PR) cover the simulator's logic. These live
tests cover what they can't: the real IoT Hub connection, the Azure pipeline and the API.

## Before you start

| What | Value |
|---|---|
| Codespace | `okeyf16/ems1000-device-sim`, secret `IOTHUB_DEVICE_CONNECTION_STRING` set |
| IoT Hub | `iot-ems1000-stg`, device `ems-sim-01` |
| API docs | https://app-ems1000-stg-4s1y.azurewebsites.net/docs |
| Get the latest code | `git checkout main && git pull` |

**Sending a command from `/docs`:** expand **POST /api/devices/{device_id}/command** → **Try it
out** → `device_id` = `ems-sim-01` → choose an entry in the **Examples** dropdown → **Execute**.
The response code and body appear below.

**Reading state from `/docs`:** **GET /api/devices/{device_id}/state** → Try it out →
`ems-sim-01` → Execute. History: **GET …/telemetry** with `limit` = 5 (newest first).

**Sending a command from the portal:** IoT Hub → **Devices** → `ems-sim-01` → **Direct method**
→ Method name (e.g. `get_status`), Payload `{}` → **Invoke method**.

Use `--interval 10` for live tests so results show up quickly (about 8,640 messages/day if left
running; the budget guard stops at 7,500). Press **Ctrl+C** to stop the sim between tests.
Telemetry also appears in the sim's own terminal as each message is sent.

## M1: connection and telemetry

| ID | Steps | Pass when |
|---|---|---|
| M1-01 | `python cli.py run --interval 5 --count 3` | "IoT Hub connection connected", 3 × "sent", budget counts up |
| M1-02 | Portal: IoT Hub → **Metrics** → "Routing: messages delivered to messages/events", Sum, last 30 min | Count matches messages sent (metrics lag 5–10 min) |
| M1-03 | Run M1-01, then Storage account → **Storage browser** → Tables → `DeviceState` | Row `ems-sim-01` / `latest` has the new `seq` and timestamp |

## History

| ID | Steps | Pass when |
|---|---|---|
| H-01 | Send a few messages, then `/docs` → GET …/telemetry?limit=5 | One row per message, newest first |
| H-02 | Wait 5–10 min, Storage browser → Blob containers → `telemetry-archive` | Blobs under `iot-ems1000-stg/…` |
| H-03 | Function App → `history_purge` → **Code + Test** → **Test/Run** | Succeeds; deletes only rows older than 9 days |

## M2: commands and state machine

Start the sim first: `python cli.py run --interval 10`.

| ID | Steps | Pass when |
|---|---|---|
| M2-01 | POST command `start` | 200, `result` accepted, state CRANKING; sim logs run 1 then 2 |
| M2-02 | GET …/state | `run` 2, `v` ≈ 218, `f` ≈ 51 |
| M2-03 | POST `start` again | 400 "Generator is already running" |
| M2-04 | POST `stop` | 200, STOPPING; run 3 then 0 after 3 s |
| M2-05 | GET …/telemetry?limit=5 | run codes 0, 3, 2, 1 newest first |
| M2-06 | POST `get_status` (or portal direct method `get_status`) | 200 with state, run, flt, readings; no telemetry message used |
| M2-07 | Set `fuel_start_pct: 3.0` in `config/default.yaml`, restart sim, POST `start` | 409 "Fuel Level Below Threshold"; state run 4, flt 2 |
| M2-08 | Straight after M2-07, POST `reset_fault` | 409 "Condition still present: Fuel Level Below Threshold". Restore `fuel_start_pct: 85.0` |
| M2-09 | Stop the sim (Ctrl+C), POST `start` | 504 "Device unreachable" |
| M2-10 | POST `{"command": "explode"}` | 400 "Invalid command. Must be one of: …" |

## M3: generator model, faults, keys and scenarios

| ID | Steps | Pass when |
|---|---|---|
| M3-01 | Set `load_pct: 110`, restart sim, POST `start`, wait ~5 s | A message with `v` 0, `i` 0, `flt` 48, `run` 2. Restore `load_pct: 25.0` |
| M3-02 | Set `oil_start: 2`, restart sim, POST `start` | 409 "Low Oil Level Warning"; telemetry run 4, flt 1. Restore `oil_start: 0` |
| M3-03 | `python cli.py run --interval 10`, press `?`, `s`, `+` `+`, `i`, `x` | Key list shown; starts; load 45 %; status line; stops |
| M3-04 | `python cli.py run --interval 10 --scenario overload` (~1.5 min) | Breaker trip message (`v` 0, `flt` 48, `run` 2); reset refused, then accepted after load drops |
| M3-05 | `python cli.py run --interval 10 --scenario out-of-fuel` (~3 min) | Low-fuel warning, stall (run 4, flt 2), refuel, reset 200 cleared, restart |

Faults you can also try by key while the sim runs: `e` then `s` (start refused, fuel), `o` `o`
while running (low-oil trip), `h` while running (over-temp trip after a few minutes, faster with
`--speed 60`), `b` then `s` (weak battery, flt 8).

Tip: `git checkout config/default.yaml` undoes any test edits to the config.

## M5: device twin

Needs the staging API from ems1000-staging PR #9 deployed. In `/docs`: **GET /api/devices/{device_id}/twin**
and **PATCH /api/devices/{device_id}/twin** (the Examples dropdown has ready-made bodies). Portal alternative:
IoT Hub → Devices → `ems-sim-01` → **Device twin** (edit `properties.desired`, then **Save**).

Start the sim first: `python cli.py run`.

| ID | Steps | Pass when |
|---|---|---|
| M5-01 | GET …/twin | `connection_state` Connected; `reported.device.model` "Sumec Firman ECO4990ES", `reported.state` "OFF", `reported.settings` lists the 6 settings |
| M5-02 | PATCH example "Telemetry every 10 s", then GET …/twin | Sim logs `twin: telemetry_interval_s = 10` and heartbeats every 10 s; `reported.settings_status.telemetry_interval_s.status` "applied" |
| M5-03 | POST command `start`, then PATCH example "Overload: 110 %" | After ~5 s a message with `v` 0, `flt` 48, `run` 2; `reported.settings.load_pct` 110 |
| M5-04 | Portal Device twin: add `"speed": 0` under `desired`, Save. Then API PATCH `{"speed": 0}` | Portal: `reported.settings_status.speed` "rejected", reason "must be between 1 and 3600". API: 400, nothing sent |
| M5-05 | Stop the sim (Ctrl+C). PATCH `{"load_pct": 60}`. Start the sim | Start-up log `twin: load_pct = 60.0 (overrides config)`; `reported.settings.load_pct` 60 |
| M5-06 | Press `e` then `s` in the sim (start with empty tank), then GET …/twin. Press `F`, then POST `reset_fault` | `reported.state` "FAULT", `last_fault.reason` "Fuel Level Below Threshold"; after the reset state "OFF" and `last_fault` still there |

Afterwards: `/docs` → **DELETE /api/devices/{device_id}/twin/settings** → Try it out → `ems-sim-01`, leave `name` empty → Execute. All six settings are cleared and the sim goes back to its config values (the sim logs `cleared, back to config value` for each). Pass: 200 with `cleared` listing the six settings.

## M6: web control panel

Run `python cli.py run --interval 10 --panel` and open port 8080 from the Codespaces notification or the
**Ports** tab (*EMS panel* → globe icon). Check the port's visibility says **Private**.

| ID | Steps | Pass when |
|---|---|---|
| M6-01 | Open the panel | Lamps show Off; six dials with coloured bands; "No faults or warnings"; the terminal shows no errors |
| M6-02 | Click **Start**, then **Stop** | Lamps go Cranking → Running (dials move: ~218 V, ~51 Hz) → Stopping → Off; both actions listed under *Last actions* |
| M6-03 | Start, drag **Load** to 110 % | After ~5 s: "Output breaker tripped", voltage and current dials drop to 0, lamp still Running. Drag back to 50 %, click **Reset breaker**: voltage returns |
| M6-04 | Click **Empty the tank** while running | Fault lamp blinks; "Out of fuel: engine stalled". **Refuel**, **Reset fault**: lamp back to Off |
| M6-05 | Pick **overheat**, click **Play** | Progress line steps through; temperature dial climbs into red, Fault, then cools; finishes "Scenario finished". **Stop** during a run stops it ("Scenario stopped") |
| M6-06 | Leave it running a few minutes | *Last N minutes* charts fill in; running periods shaded, faults marked with a red line; reload the page: history is kept |
| M6-07 | Look at *What Azure holds* | Last message a few seconds old, stored state matches, IoT Hub sees Connected, "Device and cloud agree" (a check may appear for a few seconds right after a change) |

## M4: CI and container

| ID | Steps | Pass when |
|---|---|---|
| M4-01 | Open any PR on the simulator repo | "CI / Lint + tests" and "CI / Docker image builds and starts" both ✅ |
| M4-02 | In the Codespace: `docker build -t ems1000-device-sim .` then `docker run --rm ems1000-device-sim --dry-run --interval 2 --count 3` | Image builds; 3 dry-run heartbeats printed, then "stopped after 3 messages" |
| M4-03 | (optional, live) `docker run --rm -e IOTHUB_DEVICE_CONNECTION_STRING ems1000-device-sim --interval 10 --count 3` | 3 messages reach IoT Hub, as in M1-01 |

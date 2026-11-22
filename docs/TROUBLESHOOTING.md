# Troubleshooting

Problems met while building the EMS-1000 staging pipeline, by symptom. Pipeline:
**sim → IoT Hub → route `telemetry-to-builtin` → built-in endpoint → Function `telemetry_ingest`
→ tables `DeviceState` + `DeviceTelemetry` → API → mobile app**. Commands go back:
**app → API `POST /command` → IoT Hub direct method → sim**.

## The simulator

| Symptom | Likely cause | Fix |
|---|---|---|
| `No connection string for ems-sim-01` | Secret/variable not set | Codespace: set the `IOTHUB_DEVICE_CONNECTION_STRING` secret for this repo, then **rebuild** or restart the codespace. Locally: copy `.env.example` to `.env` |
| `Connection string for … has a different DeviceId` | Using another device's string | Copy the string from IoT Hub → Devices → `ems-sim-01` |
| Sim stops sending, log says budget | Daily guard reached 7,500 (IoT Hub F1 allows 8,000/day, UTC) | Wait for 00:00 UTC. `python cli.py budget` shows the count. Use `--interval 60` for long runs |
| "Another simulator is already running in this workspace (PID n)" | A copy is still running in another terminal (a VS Code window reload keeps terminal processes alive) | Ctrl+C in that terminal, or `pkill -f 'cli.py run'`, then start again |
| "Port 8080 is already in use, probably by another simulator" | As above, or a container started with `-p 8080:8080` | `pkill -f 'cli.py run'`; `docker ps` and `docker stop <id>`; or `--panel-port 8081` |
| "IoT Hub dropped ems-sim-01 5 times in 60 s … same device ID" | Another client is connected as the same device: a container, another Codespace, Paul testing with the same ID | Stop the other client. Each device (simulator, Wokwi, Paul's board) needs its own device identity |
| "IoT Hub refused the device (403) … daily message quota" | The hub's 8,000 messages for today are used up (by anything, not only this simulator) | Wait for 00:00 UTC (01:00 WAT). Portal: IoT Hub → Overview shows the count. Find what used it: IoT Hub → Metrics (telemetry, twin reads, method calls) |
| "heartbeat #n not sent: not connected to IoT Hub" | A short network or hub hiccup | Nothing: the SDK reconnects and the next heartbeat goes out |
| Keys do nothing | Not a terminal (piped, container) or `--headless` | Run in the Codespace terminal without `--headless` |
| Scenario stops with "timed out" | A condition never became true | Check the config hasn't been edited (e.g. fuel too low to start): `git checkout config/default.yaml` |
| Rebuild ends in a **recovery container** (bare Alpine, no Python); log shows `NO_PUBKEY` / `repository ... is not signed` | A package repository in the base image has a bad signing key, so dev-container features fail to install | `.devcontainer/Dockerfile` removes the Yarn repository for this reason. If another repository breaks, remove it the same way; the "Dev container" CI check builds the image on PRs |
| `docker: command not found` in the Codespace | Codespace not rebuilt since Docker was added | Ctrl+Shift+P → **Codespaces: Rebuild Container** |
| Codespace stopped by itself | Idle timeout (30 min default), or monthly quota used | Restart it. Long unattended runs belong in a container (see README, Docker) |

## Web panel

| Symptom | Likely cause | Fix |
|---|---|---|
| "Web panel could not start on port 8080" | Another sim (or something else) already uses the port | Stop the other sim, or `--panel-port 8081` |
| Red banner "The panel can't reach the simulator" | The sim was stopped (Ctrl+C) or crashed | Restart `python cli.py run --panel`; the page reconnects by itself |
| No Codespaces notification for 8080 | Notifications dismissed, or codespace not rebuilt since M6 | **Ports** tab → Forward a Port → 8080 |
| *What Azure holds* says "API key missing or wrong" or "No API key" | `EMS_API_KEY` not set, or an old key after rotation | Add or update the Codespace secret `EMS_API_KEY` (staging output `api_keys`), then restart the Codespace or the simulator |
| *What Azure holds* shows HTTP errors | Staging API down, quota exceeded (403), or wrong `panel.api_url` | Open the API's `/docs`; App Service quotas (see README); check the URL |
| Cloud view says the last message is old | Telemetry isn't reaching Azure | See *Telemetry doesn't arrive* below |
| "Web panel not started: EMS_PANEL_USERS entry n …" | That entry is malformed (needs `Name:role:PIN`), has an unknown role, a short PIN, or repeats a name or PIN | Fix the entry in the Codespace secret, restart the Codespace; `python cli.py panel-users` checks it |
| "Web panel not started: Set both EMS_PANEL_VIEWER_PIN and …" | Older two-PIN setup with only one set | Switch to `EMS_PANEL_USERS`, or set both |
| Someone can't sign in any more | Their entry was removed or their PIN changed | Send them their current PIN |
| Someone isn't in the Signed in list | Not active in the last 15 minutes, or signed out | Nothing to fix |
| A visitor sees a GitHub sign-in page instead of the panel's PIN page | Port 8080 is Private (it can reset after a restart) | Ports tab → right-click 8080 → **Port visibility** → **Public** |
| The shared link doesn't load at all | Your Codespace stopped (idle timeout) or the sim isn't running with `--panel` | Start the Codespace and the sim; raise the idle timeout (README, *Sharing the panel*) |
| "Too many wrong PINs. Try again in N s." | Five wrong PINs from that address in 5 minutes | Wait it out; check the PIN you sent them |
| Controls are greyed out | Signed in with a viewer PIN | Give that person an operator entry, or sign in with yours |
| Everyone was signed out | A PIN changed (sessions are tied to the PINs) or 12 h passed | Sign in again |

## Telemetry doesn't arrive

Check each hop in order; IoT Hub metrics lag 5–10 minutes.

| Symptom | Likely cause | Fix |
|---|---|---|
| Sim says "sent" but hub **Metrics** show nothing | Metrics delay | Wait 10 min; set granularity to 1 minute, aggregation Sum |
| Hub receives, "Routing: messages delivered" is 0 or "dropped" rises | No route to the built-in endpoint: with any custom route defined, messages only go where a route sends them | Terraform's `telemetry-to-builtin` route must exist (`azurerm_iothub_route`, endpoint `events`). Check IoT Hub → **Message routing** |
| Routed, but the Function never runs | Function not deployed / not indexed | Function App → **Functions** list must show `telemetry_ingest` and `history_purge`. If empty, see below |
| Function list is empty after deploy | Python packages failed to import (wrong-platform wheels, missing dependency) | Packages must be built for `manylinux2014_x86_64` / Python 3.11 (see the deploy workflow). Logs: Application Insights → **Logs** → `traces` / `exceptions` |
| Executions fail with `can't subtract offset-naive and offset-aware datetimes` | Event Hub trigger's enqueued time has no timezone | Fixed in staging PR #4 (`_as_utc`). Redeploy if it reappears |
| Executions fail on table insert ("out of range") | Large integers (`ts` in ms) stored as Int32 | Fixed in staging PR #4 (Int64 `EntityProperty`) |
| `DeviceState` updates but history is empty | `DeviceTelemetry` table missing or app settings not applied | Terraform creates the table; check the `HISTORY_*` app settings on the Function App |

Where to look in Application Insights: Function App → **Application Insights** → **Logs** (KQL).
`traces | where timestamp > ago(30m) | order by timestamp desc` and the same for `exceptions`.
**Invocations** (Function → Monitor) also lists each run with its log.

## Device twin

| Symptom | Likely cause | Fix |
|---|---|---|
| Sim starts with an unexpected load/interval/speed | A desired setting stored in the twin overrides the config (start-up log says "overrides config") | `/docs` → **DELETE …/twin/settings** (all, or `name=load_pct` for one), or remove it from `desired` in the portal Device twin |
| PATCH returns 200 but nothing changes | The sim is offline (applies on next connect) or rejected the value | GET …/twin: check `connection_state` and `reported.settings_status` |
| `settings_status` says rejected | Out of range or unknown name (only possible via the portal; the API checks first) | Fix the value; ranges are in the README |
| Reported properties stale | Twin updates are skipped once the daily budget is reached | `python cli.py budget`; wait for 00:00 UTC |
| Log: "could not read the device twin; running without it" | IoT Hub twin call failed at start-up | Telemetry and commands still work; restart the sim to retry |

## API and commands

`POST /api/devices/{id}/command` mirrors the device's answer:

| Code | Meaning | Usually |
|---|---|---|
| 200 | Device accepted (`accepted`, `cleared`, `no_fault`, `ok`) | Fine |
| 400 | Device refused the request as invalid now ("already running", "stopping"), or the API rejected an unknown command | Check state; valid commands: start, stop, reset_fault, get_status |
| 404 | Device doesn't know the method | Sim older than M2, or a typo in a portal direct method name |
| 409 | Safety interlock or fault lockout ("Low Oil Level Warning", "Condition still present: …") | Clear the condition (refuel, cool down…), then `reset_fault` |
| 502 | Device returned an error (5xx) | See the sim terminal for the exception |
| 503 | API itself failing to start | App Service → **Log stream**; usually a dependency/import error after a deploy |
| 504 | Device unreachable | The sim isn't running or isn't connected. Start it and retry |

`/state` shows old data: the sim may be stopped, or telemetry isn't reaching the Function (see
above). `/state` is the last message received; `get_status` asks the device live.

## Terraform (HCP Terraform, VCS-driven)

| Symptom | Cause | Fix |
|---|---|---|
| Plan wants to **replace** the IoT Hub | Provider drift on a ForceNew attribute (`min_tls_version`) | Pin it in code (`min_tls_version = "1.2"`); `prevent_destroy` blocks accidental replacement |
| Plan changes `WEBSITE_RUN_FROM_PACKAGE` every run | Deploy workflow sets it outside Terraform | `ignore_changes` on that setting (already in place) |
| Merge done but nothing changed in Azure | HCP plans automatically but applies manually | HCP Terraform → workspace → run → **Confirm & apply** |

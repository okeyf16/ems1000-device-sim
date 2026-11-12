# ems1000-device-sim

A Python virtual **EMS-1000** generator controller that replaces the Wokwi ESP32 simulation (`wokwi-proj-09`).
It connects to Azure IoT Hub over MQTT (TLS, port 8883) and sends the same telemetry the firmware sends.

**Status: M1.** It connects, sends a 60-second heartbeat in the firmware's schema v1, and has a daily message budget guard.
Start/stop commands (M2), realistic readings and faults (M3), and tests/CI (M4) come next. The full plan is in the project doc `claude/ems-device-sim-plan.md`.

## Quick start in GitHub Codespaces

1. Repo → **Code → Codespaces → Create codespace on main**. Python 3.11, the Azure CLI and the dependencies install automatically.
2. Add the secret `IOTHUB_DEVICE_CONNECTION_STRING` under GitHub **Settings → Codespaces → Secrets** (give it access to this repo), then rebuild or reopen the codespace. You can use a local `.env` instead.
3. `python cli.py run`

Codespaces stop after the idle timeout (30 min by default). A running script doesn't count as activity, so for long unattended runs see M4 (Container Apps).

## Quick start (local)

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# 1. Offline check: prints 3 payloads, no Azure needed
python cli.py run --dry-run --interval 2 --count 3

# 2. Register the device (needs `az login`; writes .env)
bash scripts/register_device.sh <your-iot-hub-name> ems-sim-01 <resource-group>

# 3. Go live
python cli.py run                 # 60 s heartbeat, Ctrl+C to stop
python cli.py run --interval 10   # demo mode (uses ~8,640 msgs/day: the guard will stop it at 7,500)
python cli.py budget              # messages used today (UTC)
```

## Checking that it works (M1 acceptance)

1. **Messages arrive at the hub:**
   `az iot hub monitor-events --hub-name <hub> --device-id ems-sim-01 --properties all`
   You should see the JSON body, `content-type: application/json` and a `traceparent` property.
2. **The pipeline stores them:** the ingest Function upserts the `DeviceState` table row
   `PartitionKey=ems-sim-01, RowKey=latest`. Check it in the portal's Storage browser, or with
   `GET /api/devices/ems-sim-01/state` on the staging API.
3. **Unit tests:** `pytest -q`

## Telemetry (schema v1, < 512 bytes; same fields and order as the firmware)

`sv, id, ts, boot, seq, v, i, f, fuel, oil, t, vb, flt, rssi, run`

- `boot` is kept in `.state/<device>.json` and goes up on every restart, like the firmware's `bootCount`.
- `seq` starts again at 1 after each boot.
- Every payload is checked against the schema (fields, types, ranges, size) before sending. A payload that fails is logged and **not** sent.

In M1 the generator is always **OFF** (`run=0`, v/i/f = 0, battery resting, engine at ambient temperature).

## Free-tier budget

IoT Hub F1 allows **8,000 messages per UTC day for the whole hub**, and direct-method calls count too.
The simulator counts its own messages in `.state/budget.json` (the count survives restarts and resets at 00:00 UTC) and stops sending at `budget.stop_at` (7,500 by default).
One device at 60 s uses 1,440 messages a day.

## Several devices

Add ids under `devices:` in `config/default.yaml`, then register each one with the script.
Each device gets its own variable `IOTHUB_CONN_<ID>` in `.env`, and each runs in its own thread.

## Layout

```
config/default.yaml   device ids, cadence, budget, sensor baseline
sim/config.py         YAML + .env loading
sim/transport.py      IoT Hub connection (azure-iot-device), behind an interface so paho-mqtt can replace it later
sim/telemetry.py      payload builder + contract validation + traceparent
sim/sensors.py        sensor model (M1: OFF baseline)
sim/budget.py         daily message guard
sim/persist.py        boot counter / state files
sim/device.py         heartbeat loop
cli.py                command line
scripts/register_device.sh
```

# Scenarios

Scripted fault timelines for `python cli.py run --scenario <name>`. List them with
`python cli.py scenarios`. The format is described at the top of `sim/scenario.py`.

| Scenario | Shows | Real time |
|---|---|---|
| `normal-day` | start, load 25 → 50 → 75 → 25 %, stop | ~2.5 min |
| `out-of-fuel` | low-fuel warning, stall (Fault bit 1), refuel, reset, restart | ~3 min |
| `overload` | alerts above rated, breaker trip after 5 s, reset refused then accepted | ~1.5 min |
| `overheat` | over-temperature trip (bit 2), reset refused until below 80 °C | ~1 min |

`wait` and `timeout` are engine seconds, so the scenario's `speed` (or `--speed`) sets how
long it takes in real time.

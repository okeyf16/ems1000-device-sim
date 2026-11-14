#!/usr/bin/env python3
"""EMS-1000 virtual device simulator - command line entry point.

Examples:
  python cli.py run --dry-run --interval 2 --count 3   # offline check, prints payloads
  python cli.py run                                     # live, 60 s heartbeat
  python cli.py run --interval 10                       # demo mode
  python cli.py run --interval 10 --speed 60            # engine time 60x faster
  python cli.py run --interval 10 --scenario overload   # play config/scenarios/overload.yaml
  python cli.py run --headless --scenario out-of-fuel   # no keyboard (containers, CI)
  python cli.py scenarios                               # list scenarios
  python cli.py budget                                  # messages used today
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from sim.budget import BudgetGuard
from sim.config import connection_string_for, load_config
from sim.controls import Controls
from sim.device import VirtualDevice
from sim.keyboard import start_keyboard
from sim.scenario import ScenarioRunner, available, load_scenario, start_scenario
from sim.transport import AzureSdkTransport, DryRunTransport


MAX_SPEED = 3600  # 1 engine hour per real second


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    if args.interval is not None and args.interval < 1:
        logging.error("--interval must be at least 1 second")
        return 2
    scenario = None
    if args.scenario:
        try:
            scenario = load_scenario(args.scenario)
        except ValueError as err:
            logging.error("%s", err)
            return 2
        cfg.sensors = {**cfg.sensors, **scenario.setup}
    speed = args.speed if args.speed is not None else (
        scenario.speed if scenario and scenario.speed is not None else cfg.speed)
    if not 0 < speed <= MAX_SPEED:
        logging.error("--speed must be above 0 and at most %d", MAX_SPEED)
        return 2
    cfg.speed = speed
    if speed != 1:
        logging.info("Model speed %gx: 1 real second = %g engine seconds. Heartbeat, commands "
                     "and crank/stop timings stay in real time.", speed, speed)
    devices = [args.device] if args.device else cfg.devices
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    budget = None if args.dry_run else BudgetGuard(cfg.state_dir, cfg.stop_at, cfg.daily_limit)
    sims: list[VirtualDevice] = []
    for dev_id in devices:
        if args.dry_run:
            transport = DryRunTransport(dev_id)
        else:
            conn = connection_string_for(dev_id, single_device=len(devices) == 1)
            if not conn:
                logging.error("No connection string for %s - see .env.example", dev_id)
                return 2
            transport = AzureSdkTransport(conn, dev_id)
        sims.append(VirtualDevice(dev_id, cfg, transport, budget, stop, args.interval))

    controls = Controls(sims[0])  # shortcuts and scenarios drive the first device
    if len(sims) > 1 and (scenario or not args.headless):
        logging.info("Keyboard shortcuts and scenarios control %s only", sims[0].device_id)

    threads = [threading.Thread(target=s.run, kwargs={"max_messages": args.count},
                                name=s.device_id, daemon=True) for s in sims]
    for t in threads:
        t.start()
    if not args.headless:
        start_keyboard(controls, stop)
    if scenario:
        stop.wait(1.0)  # let the device connect first
        start_scenario(ScenarioRunner(scenario, controls, stop))
    while any(t.is_alive() for t in threads):
        for t in threads:
            t.join(timeout=0.5)
    return 0


def cmd_scenarios(args) -> int:
    for name in available():
        sc = load_scenario(name)
        speed = f" (speed {sc.speed:g}x)" if sc.speed else ""
        print(f"{name:14} {sc.description}{speed}")
    return 0


def cmd_budget(args) -> int:
    cfg = load_config(args.config)
    b = BudgetGuard(cfg.state_dir, cfg.stop_at, cfg.daily_limit)
    print(f"Messages sent today (UTC): {b.used} / {cfg.daily_limit}  (simulator stops at {cfg.stop_at})")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="EMS-1000 virtual device simulator")
    p.add_argument("--config", help="path to YAML config (default: config/default.yaml)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="start the simulated device(s)")
    r.add_argument("--device", help="run only this device id")
    r.add_argument("--interval", type=int, help="heartbeat seconds (overrides config)")
    r.add_argument("--count", type=int, help="stop after N messages per device")
    r.add_argument("--dry-run", action="store_true", help="print payloads, no IoT Hub")
    r.add_argument("--speed", type=float,
                   help="engine seconds per real second, e.g. 60 = 1 h per minute (default 1)")
    r.add_argument("--scenario", help="play a scenario: name in config/scenarios or a .yaml path")
    r.add_argument("--headless", action="store_true",
                   help="no keyboard shortcuts (containers, CI, background runs)")
    r.set_defaults(fn=cmd_run)

    sc = sub.add_parser("scenarios", help="list the scenarios")
    sc.set_defaults(fn=cmd_scenarios)

    b = sub.add_parser("budget", help="show today's message count")
    b.set_defaults(fn=cmd_budget)

    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
    )
    if not args.verbose:
        logging.getLogger("azure").setLevel(logging.WARNING)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

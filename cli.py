#!/usr/bin/env python3
"""EMS-1000 virtual device simulator - command line entry point.

Examples:
  python cli.py run --dry-run --interval 2 --count 3   # offline check, prints payloads
  python cli.py run                                     # live, 60 s heartbeat
  python cli.py run --interval 10                       # demo mode
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
from sim.device import VirtualDevice
from sim.transport import AzureSdkTransport, DryRunTransport


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    if args.interval is not None and args.interval < 1:
        logging.error("--interval must be at least 1 second")
        return 2
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

    threads = [threading.Thread(target=s.run, kwargs={"max_messages": args.count},
                                name=s.device_id, daemon=True) for s in sims]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        for t in threads:
            t.join(timeout=0.5)
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
    r.set_defaults(fn=cmd_run)

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

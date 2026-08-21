from __future__ import annotations

import argparse
import json
from .app.config import load_settings
from .broker.paper import PaperBroker
from .storage.database import Database


def main() -> int:
    parser = argparse.ArgumentParser(description="BISTBOT architecture skeleton (paper-only)")
    parser.add_argument("--config", default="config.yaml"); parser.add_argument("--capital", type=float)
    parser.add_argument("--status", action="store_true"); parser.add_argument("--once", action="store_true")
    args = parser.parse_args(); settings = load_settings(args.config, args.capital)
    with Database(settings.database):
        broker = PaperBroker(settings.capital, settings.execution.commission_pct, settings.execution.slippage_pct)
        if args.status:
            print(json.dumps({"mode":"paper","cash":broker.cash(),"portfolio_value":broker.portfolio_value()})); return 0
        if args.once:
            print(json.dumps({"status":"ready","message":"No strategy implementation is configured"})); return 0
        parser.print_help(); return 0


if __name__ == "__main__": raise SystemExit(main())


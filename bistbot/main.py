from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .app.config import load_settings
from .app.runtime import BistBotApplication
from .app.scheduler import Scheduler
from .broker.paper import PaperBroker
from .notifications.base import SafeNotificationDispatcher
from .notifications.email import EmailNotificationProvider
from .notifications.macos import MacOSNotificationProvider
from .notifications.telegram import TelegramNotificationProvider
from .storage.database import Database


def build_parser() -> argparse.ArgumentParser:
    parser=argparse.ArgumentParser(description="BISTBOT V1 paper trader")
    parser.add_argument("--config",default="config.yaml"); parser.add_argument("--capital",type=float)
    modes=parser.add_mutually_exclusive_group()
    modes.add_argument("--once",action="store_true"); modes.add_argument("--dry-run",action="store_true")
    modes.add_argument("--status",action="store_true"); modes.add_argument("--report",action="store_true")
    modes.add_argument("--reset",action="store_true")
    parser.add_argument("--verbose",action="store_true",help="print detailed diagnostics with --once")
    return parser


def print_verbose_diagnostics(diagnostics: dict) -> None:
    print("TOP 40 SCANNER CANDIDATES")
    for item in diagnostics.get("scanner",[]):
        print(f"{item['rank']:>2}. {item['symbol']} scanner_score={item['scanner_score']:.2f} "
              f"technical_score={item['technical_score']:.2f} momentum_score={item['momentum_score']:.2f} "
              f"volume_score={item['volume_score']:.2f} trend_score={item['trend_score']:.2f} "
              f"liquidity_score={item['liquidity_score']:.2f}")
    print("\nTOP 10 INTELLIGENCE / LLM CANDIDATES")
    for item in diagnostics.get("candidates",[]):
        print(f"\n{item['symbol']}")
        for key in ("scanner_score","news_kap_score","sentiment","importance","catalyst_score",
                    "priced_in_probability","risk_score","confidence","action_bias","final_score","decision","reason"):
            print(f"{key}: {item[key]}")
    holds=[item for item in diagnostics.get("candidates",[]) if item["decision"]=="HOLD"]
    print("\nHOLD REASON")
    if not holds: print("none")
    for item in holds:
        print(f"\n{item['symbol']}\nfinal_score: {item['final_score']}\ndecision: HOLD\nreason: {item['reason']}")
    print("\nRISK DECISION")
    risks=diagnostics.get("risk",[])
    if not risks: print("none (no executable BUY/SELL signal)")
    for item in risks:
        print(f"\n{item['symbol']}\nproposed_position_value: {item['proposed_position_value']}"
              f"\nproposed_quantity: {item['proposed_quantity']}\nstop_price: {item['stop_price']}"
              f"\nmaximum_allowed_risk: {item['maximum_allowed_risk']}\nrisk_decision: {item['risk_decision']}"
              f"\nreason_code: {item['reason_code']}\nreason: {item['reason']}")
    print()


def main() -> int:
    parser=build_parser()
    args=parser.parse_args(); logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings=load_settings(args.config,args.capital); database=Database(settings.database)
    try:
        if args.reset:
            database.close(); path=Path(settings.database)
            if path.exists(): path.unlink()
            print("Paper portfolio reset"); return 0
        notifier=SafeNotificationDispatcher([MacOSNotificationProvider(),TelegramNotificationProvider(),EmailNotificationProvider()])
        broker=PaperBroker(settings.capital,settings.execution.commission_pct,settings.execution.slippage_pct,
                           database=database,risk_settings=settings.risk,notifier=notifier)
        if args.status:
            state=broker.get_portfolio_state()
            print(json.dumps({"mode":"paper","cash":str(state.cash),"portfolio_equity":str(state.equity),
                              "positions":{symbol:item.model_dump(mode="json") for symbol,item in state.positions.items()}},indent=2)); return 0
        if args.report:
            state=broker.get_portfolio_state()
            print(json.dumps(state.model_dump(mode="json"),indent=2)); return 0
        app=BistBotApplication(settings,database,broker,notifier)
        if args.once or args.dry_run:
            summary=app.run_cycle(dry_run=args.dry_run)
            if args.once and args.verbose: print_verbose_diagnostics(app.last_diagnostics)
            print(json.dumps(summary,indent=2)); return 0
        # With no mode, the required `python main.py --capital 200000` command is long-running.
        Scheduler(settings.schedule_seconds,lambda:app.run_cycle()).run_forever(); return 0
    except KeyboardInterrupt: return 0
    except Exception as error:
        logging.exception("BISTBOT failed safely")
        try: notifier.system_error(f"{type(error).__name__}: {error}")
        except UnboundLocalError: pass
        return 1
    finally:
        try: database.close()
        except Exception: pass


if __name__=="__main__": raise SystemExit(main())

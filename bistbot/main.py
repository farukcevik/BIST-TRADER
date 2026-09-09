from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from decimal import Decimal
from datetime import datetime,timezone,timedelta

import certifi
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# VS Code's "Run Python File" executes this file as a script rather than as the
# bistbot package. Add only the project root in that mode, then use canonical
# absolute imports in both invocation styles.
if __package__ in {None,""}:
    import sys
    sys.path.insert(0,str(PROJECT_ROOT))

from bistbot.app.config import load_settings
from bistbot.app.runtime import BistBotApplication
from bistbot.app.scheduler import Scheduler
from bistbot.broker.paper import PaperBroker
from bistbot.notifications.base import SafeNotificationDispatcher
from bistbot.notifications.email import EmailNotificationProvider
from bistbot.notifications.macos import MacOSNotificationProvider
from bistbot.notifications.telegram import TelegramNotificationProvider
from bistbot.intelligence.openai_provider import OpenAILLMProvider
from bistbot.storage.database import Database
from bistbot.market.provider import _is_stale_for_bist_session


PROJECT_ENV_FILE = PROJECT_ROOT / ".env"


def load_project_environment() -> bool:
    load_dotenv(dotenv_path=PROJECT_ENV_FILE, override=False)
    # urllib/OpenSSL consults SSL_CERT_FILE when constructing its verified
    # default HTTPS context. Preserve an explicit user setting; otherwise use
    # certifi's maintained CA bundle. Verification and hostname checks remain on.
    os.environ.setdefault("SSL_CERT_FILE",certifi.where())
    return bool(os.getenv("OPENAI_API_KEY"))


def build_parser() -> argparse.ArgumentParser:
    parser=argparse.ArgumentParser(description="BISTBOT V1 paper trader")
    parser.add_argument("--config",default=str(PROJECT_ROOT/"config.yaml")); parser.add_argument("--capital",type=float)
    modes=parser.add_mutually_exclusive_group()
    modes.add_argument("--once",action="store_true"); modes.add_argument("--dry-run",action="store_true")
    modes.add_argument("--status",action="store_true"); modes.add_argument("--report",action="store_true")
    modes.add_argument("--reset",action="store_true")
    modes.add_argument("--diagnose-intelligence",action="store_true",
                       help="probe news/KAP/OpenAI status without trading")
    modes.add_argument("--diagnose-regime",action="store_true",
                       help="probe official macro feeds and regime calculation without trading")
    modes.add_argument("--analyze-symbol",metavar="SYMBOL",
                       help="analyze one BIST symbol without trading")
    parser.add_argument("--verbose",action="store_true",help="print detailed diagnostics with --once")
    return parser


def print_intelligence_diagnostics(diagnostics: dict) -> None:
    print("INTELLIGENCE DIAGNOSTICS")
    for label in ("news","kap"):
        item=diagnostics[label]
        print(f"\n{label.upper()}")
        for key in ("provider","mode","status","endpoint","request_timestamp","http_status","response_size",
                    "raw_events","parsed_events","mapped_events","symbols_matched","latency_ms","retry_count","latest_event",
                    "error_type","error_message"):
            print(f"{key}: {item.get(key)}")
    item=diagnostics["openai"]
    print("\nOPENAI")
    print(f"provider: {item['provider']}\nstatus: {item['status']}\nmodel: {item.get('model')}")
    print(f"api_key: {'configured' if os.getenv('OPENAI_API_KEY') else 'missing'}")


def print_regime_diagnostics(diagnostics: dict) -> None:
    print("MARKET REGIME DIAGNOSTICS")
    for key in ("provider","provider_status","events_fetched","events_material","events_rejected",
                "events_sent_to_llm","latest_material_event"):
        print(f"{key}: {diagnostics.get(key)}")
    regime=diagnostics["regime"]
    print(f"\nregime: {regime['regime']}\nmarket_risk_score: {regime['market_risk_score']}\nconfidence: {regime['confidence']}")
    for category,value in regime.get("risk_categories",{}).items(): print(f"{category}: {value}")
    print("\nEVENTS")
    if not diagnostics["events"]: print("No events returned by accessible sources.")
    for event in diagnostics["events"]:
        for key in ("provider","source","headline","event_type","published_at","fetched_at","timestamp_source",
                    "age_hours","source_reliability","confirmation_score","materiality","freshness_weight","effective_materiality",
                    "canonical_event_id","llm_analyzed","regime_contribution"):
            print(f"{key}: {event[key]}")
        print()
    print("SIMULATED / NO EXECUTION")


def print_symbol_analysis(result: dict) -> None:
    print(f"SYMBOL\n{result['symbol']}")
    sections=(("MARKET DATA","market_data"),("EVENT MATERIALITY FILTER","event_filter"),
              ("RECENT NEWS","news"),("RECENT KAP","kap"),
              ("LLM","llm"),("STRATEGY","strategy"),("RISK","risk"))
    for title,key in sections:
        print(f"\n{title}")
        value=result.get(key,{})
        print(json.dumps(value,indent=2,ensure_ascii=False,default=str))
    summary=result.get("action_summary")
    if summary:
        print("\nACTION SUMMARY")
        print(f"{summary['symbol']} {summary['latest_price']}")
        print(f"\nTECHNICAL          {summary['technical']['scanner_score']:.2f}  {summary['technical']['strength']}")
        print(f"VOLUME             {summary['volume']['relative_volume']:.2f}x  {summary['volume']['strength']}")
        kap=summary["kap"]
        print(f"\nKAP\n{kap['found']} found\n{kap['low_materiality_ignored']} low-materiality ignored"
              f"\n{kap['material_events_analyzed']} material event(s) analyzed")
        llm=summary["llm"]
        print(f"\nLLM\nInvoked: {llm['invoked']}\nStatus: {llm['status']}\nBias: {llm['bias']}"
              f"\nConfidence: {llm['confidence']}\nCatalyst: {llm['catalyst']}")
        score=summary["score"]
        print(f"\nFINAL SCORE\nTechnical contribution    {score['technical_contribution']:.2f}"
              f"\nNews contribution         {score['news_contribution']:.2f}"
              f"\nLLM contribution          {score['llm_contribution']:.2f}"
              f"\n-------------------------------\nFinal                     {score['final']:.2f}"
              f"\nBuy threshold             {score['buy_threshold']:.2f}")
        print(f"\nDECISION                  {summary['decision']}\nReason                    {summary['reason']}")
    print("\nSIMULATED / NO EXECUTION")


def paper_status(database: Database,settings) -> dict:
    cash=Decimal(database.query("SELECT value FROM metadata WHERE key='paper_cash'")[0]["value"]); positions={}; warnings=[]
    now=datetime.now(timezone.utc)
    for row in database.query("SELECT * FROM paper_positions"):
        columns=set(row.keys())
        symbol=row["symbol"]; quantity=row["quantity"]; average=Decimal(row["average_price"]); last=Decimal(row["last_price"])
        high=Decimal(row["high_price"])
        orders=database.query("SELECT final_score FROM paper_orders WHERE symbol=? AND side='BUY' AND status='FILLED' ORDER BY timestamp DESC LIMIT 1",(symbol,))
        scores=database.query("SELECT score FROM technical_signals WHERE symbol=? ORDER BY timestamp DESC LIMIT 1",(symbol,))
        stop=average*(Decimal("1")-Decimal(str(settings.risk.default_stop_loss_pct)))
        take=average*(Decimal("1")+Decimal(str(settings.risk.default_take_profit_pct)))
        trailing=high*(Decimal("1")-Decimal(str(settings.risk.default_trailing_stop_pct)))
        market_value=last*quantity; unrealized=(last-average)*quantity
        pnl_pct_points=(last/average-1)*Decimal("100")
        raw_data_timestamp=row["data_timestamp"] if "data_timestamp" in columns else None
        stored_status=row["position_status"] if "position_status" in columns else "UNKNOWN"
        stored_score=row["current_score"] if "current_score" in columns else None
        data_timestamp=datetime.fromisoformat(raw_data_timestamp) if raw_data_timestamp else None
        last_update=datetime.fromisoformat(row["updated_at"])
        stale=(stored_status!="FRESH" or data_timestamp is None or
               _is_stale_for_bist_session(now,data_timestamp,timedelta(minutes=settings.risk.max_price_age_minutes)))
        market_status="STALE_MARKET_DATA" if stale else "FRESH"
        if stale: warnings.append(f"{symbol}: open-position market data is stale")
        age_reference=data_timestamp or last_update
        age_seconds=max(0,(now-(age_reference if age_reference.tzinfo else age_reference.replace(tzinfo=timezone.utc))).total_seconds())
        last_known_score=stored_score if stored_score is not None else (scores[0]["score"] if scores else None)
        positions[symbol]={"symbol":symbol,"quantity":quantity,"average_price":str(average),
            "last_price":str(last),"market_value":str(market_value),"unrealized_pnl":str(unrealized),
            "unrealized_pnl_pct":f"{pnl_pct_points:.2f}%","unrealized_pnl_pct_points":str(pnl_pct_points),
            "opened_at":row["opened_at"],"updated_at":row["updated_at"],
            "entry_score":orders[0]["final_score"] if orders else None,
            "current_score":None if stale else last_known_score,"last_known_score":last_known_score,
            "last_update":raw_data_timestamp or row["updated_at"],"data_age_seconds":age_seconds,
            "market_data_status":market_status,"valuation_status":market_status,
            "stop_price":str(stop),"take_profit_price":str(take),"trailing_stop_price":str(trailing),
            "highest_price_since_entry":str(high)}
    equity=cash+sum((Decimal(item["market_value"]) for item in positions.values()),Decimal("0"))
    return {"mode":"paper","cash":str(cash),"portfolio_equity":str(equity),"warnings":warnings,"positions":positions}


def print_verbose_diagnostics(diagnostics: dict) -> None:
    regime=diagnostics.get("market_regime",{}); regime_diag=diagnostics.get("market_regime_diagnostics",{})
    print("MARKET REGIME")
    print(f"Provider status: {regime_diag.get('provider_status','UNAVAILABLE')}")
    for key in ("regime","market_risk_score","confidence","last_updated"): print(f"{key}: {regime.get(key)}")
    for category,value in regime.get("risk_categories",{}).items(): print(f"{category}: {value}")
    behavior=(diagnostics.get("candidates") or [{}])[0]
    for key in ("market_adjustment","adjusted_buy_threshold","position_multiplier"): print(f"{key}: {behavior.get(key)}")
    for key in ("events_fetched","events_material","events_rejected","events_sent_to_llm","latest_material_event"):
        print(f"{key}: {regime_diag.get(key)}")
    print("LATEST MATERIAL MACRO EVENTS")
    for event in regime.get("material_events",[]):
        print(f"{event['published_at']} | {event['source']} | {event['title']} | materiality={event['materiality_score']} "
              f"direction={event['direction']} sectors={event['affected_sectors']}")
    print()
    print("TOP 40 SCANNER CANDIDATES")
    for item in diagnostics.get("scanner",[]):
        print(f"{item['rank']:>2}. {item['symbol']} scanner_score={item['scanner_score']:.2f} "
              f"technical_score={item['technical_score']:.2f} momentum_score={item['momentum_score']:.2f} "
              f"volume_score={item['volume_score']:.2f} trend_score={item['trend_score']:.2f} "
              f"liquidity_score={item['liquidity_score']:.2f}")
        print(f"    latest_price={item['latest_price']} latest_timestamp={item['latest_timestamp']} "
              f"average_volume={item['average_volume']} latest_volume={item['latest_volume']} "
              f"relative_volume={item['relative_volume']} EMA9={item['ema9']} EMA21={item['ema21']} "
              f"RSI14={item['rsi14']} ATR14={item['atr14']} average_turnover_try={item['average_turnover_try']} "
              f"trading_continuity={item['trading_continuity']}")
        print(f"    volume_method={item.get('volume_method','UNKNOWN')} current_interval_volume={item.get('current_interval_volume')} "
              f"historical_comparable_volume={item.get('historical_comparable_volume')}")
    print("\nSCANNER SCORE DISTRIBUTION")
    for name,values in diagnostics.get("score_distribution",{}).items():
        print(f"{name}: "+" ".join(f"{key}={value}" for key,value in values.items()))
    providers=diagnostics.get("providers",{})
    print(f"\nNewsProvider availability: {providers.get('news','UNKNOWN')}")
    print(f"KapProvider availability: {providers.get('kap','UNKNOWN')}")
    print("\nTOP 10 INTELLIGENCE / LLM CANDIDATES")
    for item in diagnostics.get("candidates",[]):
        print(f"\n{item['symbol']}")
        for key in ("scanner_score","news_kap_score","sentiment","importance","catalyst_score",
                    "priced_in_probability","risk_score","confidence","action_bias","model","final_score","decision","reason"):
                print(f"{key}: {item.get(key)}")
        print(f"news_status: {item['news_status']}\nllm_status: {item['llm_status']}\nsignal_mode: {item['signal_mode']}")
        breakdown=item.get("score_breakdown",{})
        if breakdown:
            print("TECHNICAL_PLUS_NEWS SCORE BREAKDOWN")
            for component,value in breakdown.get("contributions",{}).items(): print(f"{component} contribution: {value}")
            print(f"available weights: {breakdown.get('available_weights')}")
            print(f"renormalized weights: {breakdown.get('renormalized_weights')}")
            print(f"missing components: {breakdown.get('missing_components')}")
            print(f"raw weighted sum: {breakdown.get('raw_weighted_sum')}")
            print(f"final score: {breakdown.get('final_score')}")
        plan=item.get("entry_plan") or item.get("potential_plan")
        if plan is not None:
            _print_dynamic_entry_plan(item,plan)
    holds=[item for item in diagnostics.get("candidates",[]) if item["decision"]=="HOLD"]
    print("\nHOLD REASON")
    if not holds: print("none")
    for item in holds:
        print(f"\n{item['symbol']}\nfinal_score: {item.get('final_score')}\ndecision: HOLD\nreason: {item.get('reason')}")
    print("\nRISK DECISION")
    risks=diagnostics.get("risk",[])
    if not risks: print("none (no executable BUY/SELL signal)")
    for item in risks:
        print(f"\n{item['symbol']}\nproposed_position_value: {item['proposed_position_value']}"
              f"\nproposed_quantity: {item['proposed_quantity']}\nstop_price: {item['stop_price']}"
              f"\nmaximum_allowed_risk: {item['maximum_allowed_risk']}\nrisk_decision: {item['risk_decision']}"
              f"\nreason_code: {item['reason_code']}\nreason: {item['reason']}")
    print()


def _print_dynamic_entry_plan(candidate: dict,plan) -> None:
    """Present deterministic plan diagnostics without making execution decisions."""
    if hasattr(plan,"model_dump"): plan=plan.model_dump(mode="json")
    elif not isinstance(plan,dict): plan=dict(plan)
    components=plan.get("target_components") or plan.get("target_components_json") or {}
    if isinstance(components,str):
        try: components=json.loads(components)
        except (TypeError,ValueError): components={}
    print("DYNAMIC ENTRY PLAN")
    fields=(("symbol",candidate.get("symbol")),("entry",plan.get("entry_price")),
        ("stop",plan.get("initial_stop_price") or plan.get("current_stop_price")),
        ("stop_distance_pct",_percentage_points(plan.get("downside_risk_pct"))),("target_1",plan.get("target_1")),
        ("target_2",plan.get("target_2")),("target_3",plan.get("target_3")),
        ("expected_upside_pct",_percentage_points(plan.get("expected_upside_pct"))),("potential_score",plan.get("potential_score")),
        ("risk_reward_ratio",plan.get("risk_reward_ratio")),("holding_horizon",plan.get("holding_horizon")),
        ("target_confidence",plan.get("target_confidence")),("target_method",plan.get("target_method")))
    for label,value in fields: print(f"{label}: {value}")
    print("TARGET COMPONENTS")
    for key in ("resistance_target","swing_high_target","atr_target","trend_extension_target"):
        print(f"{key}: {components.get(key)}")
    print(f"catalyst_adjustment: {components.get('catalyst_adjustment',components.get('catalyst_adjustment_pct'))}")
    decision=candidate.get("decision") or plan.get("decision") or "HOLD"
    print(f"DECISION:\n{decision}")
    if decision=="HOLD": print(f"reason: {candidate.get('reason') or plan.get('reason')}")


def _percentage_points(value):
    try:return None if value is None else round(float(value)*100,4)
    except (TypeError,ValueError):return value


def print_cycle_action_summary(summary: dict,diagnostics: dict) -> None:
    print("CYCLE ACTION SUMMARY")
    print(f"Market data             {summary.get('market_data_success',0)}/{summary.get('symbols_valid',0)} symbols")
    print(f"Scanner candidates      {summary.get('scanner_candidates',0)}")
    print(f"Intelligence candidates {summary.get('llm_candidates',0)}")
    print(f"LLM calls               {summary.get('llm_api_attempts',0)}")
    candidates=diagnostics.get("candidates",[])
    noteworthy=[item for item in candidates if item.get("strategy_decision")!="HOLD" or item.get("decision")!="HOLD"]
    print("\nDECISIONS")
    if not noteworthy: print("No BUY/SELL setup passed the strategy rules.")
    for item in noteworthy:
        effective=item.get("decision","HOLD")
        reason=item.get("reason","")
        if "pyramiding disabled" in reason: effective="MANAGE_EXISTING_POSITION"
        print(f"{item['symbol']}  score={item['final_score']:.2f}  strategy={item.get('strategy_decision')}  action={effective}")
        print(f"Reason: {reason}")
    print(f"\nPaper entry orders      {summary.get('entry_orders',0)}")
    print(f"Paper exit orders       {summary.get('exit_orders',0)}")
    if summary.get("entry_orders",0)==0 and summary.get("raw_buy_signals",0)>0:
        print("No new entry was placed because post-strategy portfolio/risk rules blocked the raw BUY setup.")
    print()


def print_position_cycle_check(diagnostics: dict) -> None:
    print("OPEN POSITION CHECK")
    positions=diagnostics.get("positions",[])
    if not positions: print("No open paper positions.")
    for item in positions:
        print(f"\n{item['symbol']}\nprice: {item['price']}\nentry: {item['entry']}"
              f"\nPnL: {item['unrealized_pnl']} ({item['unrealized_pnl_pct']})"
              f"\nscore: {item['current_score']}\nstop: {item['stop']}\ntake_profit: {item['take_profit']}"
              f"\ntrailing_stop: {item['trailing_stop']}\nhighest_price_since_entry: {item['highest_price_since_entry']}"
              f"\nlast_update: {item['last_update']}\ndata_age_seconds: {item['data_age_seconds']}"
              f"\nmarket_data_status: {item['market_data_status']}\naction: {item['action']}")
        if item["market_data_status"]!="FRESH": print(f"WARNING: {item.get('error') or 'stale open-position price'}")
    print("\nEXIT ORDERS")
    exits=diagnostics.get("exit_orders",[])
    if not exits: print("none")
    for item in exits: print(f"{item['symbol']} quantity={item['quantity']} fill={item['fill_price']} reason={item['reason']}")
    portfolio=diagnostics.get("post_exit_portfolio",{})
    print(f"Post-exit cash: {portfolio.get('cash')}  equity: {portfolio.get('equity')}  open_positions: {portfolio.get('open_positions')}"
          f"  valuation_status: {portfolio.get('valuation_status')}")
    print("\nNEW ENTRY SCANNER")


def main() -> int:
    openai_key_configured=load_project_environment()
    parser=build_parser()
    args=parser.parse_args(); logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings=load_settings(args.config,args.capital)
    if not Path(settings.database).is_absolute():
        settings=settings.model_copy(update={"database":str(PROJECT_ROOT/settings.database)})
    database=Database(":memory:") if (args.diagnose_intelligence or args.diagnose_regime or args.analyze_symbol) else Database(settings.database,read_only=args.status)
    try:
        if args.reset:
            database.close(); path=Path(settings.database)
            if path.exists(): path.unlink()
            print("Paper portfolio reset"); return 0
        if args.status:
            print(json.dumps(paper_status(database,settings),indent=2)); return 0
        notifier=SafeNotificationDispatcher([MacOSNotificationProvider(),TelegramNotificationProvider(),EmailNotificationProvider()])
        broker=PaperBroker(settings.capital,settings.execution.commission_pct,settings.execution.slippage_pct,
                           database=database,risk_settings=settings.risk,notifier=notifier,
                           yahoo_execution_freshness_seconds=settings.market_data.yahoo_execution_freshness_seconds)
        market_status=broker.calendar.status()
        print("\nBIST MARKET STATUS")
        print(f"local_time: {market_status.local_time.isoformat()}")
        print("timezone: Europe/Istanbul")
        print(f"trading_day: {'YES' if market_status.is_trading_day else 'NO'}")
        print(f"session: {market_status.current_session.value}")
        print(f"can_execute_orders: {'YES' if market_status.can_execute_orders else 'NO'}")
        print(f"reason: {market_status.reason}")
        if args.report:
            state=broker.get_portfolio_state()
            print(json.dumps(state.model_dump(mode="json"),indent=2)); return 0
        print(f"OPENAI_API_KEY: {'configured' if openai_key_configured else 'missing'}")
        llm_provider=None
        if settings.llm.enabled and settings.llm.provider.lower()=="openai" and os.getenv("OPENAI_API_KEY"):
            llm_provider=OpenAILLMProvider(model=settings.llm.model,api_key=os.environ["OPENAI_API_KEY"],
                                           timeout_seconds=settings.llm.timeout_seconds)
        llm_model=settings.llm.model if llm_provider else "disabled-v1"
        app=BistBotApplication(settings,database,broker,notifier,llm_provider=llm_provider,llm_model=llm_model)
        for status in app.provider_status(): print(status)
        if args.diagnose_intelligence:
            print_intelligence_diagnostics(app.diagnose_intelligence()); return 0
        if args.diagnose_regime:
            print_regime_diagnostics(app.diagnose_regime()); return 0
        if args.analyze_symbol:
            # The analysis runtime itself stays in-memory. Read only the list of
            # existing paper positions so the summary cannot recommend a duplicate BUY.
            try:
                with Database(settings.database,read_only=True) as portfolio_database:
                    app.diagnostic_existing_positions={row["symbol"] for row in portfolio_database.query("SELECT symbol FROM paper_positions")}
            except Exception:
                app.diagnostic_existing_positions=set()
            print_symbol_analysis(app.analyze_symbol(args.analyze_symbol)); return 0
        if args.once or args.dry_run:
            summary=app.run_cycle(dry_run=args.dry_run)
            if args.once or args.dry_run: print_position_cycle_check(getattr(app,"last_diagnostics",{}))
            if args.once and args.verbose: print_verbose_diagnostics(app.last_diagnostics)
            if args.once: print_cycle_action_summary(summary,getattr(app,"last_diagnostics",{}))
            print(json.dumps(summary,indent=2)); return 0
        # With no mode, the required `python main.py --capital 200000` command is long-running.
        def scheduled_cycle():
            result=app.run_cycle()
            print(json.dumps({key:result.get(key) for key in ("market_closed","company_llm_calls","macro_llm_calls",
                "llm_calls_avoided_by_cache")},ensure_ascii=False))
        Scheduler(settings.schedule_seconds,scheduled_cycle,calendar=broker.calendar,heartbeat_seconds=60).run_forever(); return 0
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

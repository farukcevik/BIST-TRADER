from __future__ import annotations

from datetime import datetime,timezone,timedelta
from decimal import Decimal
import json
import logging

from bistbot.app.config import Settings
from bistbot.app.models import Action,LLMAnalysisInput,RiskOrderRequest,TradeSignal
from bistbot.broker.paper import PaperBroker
from bistbot.fundamental import (FundamentalDataProvider, FundamentalEngine, FundamentalScoringSettings,
    FundamentalProviderStatus, FundamentalSnapshot, KapFundamentalProvider)
from bistbot.intelligence.kap_provider import RealKapProvider,KapProvider
from bistbot.intelligence.catalyst import assess_catalyst
from bistbot.intelligence.llm_provider import LLMAnalyst,LLMCompletion,LLMProvider
from bistbot.intelligence.news_provider import YahooFinanceNewsProvider,NewsProvider
from bistbot.intelligence.ranking import EventRanker
from bistbot.intelligence.materiality import classify_events
from bistbot.market.provider import MarketDataProvider,YahooBistProvider
from bistbot.market.execution_policy import validate_execution_quote
from bistbot.market.scanner import DeterministicMarketScanner
from bistbot.market_regime.engine import MarketRegimeEngine,apply_market_overlay
from bistbot.market_regime.llm import MacroLLMAnalyst
from bistbot.market_regime.provider import MacroNewsProvider,RealMacroNewsProvider
from bistbot.notifications.base import SafeNotificationDispatcher
from bistbot.portfolio.controls import PaperPortfolioControlService
from bistbot.risk.engine import DeterministicRiskEngine,GlobalKillSwitch
from bistbot.storage.database import Database
from bistbot.storage.repositories import (EventRepository,IntelligenceRankingRepository,LLMAnalysisRepository,
    InvestmentAnalysisRepository,MarketRegimeRepository,RiskDecisionRepository,SystemStateRepository,
    TechnicalSignalRepository)
from bistbot.strategy.engine import DeterministicStrategyEngine
from bistbot.strategy.levels import analyze_levels
from bistbot.strategy.potential import assess_potential


class DisabledLLMProvider:
    provider_mode = "DISABLED"
    def complete(self,*,system_prompt: str,input_json: str) -> LLMCompletion:
        raise RuntimeError("No external LLM provider configured")


def _apply_buy_tier_sizing(risk_decision,*,buy_tier:str|None,multiplier:float):
    """Reduce an already risk-approved PAPER quantity; never round upward."""
    normal_quantity=risk_decision.approved_quantity
    tier_multiplier=Decimal(str(multiplier))
    scaled=int(Decimal(normal_quantity)*tier_multiplier)
    return risk_decision.model_copy(update={"approved_quantity":scaled,
        "metadata":{**risk_decision.metadata,"buy_tier":buy_tier,
            "normal_approved_quantity":normal_quantity,
            "buy_tier_position_multiplier":str(tier_multiplier)}})


def _attach_potential_plan(risk_decision,potential):
    """Attach an already validated plan without changing RiskEngine's outcome or sizing."""
    return risk_decision.model_copy(update={"metadata":{**risk_decision.metadata,
        "stop_price":potential.downside_reference,"target_1":potential.target_1,
        "target_2":potential.target_2,"target_3":potential.target_3,
        "t1_exit_pct":25,"t2_exit_pct":25,"remaining_t3_or_trailing_pct":50}})


logger=logging.getLogger(__name__)


class BistBotApplication:
    """Composition root. Exit evaluation always precedes entry scanning."""
    def __init__(self,settings: Settings,database: Database,broker: PaperBroker,notifier: SafeNotificationDispatcher,
                 *,market: MarketDataProvider|None=None,news: NewsProvider|None=None,kap: KapProvider|None=None,
                 llm_provider: LLMProvider|None=None,llm_model: str="disabled-v1",
                 macro_provider: MacroNewsProvider|None=None,
                 fundamental_provider: FundamentalDataProvider|None=None):
        self.settings,self.database,self.broker,self.notifier=settings,database,broker,notifier
        self.market=market or YahooBistProvider(yahoo_execution_freshness_seconds=
            settings.market_data.yahoo_execution_freshness_seconds)
        self.news=news or YahooFinanceNewsProvider()
        self.kap=kap or RealKapProvider(database=database,
            retention_days=settings.intelligence.kap_retention_days,
            base_backoff_seconds=settings.intelligence.kap_base_backoff_seconds,
            max_backoff_seconds=settings.intelligence.kap_max_backoff_seconds)
        self.llm_provider=llm_provider or DisabledLLMProvider()
        self.event_repository=EventRepository(database)
        self.fundamental_provider=fundamental_provider or KapFundamentalProvider(database.path)
        self.fundamental_engine=_build_fundamental_engine(settings)
        self.investment_repository=InvestmentAnalysisRepository(database)
        self.scanner=DeterministicMarketScanner(settings.scanner,TechnicalSignalRepository(database))
        self.ranker=EventRanker(settings.intelligence,self.news,self.kap,self.event_repository,
                                IntelligenceRankingRepository(database))
        self.analyst=LLMAnalyst(self.llm_provider,LLMAnalysisRepository(database),
            model_name=llm_model,max_candidates=settings.analysis_top_n)
        self.strategy=DeterministicStrategyEngine(settings.scoring)
        self.macro_provider=macro_provider or RealMacroNewsProvider()
        self.market_regime=MarketRegimeEngine(settings.market_regime,self.macro_provider)
        self.market_regime_repository=MarketRegimeRepository(database)
        self.macro_analyst=MacroLLMAnalyst(self.llm_provider,self.market_regime_repository,
            model_name=llm_model,threshold=settings.market_regime.macro_llm_materiality_threshold)
        self.risk=DeterministicRiskEngine(settings.risk,RiskDecisionRepository(database),
                                           GlobalKillSwitch(SystemStateRepository(database)))
        self.portfolio_controls=PaperPortfolioControlService(database,mode=settings.trading_mode,
            configured_max_open_positions=settings.risk.max_open_positions)
        self.last_diagnostics: dict={"scanner":[],"candidates":[],"risk":[]}
        self.diagnostic_existing_positions: set[str]=set()

    def provider_status(self) -> list[str]:
        # Probe the real macro pipeline so startup reports actual availability,
        # never a configured-but-unverified STATIC fallback.
        try:self.market_regime.refresh(force=True)
        except Exception:pass
        return [f"MarketDataProvider: {type(self.market).__name__} [{getattr(self.market,'provider_mode','DISABLED')}]",
            f"NewsProvider: {type(self.news).__name__} [{getattr(self.news,'provider_mode','DISABLED')}]",
            f"KapProvider: {type(self.kap).__name__} [{getattr(self.kap,'provider_mode','DISABLED')}]",
            f"LLMProvider: {type(self.llm_provider).__name__} [{getattr(self.llm_provider,'provider_mode','DISABLED')}]",
            f"FundamentalDataProvider: {type(self.fundamental_provider).__name__}",
            f"MacroNewsProvider: {type(self.macro_provider).__name__} [{getattr(self.macro_provider,'status',getattr(self.macro_provider,'provider_mode','UNAVAILABLE'))}]",
            *([f"LLMModel: {self.analyst.model_name}"] if getattr(self.llm_provider,'provider_mode','DISABLED')=="REAL" else [])]

    def diagnose_intelligence(self,symbols: list[str]|None=None) -> dict:
        """Probe intelligence providers only. No scanner, risk, or broker method is called."""
        symbols=symbols or ["GUBRF.IS","CWENE.IS","SASA.IS","TUPRS.IS"]
        result={}
        for name,provider in (("news",self.news),("kap",self.kap)):
            try: provider.fetch(symbols)
            except Exception: pass
            diagnostics=getattr(provider,"last_diagnostics",None)
            result[name]=(diagnostics.model_dump(mode="json") if diagnostics else {
                "provider":type(provider).__name__,"mode":getattr(provider,"provider_mode","DISABLED"),
                "status":getattr(provider,"availability","UNAVAILABLE")})
        result["openai"]={"provider":type(self.llm_provider).__name__,
            "status":"AVAILABLE" if getattr(self.llm_provider,"provider_mode","DISABLED")=="REAL" else "UNAVAILABLE",
            "model":self.analyst.model_name if getattr(self.llm_provider,"provider_mode","DISABLED")=="REAL" else None}
        return result

    def diagnose_regime(self) -> dict:
        """Network/provider and deterministic regime diagnosis. Never reaches risk or broker execution."""
        state=self._analyze_macro(self.market_regime.refresh(force=True)); threshold=self.settings.market_regime.macro_llm_materiality_threshold
        events=[]
        for event in sorted(self.market_regime._events.values(),key=lambda item:item.published_at,reverse=True):
            events.append({"provider":type(self.macro_provider).__name__,"source":event.source,
                "headline":event.title,"event_type":event.event_type,"published_at":event.published_at.isoformat(),
                "fetched_at":event.fetched_at.isoformat(),"timestamp_source":event.timestamp_source.value,
                "age_hours":event.age_hours,"source_reliability":event.source_reliability,
                "confirmation_score":event.confirmation_score,
                "materiality":event.materiality_score,"freshness_weight":event.freshness_weight,
                "effective_materiality":event.effective_materiality,"canonical_event_id":event.canonical_event_id,
                "llm_analyzed":"YES" if event.canonical_event_id in self.macro_analyst.last_analyzed_ids else "NO",
                "regime_contribution":event.regime_contribution,
                "eligible_for_llm":event.materiality_score>=threshold})
        return {"provider":type(self.macro_provider).__name__,"provider_status":self.market_regime.last_diagnostics["provider_status"],
            **self.market_regime.last_diagnostics,"regime":state.model_dump(mode="json"),"events":events,
            "execution":"SIMULATED / NO EXECUTION"}

    def analyze_symbol(self,symbol: str,*,now: datetime|None=None) -> dict:
        """Read-only single-symbol analysis. It never evaluates risk or submits an order."""
        now=now or datetime.now(timezone.utc); symbol=symbol.strip().upper()
        if not symbol.endswith(".IS"): symbol+= ".IS"
        result=self.market.intraday([symbol],bars=max(60,self.settings.scanner.minimum_bars))[symbol]
        if not result.available: return {"symbol":symbol,"market_data":{"available":False,
            "error":result.diagnostics.error_reason},"execution":"SIMULATED / NO EXECUTION"}
        processing_time=result.candles[-1].timestamp
        signal=self.scanner.score(symbol,result.candles,processing_time)
        if signal is None: return {"symbol":symbol,"market_data":{"available":True},
            "strategy":{"decision":"HOLD","reason":"liquidity filter rejected symbol"},
            "execution":"SIMULATED / NO EXECUTION"}
        ranked=self.ranker.rank([signal],limit=1,now=processing_time)[0]
        events=classify_events(self.event_repository.get_by_ids(ranked.event_ids))
        sent_events=[event for event in events if event.materiality_score>=self.settings.intelligence.llm_materiality_threshold]
        candidate=LLMAnalysisInput(symbol=symbol,technical_signal=signal,events=sent_events,
            portfolio_exposure_pct=self._exposure_pct(symbol,{symbol:result}))
        before=self.analyst.api_calls; analysis=self.analyst.analyze([candidate])[0]
        catalyst=assess_catalyst(ranked,analysis,providers_available=self._event_providers_available())
        regime=self._analyze_macro(self.market_regime.refresh())
        fundamental_snapshot,fundamental=self._fundamental(symbol,processing_time)
        overlay=self.market_regime.overlay(symbol,regime); levels=potential=None
        try:
            levels=self._analyze_levels(result.candles)
            entry=float(signal.metrics["latest_price"]); stop=entry*(1-self.settings.risk.default_stop_loss_pct)
            potential=assess_potential(entry,stop,levels,float(signal.metrics["atr_14"]),signal.trend_score,
                signal.momentum_score,float(signal.metrics.get("relative_volume",0)),signal.technical_score,
                fundamental.fundamental_score,ranked.event_score if ranked.event_ids else 0,
                regime.regime,overlay.sector_adjustment)
            decision=self._investment_decision(signal,fundamental,catalyst,potential)
        except (KeyError,TypeError,ValueError):
            decision=None
        market_fields={"available":True,"latest_price":signal.metrics.get("latest_price"),
            "timestamp":signal.timestamp.isoformat(),"ema9":signal.metrics.get("ema_9"),
            "ema21":signal.metrics.get("ema_21"),"rsi14":signal.metrics.get("rsi_14"),
            "atr14":signal.metrics.get("atr_14"),"relative_volume":signal.metrics.get("relative_volume"),
            "volume_method":signal.metrics.get("volume_method"),"technical_score":signal.technical_score,
            "momentum_score":signal.momentum_score,"volume_score":signal.volume_score,
            "trend_score":signal.trend_score,"liquidity_score":signal.liquidity_score,
            "scanner_score":signal.overall_scanner_score}
        def provider_diag(provider):
            value=getattr(provider,"last_diagnostics",None)
            return value.model_dump(mode="json") if value else {"status":getattr(provider,"availability","UNAVAILABLE")}
        rejected=[event for event in events if event not in sent_events]
        contributions={}; technical_contribution=signal.technical_score
        existing_position=(self.broker.get_positions().get(symbol) or
                           (symbol if symbol in self.diagnostic_existing_positions else None))
        if existing_position and decision and decision.action is Action.BUY:
            effective_decision="MANAGE_EXISTING_POSITION"
            effective_reason=(f"BUY setup passed, but {symbol} is already held; pyramiding is disabled. "
                              "Keep managing the existing paper position with configured exits.")
        else:
            effective_decision=decision.action.value if decision else Action.HOLD.value
            effective_reason=decision.reason if decision else "TECHNICAL_LEVELS_UNAVAILABLE"
        action_summary={"symbol":symbol,"latest_price":signal.metrics.get("latest_price"),
            "technical":{"scanner_score":signal.overall_scanner_score,
                "strength":_strength(signal.overall_scanner_score)},
            "volume":{"relative_volume":signal.metrics.get("relative_volume"),
                "strength":_volume_strength(float(signal.metrics.get("relative_volume",0)))},
            "kap":{"found":sum(e.source_type.value=="KAP" for e in events),
                "low_materiality_ignored":sum(e.source_type.value=="KAP" for e in rejected),
                "material_events_analyzed":sum(e.source_type.value=="KAP" for e in sent_events)},
            "llm":{"invoked":"YES" if self.analyst.api_calls>before else "NO","status":analysis.llm_status.value,
                "bias":analysis.action_bias.value,"confidence":analysis.confidence,"catalyst":analysis.catalyst_score},
            "score":{"technical_contribution":round(technical_contribution,4),
                "news_contribution":contributions.get("news_kap",0),"llm_contribution":contributions.get("llm",0),
                "final":decision.final_score if decision else None,"buy_threshold":self.settings.scoring.buy_threshold},
            "decision":effective_decision,"strategy_decision":effective_decision,
            "reason_code":decision.reason_code if decision else "TECHNICAL_LEVELS_UNAVAILABLE","reason":effective_reason,
            "execution":"SIMULATED / NO EXECUTION"}
        return {"symbol":symbol,"market_data":market_fields,
            "event_filter":{"materiality_threshold":self.settings.intelligence.llm_materiality_threshold,
                "events_discovered":len(events),"events_rejected_before_llm":len(rejected),
                "events_sent_to_llm":len(sent_events),
                "rejected":[{"id":e.id,"event_type":e.event_type,"materiality":e.materiality_score,
                    "reason":e.materiality_reason} for e in rejected]},
            "news":{"diagnostics":provider_diag(self.news),"events":[e.model_dump(mode="json") for e in events if e.source_type.value=="NEWS"]},
            "kap":{"diagnostics":provider_diag(self.kap),"events":[e.model_dump(mode="json") for e in events if e.source_type.value=="KAP"]},
            "fundamental":fundamental.model_dump(mode="json"),
            "catalyst":catalyst.model_dump(mode="json"),
            "llm":{"invoked":"YES" if self.analyst.api_calls>before else "NO","model":self.analyst.model_name,
                **analysis.model_dump(mode="json")},"strategy":decision.model_dump(mode="json") if decision else {
                    "action":"HOLD","reason_code":"TECHNICAL_LEVELS_UNAVAILABLE"},
            "technical_levels":({**levels.model_dump(mode="json"),"provenance":{
                "source":"completed OHLCV bars","decision_timestamp":processing_time.isoformat()}}
                if levels else {"status":"UNAVAILABLE"}),
            "potential":({**potential.model_dump(mode="json"),"provenance":{
                "price_source":"completed bar","catalyst_source":"deterministic news/KAP event score"}}
                if potential else {"status":"UNAVAILABLE"}),
            "risk":{"status":"SIMULATED / NO EXECUTION","evaluated":False},
            "market_regime":regime.model_dump(mode="json"),
            "action_summary":action_summary,"execution":"SIMULATED / NO EXECUTION"}

    def run_cycle(self,*,now: datetime|None=None,dry_run: bool=False) -> dict:
        now=now or datetime.now(timezone.utc)
        effective_risk=self.portfolio_controls.effective_risk_settings(self.settings.risk)
        if hasattr(self.risk,"update_settings"): self.risk.update_settings(effective_risk)
        market_status=self.broker.calendar.status(now)
        company_calls_before=self.analyst.api_calls; macro_calls_before=self.macro_analyst.api_calls
        avoided_before=self.analyst.cache_avoided_calls+self.macro_analyst.cache_avoided_calls
        regime=self._analyze_macro(self.market_regime.refresh(),event_driven=not market_status.can_execute_orders)
        if not dry_run:
            for macro_event in regime.material_events: self.market_regime_repository.add_event(macro_event)
            self.market_regime_repository.add_state(regime)
        # Priority 1: independently fetch, score, persist, and evaluate every
        # open paper position. Nothing in the entry pipeline runs before this.
        positions=self.broker.get_positions(); exit_orders=[]; position_diagnostics=[]; exit_prices={}; stale_position_symbols=set()
        exit_price_timestamps={}
        position_results=(self.market.intraday(list(positions),bars=max(60,self.settings.scanner.minimum_bars))
                          if positions else {})
        strategy_exit_symbols=set()
        for symbol,position in positions.items():
            result=position_results.get(symbol); score=None; data_timestamp=None
            if result is not None and result.available:
                data_timestamp=result.snapshot.timestamp; price=Decimal(str(result.snapshot.price)); exit_prices[symbol]=price
                exit_price_timestamps[symbol]=data_timestamp
                try:
                    technical_signal=self.scanner.score(symbol,result.candles,result.candles[-1].timestamp)
                    score=technical_signal.overall_scanner_score if technical_signal else None
                except Exception: score=None
                if score is not None and score<=self.settings.scoring.sell_threshold: strategy_exit_symbols.add(symbol)
                high=max(_position_high(self.database,symbol),price)
                if market_status.can_execute_orders:
                    if not dry_run: high=self.broker.refresh_position(symbol,price,data_timestamp=data_timestamp,current_score=score)
                    status="FRESH"; error=None
                else:
                    status="LAST_CLOSE / MARKET_CLOSED"; error=market_status.reason
            else:
                price=position.last_price; high=_position_high(self.database,symbol); status="STALE_MARKET_DATA"
                stale_position_symbols.add(symbol)
                error=(result.diagnostics.error_reason if result is not None else "market provider returned no result")
                if not dry_run: self.broker.mark_position_stale(symbol)
            pnl=(price-position.average_price)*position.quantity
            pnl_pct=(price/position.average_price-1)*Decimal("100")
            age_seconds=None if data_timestamp is None else max(0,(now-_aware(data_timestamp)).total_seconds())
            stop=_position_stop(self.database,symbol,position.average_price,self.settings.risk.default_stop_loss_pct)
            position_diagnostics.append({"symbol":symbol,"price":str(price),"entry":str(position.average_price),
                "quantity":position.quantity,"unrealized_pnl":str(pnl),"unrealized_pnl_pct":f"{pnl_pct:.2f}%",
                "current_score":score,"stop":str(stop),
                "take_profit":str(position.average_price*(Decimal("1")+Decimal(str(self.settings.risk.default_take_profit_pct)))),
                "trailing_stop":str(high*(Decimal("1")-Decimal(str(self.settings.risk.default_trailing_stop_pct)))),
                "highest_price_since_entry":str(high),"last_update":data_timestamp.isoformat() if data_timestamp else position.updated_at.isoformat(),
                "data_age_seconds":age_seconds,"market_data_status":status,"error":error,
                "action":"LAST CLOSE — EXECUTION BLOCKED" if status=="LAST_CLOSE / MARKET_CLOSED" else
                         "STALE_MARKET_DATA — NO ASSUMPTION" if status!="FRESH" else
                         ("STRATEGY_EXIT" if symbol in strategy_exit_symbols else "HOLD / CHECK PRICE EXITS")})
        if exit_prices and not dry_run:
            exit_orders=self.broker.run_exit_checks(exit_prices,price_timestamps=exit_price_timestamps,strategy_exit_symbols=strategy_exit_symbols,
                now=now,strategy_version=self.settings.strategy_version,update_marks=False,
                execution_provider=getattr(self.market,"provider_name",None))
        exited={order.symbol:order.reason for order in exit_orders}
        for detail in position_diagnostics:
            if detail["symbol"] in exited: detail["action"]=f"PAPER SELL — {exited[detail['symbol']]}"
        # This state is the only state allowed to size new entries.
        post_exit_state=self.broker.get_portfolio_state(exit_prices,now=now,persist=False)
        symbols=self.market.active_symbols()
        market_results=self.market.intraday(symbols,bars=max(60,self.settings.scanner.minimum_bars))
        histories={symbol:result.candles for symbol,result in market_results.items() if result.available}
        processing_time=max((candles[-1].timestamp for candles in histories.values() if candles),default=now)
        top_40=self.scanner.scan(histories,limit=self.settings.scanner_top_n,now=processing_time)
        scanner_diagnostics=[{"rank":rank,"symbol":item.symbol,"scanner_score":item.overall_scanner_score,
            "technical_score":item.technical_score,"momentum_score":item.momentum_score,
            "volume_score":item.volume_score,"trend_score":item.trend_score,
            "liquidity_score":item.liquidity_score,"latest_price":item.metrics.get("latest_price"),
            "latest_timestamp":item.timestamp.isoformat(),"average_volume":item.metrics.get("average_volume"),
            "latest_volume":item.metrics.get("latest_volume"),"relative_volume":item.metrics.get("relative_volume"),
            "ema9":item.metrics.get("ema_9"),"ema21":item.metrics.get("ema_21"),"rsi14":item.metrics.get("rsi_14"),
            "atr14":item.metrics.get("atr_14"),"average_turnover_try":item.metrics.get("average_turnover_try"),
            "trading_continuity":item.metrics.get("trading_continuity"),"volume_method":item.metrics.get("volume_method"),
            "current_interval_volume":item.metrics.get("current_interval_volume"),
            "historical_comparable_volume":item.metrics.get("historical_comparable_volume")} for rank,item in enumerate(top_40,1)]
        top_10=self.ranker.rank(top_40,limit=self.settings.analysis_top_n,now=processing_time)
        technical={item.symbol:item for item in top_40}
        inputs=[LLMAnalysisInput(symbol=item.symbol,technical_signal=technical[item.symbol],
                    events=[event for event in classify_events(self.event_repository.get_by_ids(item.event_ids))
                            if event.materiality_score>=self.settings.intelligence.llm_materiality_threshold],
                    portfolio_exposure_pct=self._exposure_pct(item.symbol,market_results)) for item in top_10]
        analyses=self.analyst.analyze(inputs,event_driven=not market_status.can_execute_orders); decisions=[]; entry_orders=[]; candidate_diagnostics=[]; risk_diagnostics=[]
        ranking={item.symbol:item for item in top_10}
        for candidate,analysis in zip(inputs,analyses):
            fundamental_snapshot,fundamental=self._fundamental(candidate.symbol,processing_time)
            catalyst=assess_catalyst(ranking[candidate.symbol],analysis,
                providers_available=self._event_providers_available())
            overlay=self.market_regime.overlay(candidate.symbol,regime)
            levels=potential=None
            try:
                levels=self._analyze_levels(histories[candidate.symbol])
                entry=float(candidate.technical_signal.metrics["latest_price"])
                stop=entry*(1-self.settings.risk.default_stop_loss_pct)
                potential=assess_potential(entry,stop,levels,
                    float(candidate.technical_signal.metrics["atr_14"]),candidate.technical_signal.trend_score,
                    candidate.technical_signal.momentum_score,
                    float(candidate.technical_signal.metrics.get("relative_volume",0)),
                    candidate.technical_signal.technical_score,fundamental.fundamental_score,
                    ranking[candidate.symbol].event_score if ranking[candidate.symbol].event_ids else 0,
                    regime.regime,overlay.sector_adjustment)
            except (KeyError,TypeError,ValueError) as error:
                logger.warning("Levels/potential unavailable for %s: %s",candidate.symbol,type(error).__name__)
            if not dry_run:
                self.investment_repository.add_fundamental_snapshot(fundamental_snapshot)
                self.investment_repository.add_fundamental_score(fundamental)
                if levels:self.investment_repository.add_technical_levels(levels,provenance={
                    "source":"completed OHLCV bars","decision_timestamp":processing_time.isoformat()})
                if potential:self.investment_repository.add_potential_assessment(potential,
                    symbol=candidate.symbol,timestamp=processing_time,provenance={
                        "price_source":"scanner completed bar","levels_source":"completed OHLCV bars",
                        "catalyst_source":"deterministic news/KAP event score"})
            strategy_decision=(self._investment_decision(candidate.technical_signal,fundamental,catalyst,potential)
                               if potential else None)
            gate_diagnostics=self._investment_gate_diagnostics(candidate.technical_signal,fundamental,potential)
            decisions.append(strategy_decision)
            detail={"symbol":candidate.symbol,"scanner_score":candidate.technical_signal.overall_scanner_score,
                "latest_price":candidate.technical_signal.metrics.get("latest_price"),
                "news_kap_score":ranking[candidate.symbol].event_score,"sentiment":analysis.sentiment,
                "importance":analysis.importance,"catalyst_score":analysis.catalyst_score,
                "priced_in_probability":analysis.priced_in_probability,"risk_score":analysis.risk_score,
                "confidence":analysis.confidence,"action_bias":analysis.action_bias.value,
                "model":self.analyst.model_name if analysis.llm_status.value=="AVAILABLE" else None,
                "llm_status":analysis.llm_status.value,"news_status":ranking[candidate.symbol].intelligence_status.value,
                "fundamental":fundamental.model_dump(mode="json"),
                "fundamental_score":fundamental.fundamental_score,
                "fundamental_confidence":fundamental.confidence,
                "fundamental_provider_status":fundamental.provider_status.value,
                "fundamental_red_flags":[flag.model_dump(mode="json") for flag in fundamental.red_flags],
                "catalyst":catalyst.model_dump(mode="json"),
                "technical_levels":({**levels.model_dump(mode="json"),"provenance":{
                    "source":"completed OHLCV bars","decision_timestamp":processing_time.isoformat()}}
                    if levels else {"status":"UNAVAILABLE"}),
                "potential":({**potential.model_dump(mode="json"),"provenance":{
                    "price_source":"scanner completed bar","levels_source":"completed OHLCV bars",
                    "catalyst_source":"deterministic news/KAP event score"}}
                    if potential else {"status":"UNAVAILABLE"}),
                "final_score":strategy_decision.final_score if strategy_decision else 0,
                "decision":strategy_decision.action.value if strategy_decision else Action.HOLD.value,
                "strategy_decision":strategy_decision.action.value if strategy_decision else Action.HOLD.value,
                "reason_code":strategy_decision.reason_code if strategy_decision else "TECHNICAL_LEVELS_UNAVAILABLE",
                "reason":strategy_decision.reason if strategy_decision else "TECHNICAL_LEVELS_UNAVAILABLE",
                "gate_diagnostics":gate_diagnostics,
                "signal_mode":"STAGED_INVESTMENT"}
            detail.update({"base_stock_score":strategy_decision.final_score if strategy_decision else 0,
                "market_regime":regime.regime.value,"market_adjustment":overlay.market_adjustment,
                "sector_adjustment":overlay.sector_adjustment,
                "adjusted_final_score":strategy_decision.final_score if strategy_decision else 0,
                "adjusted_buy_threshold":self.settings.scoring.buy_threshold,
                "position_multiplier":overlay.position_multiplier})
            detail["score_breakdown"]={"dimensions":{"fundamental":fundamental.fundamental_score,
                "technical":candidate.technical_signal.technical_score,"catalyst":catalyst.catalyst_score,
                "potential":potential.potential_score if potential else None}}
            candidate_diagnostics.append(detail)
            if strategy_decision is None or strategy_decision.action is Action.HOLD: continue
            if overlay.block_new_entries and strategy_decision.action is Action.BUY:
                detail["decision"]=Action.HOLD.value; detail["reason_code"]="MARKET_REGIME_BLOCK"
                detail["reason"]="Market regime blocks new entries"; continue
            if not market_status.can_execute_orders:
                detail["execution"]="BLOCKED"; detail["execution_reason"]=market_status.reason
                detail["decision"]=Action.HOLD.value; detail["reason"]=market_status.reason; continue
            if strategy_decision.action is Action.BUY and stale_position_symbols:
                detail["decision"]=Action.HOLD.value
                detail["reason"]=("new entries blocked: stale market data for open position(s): "+
                                  ", ".join(sorted(stale_position_symbols)))
                continue
            try:
                quote=self.market.latest_execution_quotes([candidate.symbol],
                    max_age=timedelta(minutes=self.settings.risk.max_price_age_minutes)).get(candidate.symbol)
            except Exception: quote=None
            execution_time=quote.fetched_at if quote else now
            validation=(validate_execution_quote(quote,evaluated_at=execution_time,calendar=self.broker.calendar,
                default_freshness_seconds=self.settings.risk.max_price_age_minutes*60,
                yahoo_freshness_seconds=self.settings.market_data.yahoo_execution_freshness_seconds) if quote else None)
            detail.update({"execution_provider":validation.provider if validation else getattr(self.market,"provider_name","UNKNOWN"),
                "execution_quote_age_seconds":validation.quote_age_seconds if validation else None,
                "execution_freshness_limit_seconds":validation.freshness_limit_seconds if validation else self.settings.risk.max_price_age_minutes*60,
                "execution_freshness_status":validation.freshness_status.value if validation else "NO_TIMESTAMP"})
            if validation is None or not validation.valid:
                detail["execution"]="BLOCKED"; detail["execution_reason"]="NO_FRESH_SESSION_PRICE"
                detail["decision"]=Action.HOLD.value; detail["reason"]="NO_FRESH_SESSION_PRICE"; continue
            price_timestamp=validation.source_timestamp; price=validation.price
            proposed_stop=(price*(Decimal("1")-Decimal(str(self.settings.risk.default_stop_loss_pct)))
                           if strategy_decision.action is Action.BUY else None)
            if strategy_decision.action is Action.BUY:
                try:
                    fresh_potential=assess_potential(float(price),float(proposed_stop),levels,
                        float(candidate.technical_signal.metrics["atr_14"]),candidate.technical_signal.trend_score,
                        candidate.technical_signal.momentum_score,
                        float(candidate.technical_signal.metrics.get("relative_volume",0)),
                        candidate.technical_signal.technical_score,fundamental.fundamental_score,
                        ranking[candidate.symbol].event_score if ranking[candidate.symbol].event_ids else 0,
                        regime.regime,overlay.sector_adjustment)
                    strategy_decision=self._investment_decision(candidate.technical_signal,fundamental,catalyst,
                        fresh_potential)
                except (TypeError,ValueError,KeyError):
                    strategy_decision=None; fresh_potential=None
                detail["gate_diagnostics"]=self._investment_gate_diagnostics(
                    candidate.technical_signal,fundamental,fresh_potential)
                if strategy_decision is None or strategy_decision.action is Action.HOLD:
                    detail["decision"]=Action.HOLD.value
                    detail["reason_code"]=(strategy_decision.reason_code if strategy_decision
                                           else "POTENTIAL_REVALIDATION_UNAVAILABLE")
                    detail["reason"]=(strategy_decision.reason if strategy_decision
                                      else "Fresh-quote potential could not be validated")
                    if fresh_potential: detail["potential"]={**fresh_potential.model_dump(mode="json"),
                        "provenance":{"price_source":validation.provider,
                        "price_timestamp":price_timestamp.isoformat(),"stop_source":"exact proposed stop",
                        "catalyst_source":"deterministic news/KAP event score"}}
                    continue
                potential=fresh_potential; detail["potential"]={**potential.model_dump(mode="json"),
                    "provenance":{"price_source":validation.provider,
                    "price_timestamp":price_timestamp.isoformat(),"stop_source":"exact proposed stop",
                    "catalyst_source":"deterministic news/KAP event score"}}
                detail["final_score"]=strategy_decision.final_score
                detail["reason_code"]=strategy_decision.reason_code; detail["reason"]=strategy_decision.reason
                if not dry_run:self.investment_repository.add_potential_assessment(potential,
                    symbol=candidate.symbol,timestamp=price_timestamp,provenance={
                        "price_source":validation.provider,"price_timestamp":price_timestamp.isoformat(),
                        "stop_source":"exact RiskOrderRequest proposed stop",
                        "catalyst_source":"deterministic news/KAP event score"})
                proposed_stop=Decimal(str(potential.downside_reference))
            signal=TradeSignal(timestamp=price_timestamp,symbol=candidate.symbol,action=strategy_decision.action,
                score=strategy_decision.final_score,reason=strategy_decision.reason,
                strategy_version=self.settings.strategy_version,requested_price=float(price),
                buy_tier=strategy_decision.buy_tier)
            state=self.broker.get_portfolio_state({symbol:Decimal(str(result.snapshot.price))
                for symbol,result in market_results.items() if result.available},now=processing_time)
            position=state.positions.get(candidate.symbol)
            if strategy_decision.action is Action.BUY and position is not None:
                detail["decision"]=Action.HOLD.value
                detail["reason"]="existing paper position; pyramiding disabled"
                continue
            request=RiskOrderRequest(signal_id=signal.id,symbol=signal.symbol,action=signal.action,
                entry_price=price,stop_price=proposed_stop,price_timestamp=price_timestamp,
                execution_quote_validation=validation,
                requested_quantity=position.quantity if signal.action is Action.SELL and position else None)
            risk_decision=self.risk.evaluate(request,state,execution_time)
            if signal.action is Action.BUY and risk_decision.approved:
                risk_decision=_attach_potential_plan(risk_decision,potential)
            if signal.action is Action.BUY and risk_decision.approved:
                risk_decision=_apply_buy_tier_sizing(risk_decision,buy_tier=strategy_decision.buy_tier,
                    multiplier=strategy_decision.position_size_multiplier)
            if signal.action is Action.BUY and risk_decision.approved and overlay.position_multiplier<1:
                scaled=int(risk_decision.approved_quantity*overlay.position_multiplier)
                if scaled>0:
                    risk_decision=risk_decision.model_copy(update={"approved_quantity":scaled,
                        "metadata":{**risk_decision.metadata,"market_regime_position_multiplier":overlay.position_multiplier}})
            risk_detail={"symbol":signal.symbol,"proposed_position_value":str(price*risk_decision.approved_quantity),
                "proposed_quantity":risk_decision.approved_quantity,"stop_price":str(request.stop_price) if request.stop_price else None,
                "maximum_allowed_risk":str(state.equity*Decimal(str(self.settings.risk.max_trade_risk_pct))),
                "risk_decision":risk_decision.outcome.value,"reason_code":risk_decision.reason_code.value,
                "reason":risk_decision.reason}
            risk_diagnostics.append(risk_detail)
            if not risk_decision.approved:
                detail["decision"]=Action.HOLD.value
                detail["reason"]=f"RiskEngine rejected: {risk_decision.reason_code.value}"
                self.broker.notify_risk_rejection(signal.symbol,risk_decision); continue
            if signal.action is Action.BUY and risk_decision.approved_quantity<=0:
                detail["decision"]=Action.HOLD.value; detail["reason"]="FLEX size rounds below one share"
                detail["execution"]="BLOCKED"; detail["execution_reason"]="ZERO_TIER_SIZED_QUANTITY"
                continue
            if dry_run or not market_status.can_execute_orders: continue
            if signal.action is Action.BUY: entry_orders.append(self.broker.buy(signal,risk_decision,
                execution_time=execution_time,execution_provider=validation.provider))
            elif position: entry_orders.append(self.broker.sell(signal,risk_decision.approved_quantity,risk_decision,
                execution_time=execution_time,execution_provider=validation.provider))
        state=self.broker.get_portfolio_state(now=processing_time,persist=not dry_run)
        distributions=_score_distributions(self.scanner.last_signals)
        gate_rejection_counts=_gate_rejection_counts(candidate_diagnostics)
        self.last_diagnostics={"positions":position_diagnostics,
                               "exit_orders":[{"symbol":order.symbol,"reason":order.reason,
                                   "quantity":order.quantity,"fill_price":str(order.fill_price)} for order in exit_orders],
                               "post_exit_portfolio":{"cash":str(post_exit_state.cash),"equity":str(post_exit_state.equity),
                                   "open_positions":len(post_exit_state.positions),
                                   "valuation_status":"STALE_MARKET_DATA" if stale_position_symbols else "FRESH"},
                               "scanner":scanner_diagnostics,"candidates":candidate_diagnostics,
                               "gate_rejection_counts":gate_rejection_counts,
                               "risk":risk_diagnostics,"thresholds":{"buy":self.settings.scoring.buy_threshold,
                               "sell":self.settings.scoring.sell_threshold},"score_distribution":distributions,
                               "providers":{"news":getattr(self.news,"availability",getattr(self.news,"provider_mode","DISABLED")),
                               "kap":getattr(self.kap,"availability",getattr(self.kap,"provider_mode","DISABLED"))}}
        self.last_diagnostics["market_regime"]=regime.model_dump(mode="json")
        self.last_diagnostics["market_regime_diagnostics"]=self.market_regime.last_diagnostics
        self.last_diagnostics["bist_market_status"]=market_status.as_dict()
        invalid=getattr(self.market,"invalid_symbols",[])
        failures={symbol:result.diagnostics.error_reason for symbol,result in market_results.items() if not result.available}
        summary={"symbols":len(symbols),"real_symbols_loaded":len(symbols),"symbols_configured":getattr(self.market,"symbols_configured",len(symbols)),
            "symbols_valid":len(symbols),"symbols_invalid":len(invalid),"invalid_symbol_details":invalid,
            "symbols_with_market_data":len(histories),
            "symbols_without_market_data":len(symbols)-len(histories),"invalid_symbols":invalid,
            "market_data_success":len(histories),"market_data_failed":len(symbols)-len(histories),
            "market_data_errors":failures,
            "market_available":len(histories),"scanner_scored_symbols":len(self.scanner.last_signals),"scanner_candidates":len(top_40),
            "open_positions_checked":len(positions),
            "position_refresh_success":len(positions)-len(stale_position_symbols),
            "position_refresh_failed":len(stale_position_symbols),
            "stale_open_positions":sorted(stale_position_symbols),
            "llm_candidates":len(inputs),
            "raw_buy_signals":sum(item is not None and item.action is Action.BUY for item in decisions),
            "raw_sell_signals":sum(item is not None and item.action is Action.SELL for item in decisions),
            "buy_signals":sum(item["decision"]==Action.BUY.value for item in candidate_diagnostics),
            "sell_signals":sum(item["decision"]==Action.SELL.value for item in candidate_diagnostics),
            "holds":sum(item["decision"]==Action.HOLD.value for item in candidate_diagnostics),
            "entry_orders":len(entry_orders),"exit_orders":len(exit_orders),"cash":str(state.cash),
            "portfolio_equity":str(state.equity),"news_provider_status":getattr(self.news,"availability",self.news.provider_mode),
            "kap_provider_status":getattr(self.kap,"availability",self.kap.provider_mode),
            "llm_provider_status":getattr(self.llm_provider,"provider_mode","DISABLED"),
            "llm_api_analyses":self.analyst.api_analyses_completed,"llm_api_attempts":self.analyst.api_calls,
            "market_regime":regime.regime.value,"market_risk_score":regime.market_risk_score,
            "macro_provider_status":self.market_regime.last_diagnostics.get("provider_status","UNAVAILABLE"),
            "market_session_status":market_status.current_session.value,"can_execute_orders":market_status.can_execute_orders,
            "market_session_reason":market_status.reason,
            "market_closed":not market_status.can_execute_orders,
            "company_llm_calls":self.analyst.api_calls-company_calls_before,
            "macro_llm_calls":self.macro_analyst.api_calls-macro_calls_before,
            "llm_calls_avoided_by_cache":self.analyst.cache_avoided_calls+self.macro_analyst.cache_avoided_calls-avoided_before,
            "scanner_score_distribution":distributions,"dry_run":dry_run}
        if not dry_run:
            try:
                self.database.execute("INSERT INTO system_events(timestamp,level,event_type,message,payload) VALUES(?,?,?,?,?)",
                    (now.isoformat(),"INFO","CYCLE_COMPLETED","Paper cycle completed",
                     json.dumps({"summary":summary,"decisions":candidate_diagnostics},ensure_ascii=False)))
            except Exception as error:
                # Dashboard audit persistence must never change a completed PAPER
                # cycle's trading outcome.
                logger.warning("Could not persist dashboard cycle audit: %s",type(error).__name__)
        return summary

    def _exposure_pct(self,symbol: str,market_results: dict) -> float:
        state=self.broker.get_portfolio_state()
        position=state.positions.get(symbol)
        return 0.0 if not position or state.equity<=0 else float(position.market_value/state.equity*100)

    def _analyze_macro(self,state,*,event_driven:bool=False):
        try: analysis=self.macro_analyst.analyze(state.material_events,event_driven=event_driven)
        except Exception as error:
            logger.warning("Macro LLM analysis unavailable; deterministic regime retained: %s",type(error).__name__)
            analysis=None
        self.market_regime.last_diagnostics["events_sent_to_llm"]=len(self.macro_analyst.last_sent_ids)
        if analysis:
            state=state.model_copy(update={"event_summary":analysis.event_summary,
                "expected_market_direction":analysis.expected_market_direction,
                "expected_duration":analysis.expected_duration,"affected_sectors":analysis.affected_sectors,
                "positive_sectors":analysis.positive_sectors,"negative_sectors":analysis.negative_sectors,
                "uncertainty":analysis.uncertainty,"source_ids":analysis.source_ids})
            self.market_regime._state=state
        return state

    def _fundamental(self,symbol: str,as_of: datetime):
        try:
            snapshot=self.fundamental_provider.get_snapshot(symbol,as_of)
        except Exception as error:
            logger.warning("Fundamental provider unavailable for %s: %s",symbol,type(error).__name__)
            snapshot=FundamentalSnapshot(symbol=symbol,as_of=as_of,source=type(self.fundamental_provider).__name__,
                provider_status=FundamentalProviderStatus.UNAVAILABLE)
        return snapshot,self.fundamental_engine.evaluate(snapshot)

    def _event_providers_available(self) -> bool:
        unavailable={"UNAVAILABLE","ERROR","DISABLED"}
        return any(str(getattr(provider,"availability",getattr(provider,"provider_mode","UNAVAILABLE")))
                   not in unavailable for provider in (self.news,self.kap))

    def _analyze_levels(self,bars):
        settings=self.settings.technical_levels
        return analyze_levels(bars,swing_window=settings.swing_window,
            zone_atr_fraction=settings.zone_atr_fraction,dedup_atr_fraction=settings.dedup_atr_fraction,
            tick_size=settings.tick_size)

    def _investment_decision(self,technical,fundamental,catalyst,potential):
        policy=self.settings.investment_policy
        fundamental_policy=self.settings.fundamental
        return self.strategy.evaluate_investment(technical,fundamental,catalyst,potential,
            minimum_risk_reward_ratio=self.settings.strategy.minimum_risk_reward_ratio,
            minimum_fundamental_score=fundamental_policy.minimum_score,
            minimum_technical_score=policy.minimum_technical_score,
            unavailable_policy=fundamental_policy.unavailable_policy,
            fallback_minimum_risk_reward_ratio=fundamental_policy.fallback_minimum_risk_reward_ratio,
            fallback_minimum_technical_score=fundamental_policy.fallback_minimum_technical_score,
            fallback_minimum_potential_confidence=fundamental_policy.fallback_minimum_potential_confidence,
            speculative_technical_score=policy.speculative_technical_score,
            speculative_catalyst_score=policy.speculative_catalyst_score,
            minimum_fundamental_confidence=fundamental_policy.minimum_confidence,
            minimum_catalyst_score=self.settings.strategy.minimum_catalyst_score,
            require_catalyst_confirmation=self.settings.strategy.require_catalyst_confirmation,
            paper_mode=self.settings.trading_mode=="PAPER")

    def _investment_gate_diagnostics(self,technical,fundamental,potential):
        policy=self.settings.investment_policy
        fundamental_policy=self.settings.fundamental
        try:
            return self.strategy.diagnose_investment_gates(technical,fundamental,potential,
                minimum_risk_reward_ratio=self.settings.strategy.minimum_risk_reward_ratio,
                minimum_fundamental_score=fundamental_policy.minimum_score,
                minimum_technical_score=policy.minimum_technical_score,
                unavailable_policy=fundamental_policy.unavailable_policy,
                fallback_minimum_risk_reward_ratio=fundamental_policy.fallback_minimum_risk_reward_ratio,
                fallback_minimum_technical_score=fundamental_policy.fallback_minimum_technical_score,
                fallback_minimum_potential_confidence=fundamental_policy.fallback_minimum_potential_confidence,
                minimum_fundamental_confidence=fundamental_policy.minimum_confidence,
                paper_mode=self.settings.trading_mode=="PAPER")
        except Exception as error:
            logger.warning("Investment gate diagnostics unavailable for %s: %s",
                getattr(technical,"symbol","UNKNOWN"),type(error).__name__)
            unavailable={"status":"UNAVAILABLE","passed":None,"blocking_gates":[],
                **{name:{"passed":None,"actual":None,"threshold":None,"blocking_gate":None}
                   for name in ("fundamental","confidence","technical","rr")}}
            return {"status":"UNAVAILABLE","error":type(error).__name__,
                "NORMAL":dict(unavailable),"FLEX":dict(unavailable)}


def _build_fundamental_engine(settings: Settings) -> FundamentalEngine:
    fundamental=settings.fundamental
    return FundamentalEngine(FundamentalScoringSettings(weights=fundamental.weights,
        minimum_confidence=fundamental.minimum_confidence))


def _gate_rejection_counts(candidates: list[dict]) -> dict[str,int]:
    counts={}
    for detail in candidates:
        for tier in ("NORMAL","FLEX"):
            tier_diagnostics=detail.get("gate_diagnostics",{}).get(tier,{})
            for gate_name in ("fundamental","confidence","technical","rr"):
                gate=tier_diagnostics.get(gate_name,{})
                if gate.get("passed") is False:
                    key=f"{tier}.{gate_name}"
                    counts[key]=counts.get(key,0)+1
    return counts


def _score_distributions(signals) -> dict:
    fields=("technical_score","momentum_score","volume_score","trend_score","liquidity_score","overall_scanner_score")
    def percentile(values,p):
        if not values:return None
        position=(len(values)-1)*p; lower=int(position); upper=min(lower+1,len(values)-1); fraction=position-lower
        return round(values[lower]*(1-fraction)+values[upper]*fraction,4)
    return {field:{"min":min(values),"p25":percentile(values,.25),"median":percentile(values,.5),
        "p75":percentile(values,.75),"p90":percentile(values,.9),"p95":percentile(values,.95),"max":max(values)}
        for field in fields if (values:=sorted(float(getattr(item,field)) for item in signals))}


def _strength(score: float) -> str:
    return "STRONG" if score>=70 else "MODERATE" if score>=55 else "WEAK"


def _volume_strength(relative_volume: float) -> str:
    return "STRONG" if relative_volume>=1.5 else "NORMAL" if relative_volume>=.8 else "WEAK"


def _position_high(database: Database,symbol: str) -> Decimal:
    return Decimal(database.query("SELECT high_price FROM paper_positions WHERE symbol=?",(symbol,))[0]["high_price"])


def _position_stop(database: Database,symbol: str,average_price: Decimal,default_stop_loss_pct: float) -> Decimal:
    row=database.query("SELECT stop_price FROM paper_positions WHERE symbol=?",(symbol,))[0]
    return (Decimal(row["stop_price"]) if row["stop_price"] is not None else
            average_price*(Decimal("1")-Decimal(str(default_stop_loss_pct))))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

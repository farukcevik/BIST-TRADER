from __future__ import annotations

from datetime import datetime,timezone
from decimal import Decimal

from bistbot.app.config import Settings
from bistbot.app.models import Action,LLMAnalysisInput,RiskOrderRequest,TradeSignal
from bistbot.broker.paper import PaperBroker
from bistbot.intelligence.kap_provider import DisabledKapProvider,KapProvider
from bistbot.intelligence.llm_provider import LLMAnalyst,LLMCompletion,LLMProvider
from bistbot.intelligence.news_provider import DisabledNewsProvider,NewsProvider
from bistbot.intelligence.ranking import EventRanker
from bistbot.market.provider import MarketDataProvider,YahooBistProvider
from bistbot.market.scanner import DeterministicMarketScanner
from bistbot.notifications.base import SafeNotificationDispatcher
from bistbot.risk.engine import DeterministicRiskEngine,GlobalKillSwitch
from bistbot.storage.database import Database
from bistbot.storage.repositories import (EventRepository,IntelligenceRankingRepository,LLMAnalysisRepository,
    RiskDecisionRepository,SystemStateRepository,TechnicalSignalRepository)
from bistbot.strategy.engine import DeterministicStrategyEngine


class DisabledLLMProvider:
    provider_mode = "DISABLED"
    def complete(self,*,system_prompt: str,input_json: str) -> LLMCompletion:
        raise RuntimeError("No external LLM provider configured")


class BistBotApplication:
    """Composition root. Exit evaluation always precedes entry scanning."""
    def __init__(self,settings: Settings,database: Database,broker: PaperBroker,notifier: SafeNotificationDispatcher,
                 *,market: MarketDataProvider|None=None,news: NewsProvider|None=None,kap: KapProvider|None=None,
                 llm_provider: LLMProvider|None=None,llm_model: str="disabled-v1"):
        self.settings,self.database,self.broker,self.notifier=settings,database,broker,notifier
        self.market=market or YahooBistProvider(); self.news=news or DisabledNewsProvider(); self.kap=kap or DisabledKapProvider()
        self.llm_provider=llm_provider or DisabledLLMProvider()
        self.event_repository=EventRepository(database)
        self.scanner=DeterministicMarketScanner(settings.scanner,TechnicalSignalRepository(database))
        self.ranker=EventRanker(settings.intelligence,self.news,self.kap,self.event_repository,
                                IntelligenceRankingRepository(database))
        self.analyst=LLMAnalyst(self.llm_provider,LLMAnalysisRepository(database),
            model_name=llm_model,max_candidates=settings.analysis_top_n)
        self.strategy=DeterministicStrategyEngine(settings.scoring)
        self.risk=DeterministicRiskEngine(settings.risk,RiskDecisionRepository(database),
                                           GlobalKillSwitch(SystemStateRepository(database)))
        self.last_diagnostics: dict={"scanner":[],"candidates":[],"risk":[]}

    def provider_status(self) -> list[str]:
        return [f"MarketDataProvider: {type(self.market).__name__} [{getattr(self.market,'provider_mode','DISABLED')}]",
            f"NewsProvider: {type(self.news).__name__} [{getattr(self.news,'provider_mode','DISABLED')}]",
            f"KapProvider: {type(self.kap).__name__} [{getattr(self.kap,'provider_mode','DISABLED')}]",
            f"LLMProvider: {type(self.llm_provider).__name__} [{getattr(self.llm_provider,'provider_mode','DISABLED')}]",
            *([f"LLMModel: {self.analyst.model_name}"] if getattr(self.llm_provider,'provider_mode','DISABLED')=="REAL" else [])]

    def run_cycle(self,*,now: datetime|None=None,dry_run: bool=False) -> dict:
        now=now or datetime.now(timezone.utc)
        # Priority 1: refresh and evaluate every open position before scanning entries.
        positions=self.broker.get_positions(); exit_orders=[]
        if positions:
            exit_data=self.market.snapshots(list(positions))
            exit_prices={symbol:Decimal(str(result.snapshot.price)) for symbol,result in exit_data.items() if result.available}
            if not dry_run: exit_orders=self.broker.run_exit_checks(exit_prices,now=now,strategy_version=self.settings.strategy_version)
        symbols=self.market.active_symbols()
        market_results=self.market.intraday(symbols,bars=max(60,self.settings.scanner.minimum_bars))
        histories={symbol:result.candles for symbol,result in market_results.items() if result.available}
        processing_time=max([now]+[candles[-1].timestamp for candles in histories.values() if candles])
        top_40=self.scanner.scan(histories,limit=self.settings.scanner_top_n,now=processing_time)
        scanner_diagnostics=[{"rank":rank,"symbol":item.symbol,"scanner_score":item.overall_scanner_score,
            "technical_score":item.technical_score,"momentum_score":item.momentum_score,
            "volume_score":item.volume_score,"trend_score":item.trend_score,
            "liquidity_score":item.liquidity_score,"latest_price":item.metrics.get("latest_price"),
            "latest_timestamp":item.timestamp.isoformat(),"average_volume":item.metrics.get("average_volume"),
            "latest_volume":item.metrics.get("latest_volume"),"relative_volume":item.metrics.get("relative_volume"),
            "ema9":item.metrics.get("ema_9"),"ema21":item.metrics.get("ema_21"),"rsi14":item.metrics.get("rsi_14"),
            "atr14":item.metrics.get("atr_14"),"average_turnover_try":item.metrics.get("average_turnover_try"),
            "trading_continuity":item.metrics.get("trading_continuity")} for rank,item in enumerate(top_40,1)]
        top_10=self.ranker.rank(top_40,limit=self.settings.analysis_top_n,now=processing_time)
        technical={item.symbol:item for item in top_40}
        inputs=[LLMAnalysisInput(symbol=item.symbol,technical_signal=technical[item.symbol],
                    events=self.event_repository.get_by_ids(item.event_ids),
                    portfolio_exposure_pct=self._exposure_pct(item.symbol,market_results)) for item in top_10]
        analyses=self.analyst.analyze(inputs); decisions=[]; entry_orders=[]; candidate_diagnostics=[]; risk_diagnostics=[]
        ranking={item.symbol:item for item in top_10}
        for candidate,analysis in zip(inputs,analyses):
            strategy_decision=self.strategy.evaluate(candidate.technical_signal,ranking[candidate.symbol],analysis)
            decisions.append(strategy_decision)
            detail={"symbol":candidate.symbol,"scanner_score":candidate.technical_signal.overall_scanner_score,
                "news_kap_score":ranking[candidate.symbol].event_score,"sentiment":analysis.sentiment,
                "importance":analysis.importance,"catalyst_score":analysis.catalyst_score,
                "priced_in_probability":analysis.priced_in_probability,"risk_score":analysis.risk_score,
                "confidence":analysis.confidence,"action_bias":analysis.action_bias.value,
                "llm_status":analysis.llm_status.value,"news_status":ranking[candidate.symbol].intelligence_status.value,
                "final_score":strategy_decision.final_score,"decision":strategy_decision.action.value,
                "reason":strategy_decision.reason,"signal_mode":strategy_decision.signal_mode.value}
            candidate_diagnostics.append(detail)
            if strategy_decision.action is Action.HOLD: continue
            price=Decimal(str(market_results[candidate.symbol].snapshot.price))
            signal=TradeSignal(timestamp=processing_time,symbol=candidate.symbol,action=strategy_decision.action,
                score=strategy_decision.final_score,reason=strategy_decision.reason,
                strategy_version=self.settings.strategy_version,requested_price=float(price))
            state=self.broker.get_portfolio_state({symbol:Decimal(str(result.snapshot.price))
                for symbol,result in market_results.items() if result.available},now=processing_time)
            position=state.positions.get(candidate.symbol)
            request=RiskOrderRequest(signal_id=signal.id,symbol=signal.symbol,action=signal.action,
                entry_price=price,stop_price=(price*(Decimal("1")-Decimal(str(self.settings.risk.default_stop_loss_pct)))
                if signal.action is Action.BUY else None),price_timestamp=processing_time,
                requested_quantity=position.quantity if signal.action is Action.SELL and position else None)
            risk_decision=self.risk.evaluate(request,state,processing_time)
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
            if dry_run: continue
            if signal.action is Action.BUY: entry_orders.append(self.broker.buy(signal,risk_decision))
            elif position: entry_orders.append(self.broker.sell(signal,risk_decision.approved_quantity,risk_decision))
        state=self.broker.get_portfolio_state(now=processing_time,persist=not dry_run)
        self.last_diagnostics={"scanner":scanner_diagnostics,"candidates":candidate_diagnostics,
                               "risk":risk_diagnostics,"thresholds":{"buy":self.settings.scoring.buy_threshold,
                               "sell":self.settings.scoring.sell_threshold}}
        invalid=getattr(self.market,"invalid_symbols",[])
        failures={symbol:result.diagnostics.error_reason for symbol,result in market_results.items() if not result.available}
        return {"symbols":len(symbols),"real_symbols_loaded":len(symbols),"invalid_symbols":invalid,
            "market_data_success":len(histories),"market_data_failed":len(symbols)-len(histories),
            "market_data_errors":failures,
            "market_available":len(histories),"scanner_candidates":len(top_40),
            "llm_candidates":len(inputs),"buy_signals":sum(item.action is Action.BUY for item in decisions),
            "sell_signals":sum(item.action is Action.SELL for item in decisions),"holds":sum(item.action is Action.HOLD for item in decisions),
            "entry_orders":len(entry_orders),"exit_orders":len(exit_orders),"cash":str(state.cash),
            "portfolio_equity":str(state.equity),"dry_run":dry_run}

    def _exposure_pct(self,symbol: str,market_results: dict) -> float:
        state=self.broker.get_portfolio_state()
        position=state.positions.get(symbol)
        return 0.0 if not position or state.equity<=0 else float(position.market_value/state.equity*100)

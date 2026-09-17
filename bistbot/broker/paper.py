from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime,timedelta,timezone
from decimal import Decimal,ROUND_HALF_UP
from types import SimpleNamespace
from uuid import uuid4

from bistbot.app.config import RiskSettings
from bistbot.app.models import (Action,ExitReason,OrderStatus,PaperFill,PaperOrder,RiskDecision,
                                RiskOutcome,RiskReasonCode,TradeSignal)
from bistbot.notifications.base import SafeNotificationDispatcher
from bistbot.market.calendar import BistTradingCalendar,MarketClosedError
from bistbot.market.execution_policy import DEFAULT_EXECUTION_FRESHNESS_SECONDS,validate_execution_quote
from bistbot.portfolio.service import PortfolioPosition,money
from bistbot.portfolio.service import PortfolioState
from bistbot.storage.database import Database
from bistbot.storage.paper_reset import DATABASE_ENVIRONMENT_KEY,assert_paper_database_adoptable
from bistbot.storage.repositories import RiskDecisionRepository,SystemStateRepository

PRICE_STEP=Decimal("0.0001")

def _aware_utc(value:datetime)->datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class PaperBroker:
    """The only V1 execution adapter. All state is virtual and SQLite-backed."""
    def __init__(self,starting_cash: Decimal|str|int|float=Decimal("200000"),commission_pct: float=0,
                 slippage_pct: float=0,*,database: Database|None=None,risk_settings: RiskSettings|None=None,
                 notifier: SafeNotificationDispatcher|None=None,calendar: BistTradingCalendar|None=None,
                 yahoo_execution_freshness_seconds:int|None=None,clock:Callable[[],datetime]|None=None):
        self.database=database or Database(":memory:"); self.risk_settings=risk_settings
        self.commission_pct=Decimal(str(commission_pct)); self.slippage_pct=Decimal(str(slippage_pct))
        self.notifier=notifier or SafeNotificationDispatcher()
        self.calendar=calendar or BistTradingCalendar()
        self.yahoo_execution_freshness_seconds=yahoo_execution_freshness_seconds
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        capital=money(starting_cash)
        if capital<=0 or self.commission_pct<0 or self.slippage_pct<0: raise ValueError("invalid paper broker configuration")
        connection=self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            assert_paper_database_adoptable(connection)
            connection.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES(?, 'PAPER')",
                               (DATABASE_ENVIRONMENT_KEY,))
            connection.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('paper_cash',?)",(str(capital),))
            connection.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('paper_initial_capital',?)",(str(capital),))
            connection.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('paper_realized_pnl','0.00')")
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def get_cash(self) -> Decimal:
        return money(self.database.query("SELECT value FROM metadata WHERE key='paper_cash'")[0]["value"])

    def set_cash(self,amount,*,expected_version:int,idempotency_token:str,actor:str="api"):
        from bistbot.portfolio.controls import PaperPortfolioControlService
        configured=self.risk_settings.max_open_positions if self.risk_settings else 1
        return PaperPortfolioControlService(self.database,mode="PAPER",
            configured_max_open_positions=configured).set_cash(amount,expected_version=expected_version,
                idempotency_token=idempotency_token,actor=actor)

    def get_positions(self) -> dict[str,PortfolioPosition]:
        positions={}
        for row in self.database.query("SELECT * FROM paper_positions"):
            positions[row["symbol"]]=PortfolioPosition(symbol=row["symbol"],quantity=row["quantity"],
                average_price=Decimal(row["average_price"]),last_price=Decimal(row["last_price"]),
                opened_at=datetime.fromisoformat(row["opened_at"]),updated_at=datetime.fromisoformat(row["updated_at"]))
        return positions

    def get_orders(self) -> list[PaperOrder]:
        return [PaperOrder.model_validate_json(row["payload"]) for row in self.database.query("SELECT payload FROM paper_orders ORDER BY timestamp,id")]

    def get_portfolio_value(self,prices: dict[str,Decimal]|None=None) -> Decimal:
        prices=prices or {}
        return money(self.get_cash()+sum((position.quantity*Decimal(str(prices.get(symbol,position.last_price)))
                     for symbol,position in self.get_positions().items()),Decimal("0")))

    def get_portfolio_state(self,prices: dict[str,Decimal]|None=None,*,now: datetime|None=None,
                            persist: bool=False) -> PortfolioState:
        now=now or datetime.now(timezone.utc); prices=prices or {}; positions=self.get_positions()
        marked={symbol:position.model_copy(update={"last_price":Decimal(str(prices.get(symbol,position.last_price))),
                "updated_at":now}) for symbol,position in positions.items()}
        cash=self.get_cash(); equity=money(cash+sum((p.market_value for p in marked.values()),Decimal("0")))
        unrealized=money(sum((p.unrealized_pnl for p in marked.values()),Decimal("0")))
        realized=money(self.database.query("SELECT value FROM metadata WHERE key='paper_realized_pnl'")[0]["value"])
        initial=money(self.database.query("SELECT value FROM metadata WHERE key='paper_initial_capital'")[0]["value"])
        rows=self.database.query("SELECT timestamp,equity FROM paper_portfolio_snapshots ORDER BY timestamp")
        history=[(datetime.fromisoformat(row["timestamp"]),money(row["equity"])) for row in rows]
        peak=max([initial,equity]+[value for _,value in history])
        def baseline(weekly: bool):
            same=[(ts,value) for ts,value in history if (ts.isocalendar()[:2]==now.isocalendar()[:2] if weekly else ts.date()==now.date())]
            earlier=[(ts,value) for ts,value in history if ts<now]
            if same: return min(same,key=lambda item:item[0])[1]
            return max(earlier,key=lambda item:item[0])[1] if earlier else initial
        state=PortfolioState(as_of=now,initial_capital=initial,cash=cash,positions=marked,realized_pnl=realized,
            unrealized_pnl=unrealized,equity=equity,daily_pnl=money(equity-baseline(False)),
            weekly_pnl=money(equity-baseline(True)),peak_equity=peak,
            drawdown_pct=Decimal("0") if peak==0 else (peak-equity)/peak)
        if persist:
            self.database.execute("INSERT INTO paper_portfolio_snapshots(timestamp,cash,equity,realized_pnl,unrealized_pnl) VALUES(?,?,?,?,?)",
                (now.isoformat(),str(cash),str(equity),str(realized),str(unrealized)))
        return state

    def buy(self,signal: TradeSignal,risk_decision: RiskDecision,*,execution_time: datetime|None=None,
            execution_provider:str|None=None) -> PaperOrder:
        if signal.action is not Action.BUY: raise ValueError("buy requires BUY signal")
        if risk_decision.signal_id!=signal.id or not risk_decision.approved:
            self.notify_risk_rejection(signal.symbol,risk_decision); raise PermissionError("risk decision does not approve this signal")
        self._guard_execution(signal.timestamp,signal.requested_price,execution_time or signal.timestamp,execution_provider)
        return self._fill(signal,risk_decision.approved_quantity,risk_decision)

    def sell(self,signal: TradeSignal,quantity: int,risk_decision: RiskDecision,*,execution_time: datetime|None=None,
             execution_provider:str|None=None) -> PaperOrder:
        if signal.action is not Action.SELL: raise ValueError("sell requires SELL signal")
        if (risk_decision.signal_id!=signal.id or not risk_decision.approved or
                quantity>risk_decision.approved_quantity): raise PermissionError("sell is not approved")
        self._guard_execution(signal.timestamp,signal.requested_price,execution_time or signal.timestamp,execution_provider)
        return self._fill(signal,quantity,risk_decision,target_hit_column=_target_hit_column)

    def _fill(self,signal: TradeSignal,quantity: int,risk_decision: RiskDecision,*,target_hit_column:str|None=None) -> PaperOrder:
        connection=self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            return self._fill_locked(signal,quantity,risk_decision,target_hit_column=target_hit_column)
        except Exception:
            connection.rollback(); raise

    def _fill_locked(self,signal: TradeSignal,quantity: int,risk_decision: RiskDecision,*,target_hit_column:str|None=None) -> PaperOrder:
        connection=self.database.connection
        if quantity<=0: raise ValueError("quantity must be positive")
        requested=Decimal(str(signal.requested_price)); side_multiplier=(Decimal("1")+self.slippage_pct
            if signal.action is Action.BUY else Decimal("1")-self.slippage_pct)
        fill=(requested*side_multiplier).quantize(PRICE_STEP,rounding=ROUND_HALF_UP)
        commission=money(fill*quantity*self.commission_pct); now=signal.timestamp
        positions=self.get_positions(); old=positions.get(signal.symbol); cash=self.get_cash()
        high_rows=self.database.query("SELECT high_price FROM paper_positions WHERE symbol=?",(signal.symbol,))
        stored_high=Decimal(high_rows[0]["high_price"]) if high_rows else fill
        if signal.action is Action.BUY:
            total=money(fill*quantity+commission)
            if total>cash: raise ValueError("insufficient paper cash")
            new_cash=money(cash-total)
            if old:
                new_quantity=old.quantity+quantity; average=(old.average_price*old.quantity+fill*quantity)/new_quantity
                position=old.model_copy(update={"quantity":new_quantity,"average_price":money(average),
                    "last_price":fill,"updated_at":now})
            else: position=PortfolioPosition(symbol=signal.symbol,quantity=quantity,average_price=money(fill),
                    last_price=fill,opened_at=now,updated_at=now)
        else:
            if not old or quantity>old.quantity: raise ValueError("insufficient paper position")
            new_cash=money(cash+fill*quantity-commission); remaining=old.quantity-quantity
            realized=money((fill-old.average_price)*quantity-commission)
            position=None if remaining==0 else old.model_copy(update={"quantity":remaining,"last_price":fill,"updated_at":now})
        order=PaperOrder(signal_id=signal.id,symbol=signal.symbol,side=signal.action,quantity=quantity,
            requested_price=requested,fill_price=fill,timestamp=now,reason=signal.reason,final_score=signal.score,
            risk_decision=risk_decision,strategy_version=signal.strategy_version,status=OrderStatus.FILLED)
        realized=Decimal("0") if signal.action is Action.BUY else realized
        paper_fill=PaperFill(order_id=order.id,timestamp=now,symbol=signal.symbol,side=signal.action,
                             quantity=quantity,fill_price=fill,commission=commission,realized_pnl=realized)
        with self.database.connection:
            connection=self.database.connection
            connection.execute("UPDATE metadata SET value=? WHERE key='paper_cash'",(str(new_cash),))
            if signal.action is Action.SELL:
                current=Decimal(connection.execute("SELECT value FROM metadata WHERE key='paper_realized_pnl'").fetchone()[0])
                connection.execute("UPDATE metadata SET value=? WHERE key='paper_realized_pnl'",(str(money(current+realized)),))
            if position is None: connection.execute("DELETE FROM paper_positions WHERE symbol=?",(signal.symbol,))
            else: connection.execute("INSERT INTO paper_positions(symbol,quantity,average_price,last_price,high_price,opened_at,updated_at,data_timestamp,position_status) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET quantity=excluded.quantity,average_price=excluded.average_price,last_price=excluded.last_price,high_price=excluded.high_price,updated_at=excluded.updated_at,data_timestamp=excluded.data_timestamp,position_status=excluded.position_status",
                (position.symbol,position.quantity,str(position.average_price),str(position.last_price),str(max(stored_high,fill)),position.opened_at.isoformat(),position.updated_at.isoformat(),position.updated_at.isoformat(),"FRESH"))
            connection.execute("INSERT INTO paper_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(str(order.id),str(order.signal_id),order.symbol,order.side.value,order.quantity,str(order.requested_price),str(order.fill_price),order.timestamp.isoformat(),order.reason,order.final_score,order.risk_decision.model_dump_json(),order.strategy_version,order.status.value,order.model_dump_json()))
            connection.execute("INSERT INTO paper_fills VALUES(?,?,?,?,?,?,?,?,?,?)",(str(paper_fill.id),str(paper_fill.order_id),paper_fill.timestamp.isoformat(),paper_fill.symbol,paper_fill.side.value,paper_fill.quantity,str(paper_fill.fill_price),str(paper_fill.commission),str(paper_fill.realized_pnl),paper_fill.model_dump_json()))
        self.notifier.send("BUY" if signal.action is Action.BUY else "SELL",f"{signal.symbol} x{quantity} @ {fill}")
        if signal.action is Action.SELL and signal.reason in {reason.value for reason in ExitReason}:
            self.notifier.send(signal.reason,f"{signal.symbol} x{quantity} @ {fill}")
        return order

    def refresh_position(self,symbol: str,price: Decimal,*,data_timestamp: datetime,current_score: float|None=None) -> Decimal:
        """Persist one verified market mark. High-water mark can only increase."""
        rows=self.database.query("SELECT high_price FROM paper_positions WHERE symbol=?",(symbol,))
        if not rows: raise KeyError(symbol)
        high=max(Decimal(rows[0]["high_price"]),Decimal(str(price)))
        self.database.execute("UPDATE paper_positions SET high_price=?,last_price=?,updated_at=?,data_timestamp=?,current_score=?,position_status='FRESH' WHERE symbol=?",
            (str(high),str(price),data_timestamp.isoformat(),data_timestamp.isoformat(),current_score,symbol))
        return high

    def mark_position_stale(self,symbol: str) -> None:
        self.database.execute("UPDATE paper_positions SET position_status='STALE_MARKET_DATA' WHERE symbol=?",(symbol,))

    def run_exit_checks(self,prices: dict[str,Decimal],*,price_timestamps: dict[str,datetime]|None=None,
                        strategy_exit_symbols: set[str]|None=None,
                        now: datetime|None=None,strategy_version: str="v1",update_marks: bool=True,
                        execution_provider:str|None=None) -> list[PaperOrder]:
        """Must be invoked before each new-entry scan."""
        if self.risk_settings is None: raise RuntimeError("risk settings are required for exit checks")
        now=now or datetime.now(timezone.utc); strategy_exit_symbols=strategy_exit_symbols or set(); exits=[]
        price_timestamps=price_timestamps or {symbol:now for symbol in prices}
        if not self.calendar.status(now).can_execute_orders:return exits
        for symbol,position in list(self.get_positions().items()):
            if symbol not in prices: continue
            price=Decimal(str(prices[symbol]))
            validation=self._validation(price_timestamps.get(symbol),price,now,execution_provider)
            if not validation.valid:continue
            if update_marks: high=self._update_high(symbol,price,now)
            else: high=Decimal(self.database.query("SELECT high_price FROM paper_positions WHERE symbol=?",(symbol,))[0]["high_price"])
            reason=None
            if price<=position.average_price*(Decimal("1")-Decimal(str(self.risk_settings.default_stop_loss_pct))): reason=ExitReason.HARD_STOP
            elif price>=position.average_price*(Decimal("1")+Decimal(str(self.risk_settings.default_take_profit_pct))): reason=ExitReason.TAKE_PROFIT
            elif price<=high*(Decimal("1")-Decimal(str(self.risk_settings.default_trailing_stop_pct))): reason=ExitReason.TRAILING_STOP
            elif symbol in strategy_exit_symbols: reason=ExitReason.STRATEGY_EXIT
            elif now-position.opened_at>=timedelta(days=self.risk_settings.max_holding_days): reason=ExitReason.TIME_STOP
            if reason:
                signal=TradeSignal(symbol=symbol,action=Action.SELL,score=0,reason=reason.value,
                    strategy_version=strategy_version,requested_price=float(price),timestamp=price_timestamps.get(symbol,now))
                from bistbot.risk.engine import DeterministicRiskEngine,GlobalKillSwitch
                risk_engine=DeterministicRiskEngine(self.risk_settings,RiskDecisionRepository(self.database),
                    GlobalKillSwitch(SystemStateRepository(self.database)))
                state=self.get_portfolio_state({symbol:price},now=now)
                from bistbot.app.models import RiskOrderRequest
                decision=risk_engine.evaluate(RiskOrderRequest(signal_id=signal.id,symbol=symbol,action=Action.SELL,
                    entry_price=price,price_timestamp=signal.timestamp,execution_quote_validation=validation,
                    requested_quantity=position.quantity),state,now)
                exits.append(self.sell(signal,position.quantity,decision,execution_time=now,
                    execution_provider=execution_provider))
        return exits

    def _update_high(self,symbol: str,price: Decimal,now: datetime) -> Decimal:
        row=self.database.query("SELECT high_price FROM paper_positions WHERE symbol=?",(symbol,))[0]
        high=max(Decimal(row["high_price"]),price)
        self.database.execute("UPDATE paper_positions SET high_price=?,last_price=?,updated_at=? WHERE symbol=?",
                              (str(high),str(price),now.isoformat(),symbol))
        return high

    def _guard_execution(self,price_timestamp:datetime|None,price,execution_time:datetime,
                         execution_provider:str|None)->None:
        broker_now=_aware_utc(self.clock())
        checked_at=max(broker_now,_aware_utc(execution_time))
        validation=self._validation(price_timestamp,price,checked_at,execution_provider)
        if not validation.valid:
            if validation.reason.startswith("MARKET_CLOSED"): raise MarketClosedError(validation.reason)
            raise MarketClosedError("NO_FRESH_SESSION_PRICE")

    def _validation(self,timestamp,price,now,provider):
        default=(self.risk_settings.max_price_age_minutes*60 if self.risk_settings else DEFAULT_EXECUTION_FRESHNESS_SECONDS)
        return validate_execution_quote(SimpleNamespace(provider=provider or "UNKNOWN",price=price,source_timestamp=timestamp),
            evaluated_at=now,calendar=self.calendar,default_freshness_seconds=default,
            yahoo_freshness_seconds=self.yahoo_execution_freshness_seconds or default)

    def cancel_order(self,order_id: str) -> bool:
        rows=self.database.query("SELECT payload FROM paper_orders WHERE id=? AND status='PENDING'",(order_id,))
        if not rows: return False
        order=PaperOrder.model_validate_json(rows[0]["payload"]).model_copy(update={"status":OrderStatus.CANCELLED})
        self.database.execute("UPDATE paper_orders SET status='CANCELLED',payload=? WHERE id=?",(order.model_dump_json(),order_id))
        return True

    def notify_risk_rejection(self,symbol: str,decision: RiskDecision) -> None:
        important={RiskReasonCode.DAILY_LOSS_LIMIT,RiskReasonCode.WEEKLY_LOSS_LIMIT,
                   RiskReasonCode.MAX_DRAWDOWN,RiskReasonCode.KILL_SWITCH}
        if decision.reason_code in important or decision.outcome is RiskOutcome.HALT_TRADING:
            self.notifier.send("RISK REJECTION",f"{symbol}: {decision.reason_code.value} — {decision.reason}")
        if decision.outcome is RiskOutcome.HALT_TRADING:
            self.notifier.send("TRADING HALTED",f"{decision.reason_code.value}: {decision.reason}")

    # Compatibility aliases; canonical V1 interface uses get_*/cancel_order.
    def cash(self): return self.get_cash()
    def positions(self): return self.get_positions()
    def orders(self): return self.get_orders()
    def portfolio_value(self,prices=None): return self.get_portfolio_value(prices)
    def cancel(self,order_id: str): return self.cancel_order(order_id)

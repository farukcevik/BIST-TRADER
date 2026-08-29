from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd

from bistbot.app.config import Settings
from bistbot.market.provider import _is_stale_for_bist_session
from bistbot.market.calendar import BistTradingCalendar


class DashboardDataService:
    """Read-only projection of persisted PAPER state for the local dashboard."""
    def __init__(self,database_path: str|Path,settings: Settings):
        self.database_path=Path(database_path).resolve(); self.settings=settings

    @contextmanager
    def connection(self):
        connection=sqlite3.connect(f"file:{self.database_path}?mode=ro",uri=True,timeout=5)
        connection.row_factory=sqlite3.Row
        connection.execute("PRAGMA query_only=ON"); connection.execute("PRAGMA busy_timeout=5000")
        try: yield connection
        finally: connection.close()

    def load(self) -> dict:
        with self.connection() as connection:
            positions=self._positions(connection); trades=self._trades(connection)
            closed_positions=self._closed_positions(trades)
            cycle=self._last_cycle(connection); history=self._portfolio_history(connection)
            market_regime=self._market_regime(connection)
            metadata={row["key"]:row["value"] for row in connection.execute(
                "SELECT key,value FROM metadata WHERE key IN ('paper_cash','paper_initial_capital','paper_realized_pnl')")}
        cash=Decimal(metadata.get("paper_cash",str(self.settings.capital)))
        initial=Decimal(metadata.get("paper_initial_capital",str(self.settings.capital)))
        invested=sum((Decimal(str(item["market_value"])) for item in positions),Decimal("0"))
        unrealized=sum((Decimal(str(item["unrealized_pnl"])) for item in positions),Decimal("0"))
        equity=cash+invested
        bot_recorded_equity=Decimal(str(history[-1]["equity"])) if history else equity
        realized=sum((Decimal(str(item["realized_pnl"])) for item in closed_positions),Decimal("0"))
        winners=sum(item["realized_pnl"]>0 for item in closed_positions)
        losers=sum(item["realized_pnl"]<0 for item in closed_positions)
        market_status=BistTradingCalendar().status()
        return {"mode":"PAPER","market_status":market_status.as_dict(),"summary":{"equity":float(equity),"cash":float(cash),"invested":float(invested),
            "unrealized":float(unrealized),"unrealized_pct":float(unrealized/invested*100) if invested else 0,
            "open_positions":len(positions),"initial_capital":float(initial),
            "realized":float(realized),"winning_closed_trades":winners,"losing_closed_trades":losers,
            "win_rate":float(winners/(winners+losers)*100) if winners+losers else 0,
            "bot_recorded_equity":float(bot_recorded_equity),
            "last_cycle":cycle.get("timestamp") if cycle else None},"positions":positions,"trades":trades,
            "closed_positions":closed_positions,
            "cycle":cycle,"decisions":cycle.get("decisions",[]) if cycle else [],"history":history,
            "market_regime":market_regime,
            "risk":{"cash_pct":float(cash/equity*100) if equity else 0,"invested_pct":float(invested/equity*100) if equity else 0,
                "max_open_positions":self.settings.risk.max_open_positions,
                "min_cash_pct":self.settings.risk.min_cash_pct*100,
                "daily_loss_limit":self.settings.risk.max_daily_loss_pct*100,
                "weekly_loss_limit":self.settings.risk.max_weekly_loss_pct*100,
                "drawdown_limit":self.settings.risk.max_total_drawdown_pct*100}}

    @staticmethod
    def _market_regime(connection) -> dict:
        try:
            row=connection.execute("SELECT payload FROM market_regime_states ORDER BY id DESC LIMIT 1").fetchone()
            return json.loads(row["payload"]) if row else {}
        except (sqlite3.OperationalError,sqlite3.ProgrammingError):
            return {}

    def _positions(self,connection) -> list[dict]:
        rows=list(connection.execute("SELECT * FROM paper_positions ORDER BY symbol")); now=datetime.now(timezone.utc); output=[]
        for row in rows:
            columns=set(row.keys()); symbol=row["symbol"]; quantity=int(row["quantity"])
            average=Decimal(row["average_price"]); last=Decimal(row["last_price"]); high=Decimal(row["high_price"])
            raw_timestamp=row["data_timestamp"] if "data_timestamp" in columns else None
            timestamp=_parse_one_timestamp(raw_timestamp) if raw_timestamp else None
            stored_status=row["position_status"] if "position_status" in columns else "UNKNOWN"
            stale=(stored_status!="FRESH" or timestamp is None or
                _is_stale_for_bist_session(now,timestamp,timedelta(minutes=self.settings.risk.max_price_age_minutes)))
            entry_score=connection.execute("SELECT final_score FROM paper_orders WHERE symbol=? AND side='BUY' AND status='FILLED' ORDER BY timestamp DESC LIMIT 1",(symbol,)).fetchone()
            latest_score=connection.execute("SELECT score FROM technical_signals WHERE symbol=? ORDER BY timestamp DESC LIMIT 1",(symbol,)).fetchone()
            stored_score=row["current_score"] if "current_score" in columns else None
            pnl=(last-average)*quantity; market_value=last*quantity
            output.append({"symbol":symbol,"quantity":quantity,"average_entry":float(average),"last_price":float(last),
                "market_value":float(market_value),"unrealized_pnl":float(pnl),
                "unrealized_pnl_pct":float((last/average-1)*100),
                "entry_score":entry_score[0] if entry_score else None,
                "current_score":None if stale else (stored_score if stored_score is not None else (latest_score[0] if latest_score else None)),
                "last_known_score":stored_score if stored_score is not None else (latest_score[0] if latest_score else None),
                "stop_price":float(average*(Decimal("1")-Decimal(str(self.settings.risk.default_stop_loss_pct)))),
                "take_profit_price":float(average*(Decimal("1")+Decimal(str(self.settings.risk.default_take_profit_pct)))),
                "trailing_stop":float(high*(Decimal("1")-Decimal(str(self.settings.risk.default_trailing_stop_pct)))),
                "highest_price":float(high),"opened_at":row["opened_at"],"updated_at":row["updated_at"],
                "market_data_status":"STALE_MARKET_DATA" if stale else "FRESH"})
        return output

    @staticmethod
    def _trades(connection) -> list[dict]:
        query="""SELECT f.id AS fill_id,f.order_id,f.timestamp,f.symbol,f.side,f.quantity,
                 f.fill_price,f.commission,f.realized_pnl,f.payload,o.requested_price,
                 o.final_score,o.reason
                 FROM paper_fills f LEFT JOIN paper_orders o ON o.id=f.order_id"""
        output=[]
        for row in connection.execute(query):
            price=Decimal(row["fill_price"]); quantity=int(row["quantity"])
            slippage=None
            try:
                payload=json.loads(row["payload"] or "{}")
                slippage=payload.get("slippage") or payload.get("slippage_amount")
            except (TypeError,ValueError):
                pass
            output.append({"timestamp":row["timestamp"],"symbol":row["symbol"],"side":row["side"],
                "quantity":quantity,"price":float(price),"gross_value":float(price*quantity),
                "commission":float(row["commission"]),"slippage":float(slippage) if slippage is not None else None,
                "final_score":row["final_score"],"reason":row["reason"] or "UNKNOWN",
                "exit_reason":(row["reason"] or "UNKNOWN") if row["side"]=="SELL" else None,
                "realized_pnl":float(row["realized_pnl"]) if row["side"]=="SELL" else None,
                "realized_pnl_pct":None,"holding_seconds":None,
                "order_id":row["order_id"],"trade_id":row["fill_id"]})
        return sorted(output,key=lambda item:_timestamp_sort_key(item["timestamp"]),reverse=True)

    @staticmethod
    def _closed_positions(trades: list[dict]) -> list[dict]:
        """Reconstruct completed inventory cycles; SELL P&L remains fill-authoritative."""
        state={}; closed=[]
        for fill in sorted(trades,key=lambda item:_timestamp_sort_key(item["timestamp"])):
            symbol=fill["symbol"]
            cycle=state.setdefault(symbol,{"quantity":0,"buys":[],"sells":[]})
            if fill["side"]=="BUY":
                if cycle["quantity"]==0: cycle={"quantity":0,"buys":[],"sells":[]}; state[symbol]=cycle
                cycle["buys"].append(fill); cycle["quantity"]+=fill["quantity"]
            elif fill["side"]=="SELL" and cycle["quantity"]>0:
                cycle["sells"].append(fill); cycle["quantity"]-=fill["quantity"]
                if cycle["quantity"]==0:
                    buys=cycle["buys"]; sells=cycle["sells"]
                    buy_qty=sum(item["quantity"] for item in buys); sell_qty=sum(item["quantity"] for item in sells)
                    gross_buy=sum(item["gross_value"] for item in buys); gross_sell=sum(item["gross_value"] for item in sells)
                    pnl=sum(item["realized_pnl"] or 0 for item in sells); costs=sum(item["commission"] for item in buys+sells)
                    opened=_parse_one_timestamp(buys[0]["timestamp"]); closed_at=_parse_one_timestamp(sells[-1]["timestamp"])
                    basis=gross_buy
                    for sell in sells:
                        sell_basis=sell["gross_value"]-(sell["realized_pnl"] or 0)-sell["commission"]
                        sell["realized_pnl_pct"]=(sell["realized_pnl"] or 0)/sell_basis*100 if sell_basis else 0
                        sell_at=_parse_one_timestamp(sell["timestamp"])
                        sell["holding_seconds"]=max(0,(sell_at-opened).total_seconds()) if opened and sell_at else None
                    closed.append({"symbol":symbol,"opened_at":buys[0]["timestamp"],"closed_at":sells[-1]["timestamp"],
                        "buy_quantity":buy_qty,"average_buy_price":gross_buy/buy_qty,"average_sell_price":gross_sell/sell_qty,
                        "gross_buy_value":gross_buy,"gross_sell_value":gross_sell,"commission_costs":costs,
                        "realized_pnl":pnl,"realized_pnl_pct":pnl/basis*100 if basis else 0,
                        "holding_seconds":max(0,(closed_at-opened).total_seconds()) if opened and closed_at else None,
                        "entry_score":buys[0]["final_score"],"exit_reason":sells[-1]["exit_reason"] or "UNKNOWN"})
        return sorted(closed,key=lambda item:_timestamp_sort_key(item["closed_at"]),reverse=True)

    @staticmethod
    def _last_cycle(connection) -> dict:
        row=connection.execute("SELECT timestamp,payload FROM system_events WHERE event_type='CYCLE_COMPLETED' ORDER BY id DESC LIMIT 1").fetchone()
        if not row:return {}
        payload=json.loads(row["payload"]); return {"timestamp":row["timestamp"],**payload}

    @staticmethod
    def _portfolio_history(connection) -> list[dict]:
        return [{"timestamp":row["timestamp"],"equity":float(row["equity"]),"cash":float(row["cash"])}
            for row in connection.execute("SELECT timestamp,equity,cash FROM paper_portfolio_snapshots ORDER BY timestamp DESC LIMIT 500")][::-1]


def parse_dashboard_timestamps(values) -> pd.Series:
    """Parse mixed/legacy ISO values without allowing one bad row to abort a view."""
    series=pd.Series(values)
    try:
        return pd.to_datetime(series,format="mixed",utc=True,errors="coerce")
    except (TypeError,ValueError):  # pandas < 2.0
        return series.map(lambda value: pd.to_datetime(value,utc=True,errors="coerce"))


def _parse_one_timestamp(value):
    parsed=parse_dashboard_timestamps([value]).iloc[0]
    return None if pd.isna(parsed) else parsed.to_pydatetime()


def _timestamp_sort_key(value):
    parsed=_parse_one_timestamp(value)
    return parsed or datetime.min.replace(tzinfo=timezone.utc)

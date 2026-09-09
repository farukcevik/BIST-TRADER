from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from bistbot.app.config import load_settings
from bistbot.dashboard_data import DashboardDataService,parse_dashboard_timestamps
from bistbot.dashboard_live import refresh_live_positions,live_portfolio_summary
from bistbot.market.provider import YahooBistProvider


ROOT=Path(__file__).resolve().parent
SETTINGS=load_settings(ROOT/"config.yaml")
DATABASE=Path(SETTINGS.database)
if not DATABASE.is_absolute(): DATABASE=ROOT/DATABASE

st.set_page_config(page_title="BISTBOT Paper Dashboard",page_icon="📊",layout="wide")
st.markdown("""<style>
.paper-mode {display:inline-block;padding:.25rem .65rem;border-radius:.4rem;background:#18392b;color:#7ef0b5;font-weight:700}
[data-testid="stMetricValue"] {font-size:1.65rem}
</style>""",unsafe_allow_html=True)
st.title("BISTBOT")
st.markdown('<span class="paper-mode">MODE: PAPER</span> &nbsp; Local single-user dashboard · No order controls',unsafe_allow_html=True)
REFRESH_SECONDS=SETTINGS.dashboard.live_price_refresh_seconds
st.caption(f"Auto-refresh: approximately every {REFRESH_SECONDS} seconds. Quotes are fetched only for open positions; no bot cycle or OpenAI call runs.")


def money(value): return f"₺{value:,.2f}"
def percentage(value): return f"{value:+.2f}%"
def optional_money(value): return "—" if value is None else money(value)
def optional_percentage(value): return "—" if value is None else percentage(value)


def display_timestamps(frame,column):
    parsed=parse_dashboard_timestamps(frame[column])
    frame=frame.copy()
    valid=parsed.notna()
    frame[column]="INVALID TIMESTAMP"
    frame.loc[valid,column]=parsed[valid].dt.tz_convert("Europe/Istanbul").dt.strftime("%d.%m.%Y %H:%M")
    return frame


def duration(seconds):
    if seconds is None:return "UNKNOWN"
    minutes=int(seconds)//60
    days,minutes=divmod(minutes,1440); hours,minutes=divmod(minutes,60)
    return f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m"


@st.cache_data(ttl=REFRESH_SECONDS,show_spinner=False)
def cached_live_positions(positions_key: tuple[tuple,...]) -> list[dict]:
    positions=[dict(item) for item in positions_key]
    return refresh_live_positions(positions,YahooBistProvider(),refresh_seconds=REFRESH_SECONDS)


def position_cache_key(positions: list[dict]) -> tuple[tuple,...]:
    # Include persisted values needed for display. A bot update invalidates the
    # cache even before its short TTL expires.
    return tuple(tuple(sorted(item.items())) for item in positions)


def dashboard():
    try: data=DashboardDataService(DATABASE,SETTINGS).load()
    except Exception as error:
        st.error(f"Dashboard could not read the PAPER database: {type(error).__name__}: {error}"); return
    summary=data["summary"]
    market_status=data["market_status"]
    st.header("BIST MARKET")
    status_label="OPEN" if market_status["can_execute_orders"] else "CLOSED"
    st.subheader(status_label)
    status_columns=st.columns(4)
    status_columns[0].metric("Session",market_status["session"])
    status_columns[1].metric("Reason",market_status["reason"].replace("MARKET_CLOSED_","").replace("_"," ").title())
    status_columns[2].metric("Local time",market_status["local_time"])
    status_columns[3].metric("Next order execution","Allowed now" if market_status["can_execute_orders"] else "Not allowed until valid BIST session")
    live_positions=cached_live_positions(position_cache_key(data["positions"]))
    live=live_portfolio_summary(cash=summary["cash"],positions=live_positions,
        initial_capital=summary["initial_capital"],bot_recorded_equity=summary["bot_recorded_equity"])
    total_pnl=live["live_equity"]-summary["initial_capital"]
    total_return=total_pnl/summary["initial_capital"]*100 if summary["initial_capital"] else 0
    st.header("SUMMARY")
    columns=st.columns(9)
    values=(("Starting Capital",money(summary["initial_capital"])),("Current Live Equity",money(live["live_equity"])),
        ("Total Net P&L",money(total_pnl)),("Total Net Return",percentage(total_return)),
        ("Realized P&L",money(summary["realized"])),("Unrealized P&L",money(live["live_unrealized"])),
        ("Winning Closed Trades",summary["winning_closed_trades"]),("Losing Closed Trades",summary["losing_closed_trades"]),
        ("Win Rate",percentage(summary["win_rate"])))
    for column,(label,value) in zip(columns,values): column.metric(label,value)

    st.header("MARKET REGIME")
    regime=data.get("market_regime",{})
    if not regime: st.info("No persisted market-regime refresh yet. Run a PAPER cycle.")
    else:
        cycle_regime=(data.get("cycle",{}).get("summary",{}))
        provider_status=cycle_regime.get("macro_provider_status","UNKNOWN")
        cols=st.columns(5)
        for column,(label,value) in zip(cols,(("Provider status",provider_status),("Regime",regime.get("regime")),
                ("Risk",f"{regime.get('market_risk_score',0)}/100"),("Confidence",regime.get("confidence")),
                ("Last update",regime.get("last_updated")))): column.metric(label,value)
        categories=regime.get("risk_categories",{})
        st.subheader("Risk categories")
        category_columns=st.columns(7)
        for column,(label,value) in zip(category_columns,categories.items()): column.metric(label.replace("_"," ").title(),value)
        st.subheader("LATEST MATERIAL MACRO EVENTS")
        events=pd.DataFrame(regime.get("material_events",[]))
        if events.empty: st.info("No material macro events in the current regime window.")
        else:
            desired=[name for name in ("published_at","source","title","materiality_score","direction","affected_sectors") if name in events]
            st.dataframe(events[desired].rename(columns={"published_at":"Timestamp","source":"Source","title":"Headline",
                "materiality_score":"Materiality","direction":"Direction","affected_sectors":"Affected sectors"}),
                use_container_width=True,hide_index=True)

    st.header("RECENT ACTIVITY")
    activity=pd.DataFrame(data["trades"])
    if activity.empty: st.info("No PAPER activity recorded.")
    else:
        activity=display_timestamps(activity,"timestamp")
        activity=activity.rename(columns={"timestamp":"Timestamp","side":"Action","symbol":"Symbol","quantity":"Quantity",
            "price":"Price","realized_pnl":"P&L for SELL","reason":"Reason"})
        st.dataframe(activity[["Timestamp","Action","Symbol","Quantity","Price","P&L for SELL","Reason"]].style.format(
            {"Price":"₺{:,.4f}","P&L for SELL":lambda value:"" if pd.isna(value) else f"₺{value:+,.2f}"}),
            use_container_width=True,hide_index=True)

    st.header("TRADE HISTORY")
    trades=pd.DataFrame(data["trades"])
    if trades.empty: st.info("No PAPER fills recorded.")
    else:
        trades=display_timestamps(trades,"timestamp")
        trades["holding_duration"]=trades["holding_seconds"].map(lambda value:"—" if pd.isna(value) else duration(value))
        trades=trades.rename(columns={"timestamp":"Timestamp","symbol":"Symbol","side":"Side","quantity":"Quantity",
            "price":"Price","gross_value":"Gross Value","commission":"Commission","slippage":"Slippage",
            "final_score":"Final / Entry Score","reason":"Reason","realized_pnl":"Realized P&L TL",
            "realized_pnl_pct":"Realized P&L %","holding_duration":"Holding Duration","order_id":"Order ID","trade_id":"Trade ID"})
        st.dataframe(trades[["Timestamp","Symbol","Side","Quantity","Price","Gross Value","Commission","Slippage",
            "Final / Entry Score","Reason","Realized P&L TL","Realized P&L %","Holding Duration","Order ID","Trade ID"]],
            use_container_width=True,hide_index=True)

    st.header("CLOSED POSITIONS")
    closed=pd.DataFrame(data["closed_positions"])
    if closed.empty: st.info("No completed PAPER positions recorded.")
    else:
        closed=display_timestamps(closed,"opened_at"); closed=display_timestamps(closed,"closed_at")
        closed["holding_time"]=closed["holding_seconds"].map(duration)
        closed=closed.rename(columns={"symbol":"Symbol","opened_at":"Opened At","closed_at":"Closed At",
            "buy_quantity":"Buy Quantity","average_buy_price":"Average Buy Price","average_sell_price":"Average Sell Price",
            "gross_buy_value":"Gross Buy Value","gross_sell_value":"Gross Sell Value","commission_costs":"Commission / Costs",
            "realized_pnl":"Realized P&L TL","realized_pnl_pct":"Realized P&L %","holding_time":"Holding Time",
            "entry_score":"Entry Score","exit_reason":"Exit Reason"})
        st.dataframe(closed[["Symbol","Opened At","Closed At","Buy Quantity","Average Buy Price","Average Sell Price",
            "Gross Buy Value","Gross Sell Value","Commission / Costs","Realized P&L TL","Realized P&L %","Holding Time",
            "Entry Score","Exit Reason"]],use_container_width=True,hide_index=True)

    st.header("PORTFOLIO")

    st.header("OPEN POSITIONS")
    positions=pd.DataFrame(live_positions)
    if positions.empty: st.info("No open PAPER positions.")
    else:
        sort_label=st.selectbox("Sort positions by",["P&L %","Market Value","Current Score","Symbol"])
        sort_map={"P&L %":"live_unrealized_pnl_pct","Market Value":"live_market_value","Current Score":"current_score","Symbol":"symbol"}
        ascending=sort_label=="Symbol"; positions=positions.sort_values(sort_map[sort_label],ascending=ascending,na_position="last")
        display=positions.rename(columns={"symbol":"Symbol","quantity":"Quantity","average_entry":"Average Entry Price",
            "live_price":"Live Price","live_market_value":"Live Market Value","live_unrealized_pnl":"Live P&L TL",
            "live_unrealized_pnl_pct":"Live P&L %","price_timestamp_display":"Price Timestamp",
            "price_age":"Price Age","price_status":"Price Status","entry_score":"Entry Score","current_score":"Current Score",
            "stop_price":"Stop Price","take_profit_price":"Take Profit Price","trailing_stop":"Trailing Stop",
            "highest_price":"Highest Price Since Entry","opened_at":"Opened At","updated_at":"Updated At",
            "market_data_status":"Market Data Status"})
        columns_to_show=["Symbol","Quantity","Average Entry Price","Live Price","Live Market Value","Live P&L TL",
            "Live P&L %","Price Timestamp","Price Age","Price Status","Entry Score","Current Score","Stop Price",
            "Take Profit Price","Trailing Stop","Highest Price Since Entry","Opened At","Updated At","Market Data Status"]
        styled=display[columns_to_show].style.format({"Average Entry Price":"₺{:,.4f}","Live Price":"₺{:,.4f}",
            "Live Market Value":"₺{:,.2f}","Live P&L TL":"₺{:+,.2f}","Live P&L %":"{:+.2f}%",
            "Stop Price":"₺{:,.4f}","Take Profit Price":"₺{:,.4f}","Trailing Stop":"₺{:,.4f}",
            "Highest Price Since Entry":"₺{:,.4f}"}).map(
                lambda value:"color:#159957;font-weight:600" if isinstance(value,(int,float)) and value>0 else
                             "color:#d9534f;font-weight:600" if isinstance(value,(int,float)) and value<0 else "",
                subset=["Live P&L TL","Live P&L %"])
        st.dataframe(styled,use_container_width=True,hide_index=True)

        st.subheader("DYNAMIC EXIT PLAN")
        exit_plans=positions.copy()
        for number in (1,2,3):
            target=f"target_{number}"; distance=f"distance_to_target_{number}"
            exit_plans[distance]=exit_plans.apply(
                lambda row: ((row[target]/row["live_price"])-1)*100
                if pd.notna(row.get(target)) and row.get("live_price",0)>0 else None,axis=1)
        exit_plans=exit_plans.rename(columns={"symbol":"Symbol","average_entry":"Entry","live_price":"Current Price",
            "live_unrealized_pnl":"Current P&L","initial_stop_price":"Initial Stop","current_stop_price":"Current Stop",
            "target_1":"Target 1","target_2":"Target 2","target_3":"Target 3","potential_score":"Potential Score",
            "expected_upside_pct":"Expected Upside","risk_reward_ratio":"Risk / Reward",
            "target_confidence":"Target Confidence","holding_horizon":"Holding Horizon","exit_stage":"Exit Stage",
            "distance_to_target_1":"Distance to Target 1","distance_to_target_2":"Distance to Target 2",
            "distance_to_target_3":"Distance to Target 3"})
        plan_columns=["Symbol","Entry","Current Price","Current P&L","Initial Stop","Current Stop","Target 1","Target 2",
            "Target 3","Potential Score","Expected Upside","Risk / Reward","Target Confidence","Holding Horizon","Exit Stage",
            "Distance to Target 1","Distance to Target 2","Distance to Target 3"]
        st.dataframe(exit_plans[plan_columns].style.format({
            "Entry":"₺{:,.4f}","Current Price":"₺{:,.4f}","Current P&L":"₺{:+,.2f}","Initial Stop":"₺{:,.4f}",
            "Current Stop":"₺{:,.4f}","Target 1":lambda value:"—" if pd.isna(value) else f"₺{value:,.4f}",
            "Target 2":lambda value:"—" if pd.isna(value) else f"₺{value:,.4f}",
            "Target 3":lambda value:"—" if pd.isna(value) else f"₺{value:,.4f}",
            "Expected Upside":lambda value:"—" if pd.isna(value) else f"{value*100:+.2f}%",
            "Risk / Reward":lambda value:"—" if pd.isna(value) else f"{value:.2f}",
            "Distance to Target 1":lambda value:"—" if pd.isna(value) else f"{value:+.2f}%",
            "Distance to Target 2":lambda value:"—" if pd.isna(value) else f"{value:+.2f}%",
            "Distance to Target 3":lambda value:"—" if pd.isna(value) else f"{value:+.2f}%"}),
            use_container_width=True,hide_index=True)

        st.subheader("POSITION DETAIL")
        selected=st.selectbox("Symbol",positions["symbol"].tolist()); item=next(row for row in live_positions if row["symbol"]==selected)
        details={"Entry Price":money(item["average_entry"]),"Live Price":money(item["live_price"]),"Quantity":item["quantity"],
            "Live Position Value":money(item["live_market_value"]),"Live P&L":f"{money(item['live_unrealized_pnl'])} ({percentage(item['live_unrealized_pnl_pct'])})",
            "Entry Score":item["entry_score"],"Current Score":item["current_score"],"Stop":money(item["stop_price"]),
            "Take Profit":money(item["take_profit_price"]),"Trailing Stop":money(item["trailing_stop"]),
            "Highest Since Entry":money(item["highest_price"]),"Opened At":item["opened_at"],"Updated At":item["updated_at"],
            "Price Timestamp":item["price_timestamp_display"],"Price Age":item["price_age"],"Price Status":item["price_status"]}
        details.update({"Initial Stop":optional_money(item.get("initial_stop_price")),
            "Current Stop":optional_money(item.get("current_stop_price")),"Target 1":optional_money(item.get("target_1")),
            "Target 2":optional_money(item.get("target_2")),"Target 3":optional_money(item.get("target_3")),
            "Potential Score":item.get("potential_score") if item.get("potential_score") is not None else "—",
            "Expected Upside":optional_percentage(item["expected_upside_pct"]*100) if item.get("expected_upside_pct") is not None else "—",
            "Risk / Reward":item.get("risk_reward_ratio") if item.get("risk_reward_ratio") is not None else "—",
            "Target Confidence":item.get("target_confidence","—"),"Holding Horizon":item.get("holding_horizon","—"),
            "Exit Stage":item.get("exit_stage","—")})
        detail_columns=st.columns(4)
        for index,(label,value) in enumerate(details.items()): detail_columns[index%4].metric(label,value)
        st.info("Per-symbol historical prices are not persisted. No synthetic position chart is shown.")

    st.header("PERFORMANCE")
    history=pd.DataFrame(data["history"])
    if history.empty: st.info("No stored portfolio snapshots available.")
    else:
        history["timestamp"]=parse_dashboard_timestamps(history["timestamp"])
        invalid=int(history["timestamp"].isna().sum()); history=history.dropna(subset=["timestamp"])
        if invalid: st.warning(f"Ignored {invalid} malformed portfolio timestamp row(s); persisted data was not changed.")
        if history.empty: st.info("No valid stored portfolio snapshots available.")
        else:
            history["timestamp"]=history["timestamp"].dt.tz_convert("Europe/Istanbul")
            history["Starting Capital"]=summary["initial_capital"]
            st.line_chart(history.set_index("timestamp")[["equity","cash","Starting Capital"]].rename(
                columns={"equity":"Portfolio Equity","cash":"Cash"}))

    st.header("BOT ACTIVITY")
    cycle=data["cycle"]; cycle_summary=cycle.get("summary",{})
    if not cycle: st.info("No CYCLE_COMPLETED audit record yet. It will appear after the next bot cycle.")
    else:
        fields=("market_data_success","symbols_valid","scanner_candidates","llm_candidates","llm_api_attempts",
            "buy_signals","sell_signals","entry_orders","exit_orders","news_provider_status","kap_provider_status","llm_provider_status")
        st.json({"last_cycle":cycle["timestamp"],**{field:cycle_summary.get(field) for field in fields}})
    decisions=pd.DataFrame(data["decisions"])
    st.subheader("RECENT DECISIONS")
    if decisions.empty: st.info("No persisted cycle decisions available.")
    else:
        desired=[name for name in ("symbol","scanner_score","final_score","decision","signal_mode","reason") if name in decisions]
        st.dataframe(decisions[desired],use_container_width=True,hide_index=True)

    st.header("RISK")
    risk=data["risk"]; cols=st.columns(7)
    risk_values=(("Cash %",percentage(risk["cash_pct"])),("Invested %",percentage(risk["invested_pct"])),
        ("Open / Max",f"{summary['open_positions']} / {risk['max_open_positions']}"),("Minimum Cash",f"{risk['min_cash_pct']:.2f}%"),
        ("Daily Loss Limit",f"{risk['daily_loss_limit']:.2f}%"),("Weekly Loss Limit",f"{risk['weekly_loss_limit']:.2f}%"),
        ("Drawdown Limit",f"{risk['drawdown_limit']:.2f}%"))
    for column,(label,value) in zip(cols,risk_values): column.metric(label,value)


if hasattr(st,"fragment"):
    st.fragment(run_every=f"{REFRESH_SECONDS}s")(dashboard)()
else:
    # Streamlit <1.37 compatibility. This refreshes only the browser page;
    # dashboard() remains a read-only SQLite projection.
    st.markdown(f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">',unsafe_allow_html=True)
    dashboard()

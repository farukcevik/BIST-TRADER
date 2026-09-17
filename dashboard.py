from __future__ import annotations

import math
from numbers import Real
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
REFRESH_SECONDS=SETTINGS.dashboard.live_price_refresh_seconds

TEXT={
    "TR":{"overview":"Genel bakış","market":"BIST piyasası","portfolio":"Portföy","candidates":"Adaylar ve kararlar",
        "history":"Geçmiş ve performans","risk":"Risk ayarları","open_positions":"Açık pozisyonlar",
        "recent_activity":"Son işlemler","trade_history":"İşlem geçmişi","closed_positions":"Kapanan pozisyonlar",
        "performance":"Performans","regime":"Piyasa rejimi","bot_activity":"Bot etkinliği",
        "paper_note":"Yerel, tek kullanıcılı ve salt okunur PAPER ekranı · Emir kontrolü yok",
        "refresh":f"Yaklaşık {REFRESH_SECONDS} saniyede bir yenilenir. Yalnızca açık pozisyon fiyatları alınır; bot döngüsü ve OpenAI çağrısı çalışmaz.",
        "language":"Dil / Language","paper_controls":"PAPER portföy durumu","read_only":"Bu değerler salt okunur. Değişiklikler dashboard dışındaki kontrollü yönetim akışından yapılır."},
    "EN":{"overview":"Overview","market":"BIST market","portfolio":"Portfolio","candidates":"Candidates and decisions",
        "history":"History and performance","risk":"Risk settings","open_positions":"Open positions",
        "recent_activity":"Recent activity","trade_history":"Trade history","closed_positions":"Closed positions",
        "performance":"Performance","regime":"Market regime","bot_activity":"Bot activity",
        "paper_note":"Local, single-user, read-only PAPER dashboard · No order controls",
        "refresh":f"Refreshes approximately every {REFRESH_SECONDS} seconds. Only open-position quotes are fetched; no bot cycle or OpenAI call runs.",
        "language":"Language / Dil","paper_controls":"PAPER portfolio status","read_only":"These values are read-only. Changes belong in the controlled management workflow outside the dashboard."},
}


def t(key: str) -> str:
    return TEXT[st.session_state.get("dashboard_language","TR")][key]


def ui(en: str,tr: str) -> str:
    return tr if st.session_state.get("dashboard_language","TR")=="TR" else en


TABLE_TR={"Timestamp":"Zaman","Source":"Kaynak","Headline":"Başlık","Materiality":"Önem","Direction":"Yön",
    "Affected sectors":"Etkilenen sektörler","Action":"Eylem","Symbol":"Sembol","Quantity":"Adet","Price":"Fiyat",
    "Reason":"Neden","Side":"Yön","Gross Value":"Brüt değer","Commission":"Komisyon","Slippage":"Kayma",
    "Holding Duration":"Elde tutma süresi","Opened At":"Açılış","Closed At":"Kapanış","Exit Reason":"Çıkış nedeni",
    "Average Entry Price":"Ortalama giriş","Live Price":"Canlı fiyat","Live Market Value":"Canlı piyasa değeri",
    "Price Timestamp":"Fiyat zamanı","Price Age":"Fiyat yaşı","Price Status":"Fiyat durumu","Entry Score":"Giriş puanı",
    "Current Score":"Güncel puan","Stop Price":"Zarar durdur","Take Profit Price":"Kâr al","Trailing Stop":"İzleyen stop",
    "Market Data Status":"Piyasa verisi durumu","P&L for SELL":"SATIŞ K/Z","Final / Entry Score":"Nihai / giriş puanı",
    "Realized P&L TL":"Gerçekleşen K/Z TL","Realized P&L %":"Gerçekleşen K/Z %","Order ID":"Emir no","Trade ID":"İşlem no",
    "Buy Quantity":"Alış adedi","Average Buy Price":"Ortalama alış","Average Sell Price":"Ortalama satış",
    "Gross Buy Value":"Brüt alış değeri","Gross Sell Value":"Brüt satış değeri","Commission / Costs":"Komisyon / maliyet",
    "Holding Time":"Elde tutma süresi","Highest Price Since Entry":"Girişten beri en yüksek","Opened At":"Açılış",
    "Updated At":"Güncelleme","Live P&L TL":"Canlı K/Z TL","Live P&L %":"Canlı K/Z %"}


def table_labels(columns) -> dict:
    return {name:TABLE_TR[name] for name in columns if st.session_state.get("dashboard_language","TR")=="TR" and name in TABLE_TR}


def human_reason(code,reason,language=None) -> str:
    """Turn stable machine reason codes into concise operator-facing explanations."""
    language=language or st.session_state.get("dashboard_language","TR")
    key=str(code or "UNKNOWN").strip().upper().replace("-","_").replace(" ","_")
    explanations={
        "TR":{"BUY_CANDIDATE":"Alım koşulları karşılandı.","INSUFFICIENT_RISK_REWARD":"Beklenen getiri, alınan riske göre yetersiz.",
            "FUNDAMENTAL_BLOCKING_RED_FLAG":"Kritik temel analiz riski alımı engelliyor.","FUNDAMENTAL_DATA_UNAVAILABLE":"Temel veri yetersiz; güvenli varsayım olarak bekleniyor.",
            "FUNDAMENTAL_CONFIDENCE_LOW":"Temel analiz güveni alım için düşük.","FUNDAMENTAL_QUALITY_LOW":"Kapsama ve güvenle düzeltilmiş temel kalite düşük.",
            "TECHNICAL_TIMING_WEAK":"Teknik zamanlama henüz yeterince güçlü değil.","CATALYST_CONFIRMATION_WEAK":"Haber/katalizör teyidi yetersiz.",
            "POTENTIAL_ASSESSMENT_UNAVAILABLE":"Hedef ve potansiyel değerlendirmesi kullanılamıyor.",
            "FUNDAMENTAL_FALLBACK_TECHNICAL_SCORE_TOO_LOW":"Temel veri yokken gereken daha yüksek teknik puan karşılanmadı.",
            "FUNDAMENTAL_FALLBACK_CONFIDENCE_TOO_LOW":"Temel veri yokken hedef güveni yeterli değil.",
            "FUNDAMENTAL_FALLBACK_RR_TOO_LOW":"Temel veri yokken gereken risk/getiri oranı karşılanmadı.",
            "TECHNICAL_LEVELS_UNAVAILABLE":"Güvenilir teknik destek ve direnç seviyeleri oluşturulamadı.",
            "MARKET_REGIME_BLOCK":"Mevcut piyasa rejimi yeni alımı engelliyor.",
            "HARD_STOP":"Zarar durdur seviyesi tetiklendi.","STOP_LOSS":"Zarar durdur seviyesi tetiklendi.","TAKE_PROFIT":"Kâr alma hedefi tetiklendi.",
            "TRAILING_STOP":"İzleyen zarar durdur seviyesi tetiklendi.","STRATEGY_EXIT":"Strateji çıkış koşulları oluştu.",
            "TIME_STOP":"Azami elde tutma süresi doldu.","ENTRY":"Alım koşulları karşılandı ve pozisyon açıldı.",
            "APPROVED":"Risk kontrolleri işlemi onayladı.","HOLD":"Koşullar yeni işlem için yeterli değil; bekleniyor.",
            "BUY":"Alım koşulları karşılandı.","SELL":"Satış koşulları karşılandı."},
        "EN":{"BUY_CANDIDATE":"Buy conditions are satisfied.","INSUFFICIENT_RISK_REWARD":"Expected return is too low for the risk taken.",
            "FUNDAMENTAL_BLOCKING_RED_FLAG":"A critical fundamental risk blocks the entry.","FUNDAMENTAL_DATA_UNAVAILABLE":"Fundamental data is insufficient, so the safe decision is to wait.",
            "FUNDAMENTAL_CONFIDENCE_LOW":"Fundamental-analysis confidence is too low to buy.","FUNDAMENTAL_QUALITY_LOW":"Coverage- and confidence-adjusted fundamental quality is too low.",
            "TECHNICAL_TIMING_WEAK":"Technical timing is not strong enough yet.","CATALYST_CONFIRMATION_WEAK":"News/catalyst confirmation is insufficient.",
            "POTENTIAL_ASSESSMENT_UNAVAILABLE":"Target and potential assessment is unavailable.",
            "FUNDAMENTAL_FALLBACK_TECHNICAL_SCORE_TOO_LOW":"The higher technical-score requirement for missing fundamentals was not met.",
            "FUNDAMENTAL_FALLBACK_CONFIDENCE_TOO_LOW":"Target confidence is too low while fundamentals are unavailable.",
            "FUNDAMENTAL_FALLBACK_RR_TOO_LOW":"The higher risk/reward requirement for missing fundamentals was not met.",
            "TECHNICAL_LEVELS_UNAVAILABLE":"Reliable technical support and resistance levels could not be established.",
            "MARKET_REGIME_BLOCK":"The current market regime blocks new entries.",
            "HARD_STOP":"The hard stop-loss level was triggered.","STOP_LOSS":"The stop-loss level was triggered.","TAKE_PROFIT":"The take-profit target was triggered.",
            "TRAILING_STOP":"The trailing stop-loss level was triggered.","STRATEGY_EXIT":"Strategy exit conditions were met.",
            "TIME_STOP":"The maximum holding period expired.","ENTRY":"Buy conditions were met and the position was opened.",
            "APPROVED":"Risk controls approved the trade.","HOLD":"Conditions are insufficient for a new trade; waiting.",
            "BUY":"Buy conditions were met.","SELL":"Sell conditions were met."},
    }
    return explanations[language].get(key,str(reason or key).replace("_"," ").strip().capitalize())


def display_reason(value,language=None) -> str:
    """Translate known persisted reason codes without altering stored values."""
    text=str(value or "")
    code=text.split(":",1)[0].strip().upper().replace("-","_").replace(" ","_")
    return human_reason(code,text,language)


def money(value): return f"₺{value:,.2f}"
def percentage(value): return f"{value:+.2f}%"


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


def display_value(value,*,suffix=""):
    if value is None or value=="":return "Unavailable"
    return f"{value}{suffix}"


def _valid_price(value) -> bool:
    return isinstance(value,Real) and not isinstance(value,bool) and math.isfinite(float(value)) and value>0


def _valid_number(value) -> bool:
    return isinstance(value,Real) and not isinstance(value,bool) and math.isfinite(float(value))


def format_analysis_value(section: str,label: str,value) -> str:
    """Format persisted analysis units without rescaling or inferring values."""
    if value is None or value=="":return "Unavailable"
    if isinstance(value,list):return ", ".join(str(item) for item in value) if value else "None reported"
    price_labels={"Support 1","Support 2","Resistance 1","Resistance 2","Entry","Stop","Target"}
    score_labels={"Fundamental score","Effective fundamental","Growth score","Profitability score","Cash flow quality",
        "Debt / balance sheet","Potential score"}
    if label in price_labels and isinstance(value,Real):return money(value)
    if label in score_labels and isinstance(value,Real):return f"{value:.2f}/100"
    if label in {"Confidence","Coverage"} and section in {"fundamental","levels","potential"} and isinstance(value,Real):
        return f"{value:.2f}%"
    if label in {"Expected upside","Downside risk"} and isinstance(value,Real):return f"{value:+.2f}%"
    if label=="Risk / reward" and isinstance(value,Real):return f"{value:.2f}x"
    return str(value)


def _first(mapping: dict,*names):
    for name in names:
        value=mapping.get(name)
        if value is not None:return value
    return None


def _available(*values):
    return next((value for value in values if value is not None),None)


def _fundamental_audit(fundamental: dict) -> dict:
    """Project optional provider provenance without inferring absent metadata."""
    audit=fundamental.get("audit") if isinstance(fundamental.get("audit"),dict) else {}
    audit_metadata=fundamental.get("audit_metadata") if isinstance(fundamental.get("audit_metadata"),dict) else {}
    metadata=fundamental.get("source_metadata") if isinstance(fundamental.get("source_metadata"),dict) else {}
    periods=fundamental.get("periods") if isinstance(fundamental.get("periods"),list) else []
    valid_periods=[period for period in periods if isinstance(period,dict) and period.get("period_end")]
    latest=max(valid_periods,key=lambda period:str(period["period_end"]),default={})
    consolidated=latest.get("consolidated")
    period_scope=("CONSOLIDATED" if consolidated is True else
        "UNCONSOLIDATED" if consolidated is False else None)
    return {
        "Source":_available(_first(fundamental,"source","provider_name"),_first(metadata,"source","provider"),
            _first(audit_metadata,"official_source","source"),audit.get("source")),
        "Statement as of":_available(_first(fundamental,"as_of","fetched_at","updated_at"),
            _first(metadata,"fetched_at","updated_at"),_first(audit_metadata,"fetched_at","updated_at"),
            _first(audit,"fetched_at","updated_at")),
        "Latest period":_available(_first(fundamental,"latest_period_end","period_end"),
            _first(metadata,"latest_period_end","period_end"),latest.get("period_end")),
        "Currency":_available(fundamental.get("currency"),metadata.get("currency"),audit.get("currency"),latest.get("currency")),
        "Unit":_available(fundamental.get("unit"),metadata.get("unit"),audit.get("unit"),latest.get("unit")),
        "Statement scope":_available(_first(fundamental,"consolidation_scope","statement_scope","scope"),
            _first(metadata,"consolidation_scope","statement_scope","scope"),
            period_scope,
            _first(audit_metadata,"consolidation_policy","consolidation_scope","statement_scope","scope"),
            _first(audit,"consolidation_scope","statement_scope","scope")),
    }


def investment_analysis(decision: dict) -> dict:
    """Normalize legacy/new decision payloads for read-only presentation."""
    fundamental=decision.get("fundamental") if isinstance(decision.get("fundamental"),dict) else {}
    levels=decision.get("technical_levels") if isinstance(decision.get("technical_levels"),dict) else {}
    potential=decision.get("potential") if isinstance(decision.get("potential"),dict) else {}
    breakdown=fundamental.get("score_breakdown") if isinstance(fundamental.get("score_breakdown"),dict) else {}
    entry=_available(_first(potential,"entry_price"),decision.get("entry_price"))
    # Stop, target and RR are meaningful only relative to a valid persisted entry.
    stop=_available(_first(potential,"downside_reference","stop_price"),decision.get("stop_price")) if _valid_price(entry) else None
    target=_available(_first(potential,"expected_target_price"),decision.get("expected_target_price")) if _valid_price(entry) else None
    risk_reward=_available(_first(potential,"risk_reward_ratio"),decision.get("risk_reward_ratio")) if _valid_price(entry) else None
    stop=stop if _valid_price(stop) else None
    target=target if _valid_price(target) else None
    risk_reward=risk_reward if _valid_number(risk_reward) else None
    audit=_fundamental_audit(fundamental)
    return {
        "fundamental":{
            "Provider status":_first(fundamental,"provider_status") or decision.get("fundamental_provider_status","UNAVAILABLE"),
            "Fundamental score":_available(_first(fundamental,"fundamental_score","score"),decision.get("fundamental_score")),
            "Effective fundamental":_available(_first(fundamental,"effective_score"),decision.get("effective_fundamental_score")),
            "Coverage":_available(_first(fundamental,"coverage"),decision.get("fundamental_coverage")),
            "Growth score":_available(_first(fundamental,"growth_score"),breakdown.get("growth_score")),
            "Profitability score":_available(_first(fundamental,"profitability_score"),breakdown.get("profitability_score")),
            "Cash flow quality":_available(_first(fundamental,"cash_flow_quality_score"),breakdown.get("cash_flow_quality_score")),
            "Debt / balance sheet":_available(_first(fundamental,"balance_sheet_score","debt_quality_score"),breakdown.get("balance_sheet_score")),
            "Valuation":_available(_first(fundamental,"valuation_status"),decision.get("valuation_status")),
            "Confidence":_available(_first(fundamental,"confidence"),decision.get("fundamental_confidence")),
            "Red flags":_first(fundamental,"red_flags") or decision.get("fundamental_red_flags") or [],
        },
        "fundamental_audit":audit,
        "levels":{
            "Support 1":_available(_first(levels,"support_1"),decision.get("support")),
            "Support 2":_first(levels,"support_2"),
            "Resistance 1":_available(_first(levels,"resistance_1"),decision.get("resistance")),
            "Resistance 2":_first(levels,"resistance_2"),
            "Market structure":_first(levels,"market_structure"),
            "Confidence":_first(levels,"confidence"),
        },
        "potential":{
            "Entry":entry if _valid_price(entry) else None,
            "Stop":stop,
            "Target":target,
            "Expected upside":_available(_first(potential,"expected_upside_pct"),decision.get("expected_upside_pct")),
            "Downside risk":_available(_first(potential,"downside_risk_pct"),decision.get("downside_risk_pct")),
            "Risk / reward":risk_reward,
            "Potential score":_available(_first(potential,"potential_score"),decision.get("potential_score")),
            "Confidence":_available(_first(potential,"target_confidence"),decision.get("target_confidence")),
        },
        "decision":{
            "Decision":decision.get("decision","HOLD"),
            "Reason code":decision.get("reason_code","UNKNOWN"),
            "Reason":decision.get("reason","Unavailable"),
        },
    }


def render_investment_analysis(decisions: list[dict]) -> None:
    st.subheader("Yatırım kalitesi ve giriş analizi" if st.session_state.get("dashboard_language","TR")=="TR" else "Investment quality and entry analysis")
    st.caption("Son PAPER döngüsünün salt okunur anlık görüntüsü. Eksik değerler tahmin edilmez." if st.session_state.get("dashboard_language","TR")=="TR" else "Read-only snapshots from the latest PAPER cycle. Missing values are never inferred.")
    if not decisions:
        st.info(ui("No persisted investment analysis is available yet. Run a PAPER cycle.","Henüz kaydedilmiş yatırım analizi yok. Bir PAPER döngüsü çalıştırın.")); return
    symbols=[item.get("symbol","UNKNOWN") for item in decisions]
    selected=st.selectbox(ui("Analysis symbol","Analiz sembolü"),symbols,key="investment_analysis_symbol")
    decision=next(item for item in decisions if item.get("symbol","UNKNOWN")==selected)
    view=investment_analysis(decision)
    for title,key in (("FUNDAMENTAL ANALYSIS","fundamental"),("TECHNICAL LEVELS","levels"),
                      ("POTENTIAL / RR","potential"),("FINAL DECISION","decision")):
        titles={"fundamental":ui("Fundamental analysis","Temel analiz"),"levels":ui("Technical levels","Teknik seviyeler"),
            "potential":ui("Potential / RR","Potansiyel / risk-getiri"),"decision":ui("Final decision","Nihai karar")}
        st.markdown(f"**{titles[key]}**")
        values=view[key]
        columns=st.columns(min(4,len(values)))
        for index,(label,value) in enumerate(values.items()):
            help_text={
                "Risk / reward":ui("Expected upside divided by downside risk (RR).","Beklenen yükselişin aşağı yönlü riske oranı."),
                "Fundamental score":ui("Raw score calculated from available fundamental metrics.","Mevcut temel göstergelerden hesaplanan ham puan."),
                "Effective fundamental":ui("Raw score reduced for incomplete coverage and lower confidence; used by the quality gate.","Eksik kapsama ve düşük güven için azaltılan, kalite eşiğinde kullanılan puan."),
                "Coverage":ui("Share of required fundamental metrics that were available.","Kullanılabilir olan gerekli temel göstergelerin oranı."),
                "Confidence":ui("Reliability estimate for the underlying analysis.","Analizin güvenilirlik tahmini."),
            }.get(label)
            shown=format_analysis_value(key,label,value)
            if shown=="Unavailable":shown=ui("Unavailable","Kullanılamıyor")
            if label=="Reason":shown=human_reason(values.get("Reason code"),value)
            label_tr={"Provider status":"Sağlayıcı durumu","Fundamental score":"Ham temel puan","Effective fundamental":"Etkin temel puan",
                "Coverage":"Kapsama","Confidence":"Güven","Growth score":"Büyüme puanı","Profitability score":"Kârlılık puanı",
                "Cash flow quality":"Nakit akış kalitesi","Debt / balance sheet":"Borç / bilanço","Valuation":"Değerleme",
                "Red flags":"Risk işaretleri","Support 1":"Destek 1","Support 2":"Destek 2","Resistance 1":"Direnç 1",
                "Resistance 2":"Direnç 2","Market structure":"Piyasa yapısı","Entry":"Giriş","Stop":"Zarar durdur",
                "Target":"Hedef","Expected upside":"Beklenen yükseliş","Downside risk":"Aşağı risk","Risk / reward":"Risk / getiri",
                "Potential score":"Potansiyel puanı","Decision":"Karar","Reason code":"Neden kodu","Reason":"Açıklama"}
            columns[index%len(columns)].metric(label_tr.get(label,label) if st.session_state.get("dashboard_language")=="TR" else label,shown,help=help_text)
        if key=="fundamental":
            status=str(values["Provider status"])
            if status=="PARTIAL":st.warning(ui("KAP fundamental coverage is partial; unavailable values remain UNKNOWN.","KAP temel veri kapsaması kısmi; eksik değerler BİLİNMİYOR olarak kalır."))
            elif status=="UNAVAILABLE":st.info(ui("KAP fundamentals are temporarily unavailable; technical-only fallback remains active.","KAP temel verileri geçici olarak kullanılamıyor; yalnızca teknik veriye dayalı yedek akış etkin."))
            audit={label:value for label,value in view["fundamental_audit"].items() if value not in (None,"")}
            if audit:
                st.caption(ui("KAP source / audit metadata","KAP kaynak / denetim bilgileri"))
                audit_columns=st.columns(min(3,len(audit)))
                audit_tr={"Source":"Kaynak","Statement as of":"Finansal tablo tarihi","Latest period":"Son dönem",
                    "Currency":"Para birimi","Unit":"Birim","Statement scope":"Finansal tablo kapsamı"}
                for index,(label,value) in enumerate(audit.items()):
                    shown_label=audit_tr.get(label,label) if st.session_state.get("dashboard_language")=="TR" else label
                    audit_columns[index%len(audit_columns)].metric(shown_label,format_analysis_value("fundamental_audit",label,value))


@st.cache_data(ttl=REFRESH_SECONDS,show_spinner=False)
def cached_live_positions(positions_key: tuple[tuple,...]) -> list[dict]:
    positions=[dict(item) for item in positions_key]
    return refresh_live_positions(
        positions,
        YahooBistProvider(
            yahoo_execution_freshness_seconds=SETTINGS.market_data.yahoo_execution_freshness_seconds
        ),
        refresh_seconds=REFRESH_SECONDS,
    )


def position_cache_key(positions: list[dict]) -> tuple[tuple,...]:
    # Include persisted values needed for display. A bot update invalidates the
    # cache even before its short TTL expires.
    return tuple(tuple(sorted(item.items())) for item in positions)


def dashboard():
    language=st.radio(TEXT["TR"]["language"],["TR","EN"],horizontal=True,key="dashboard_language")
    st.title("BISTBOT PAPER")
    st.markdown(f'<span class="paper-mode">MODE: PAPER</span> &nbsp; {TEXT[language]["paper_note"]}',unsafe_allow_html=True)
    st.caption(TEXT[language]["refresh"])
    try: data=DashboardDataService(DATABASE,SETTINGS).load()
    except Exception as error:
        st.error(ui("Dashboard could not read the PAPER database","Dashboard PAPER veritabanını okuyamadı")+f": {type(error).__name__}: {error}"); return
    summary=data["summary"]
    market_status=data["market_status"]
    st.header(t("market"),anchor=False)
    status_label=ui("OPEN","AÇIK") if market_status["can_execute_orders"] else ui("CLOSED","KAPALI")
    st.subheader(status_label)
    status_columns=st.columns(4)
    status_columns[0].metric(ui("Session","Seans"),market_status["session"])
    status_columns[1].metric(ui("Reason","Neden"),market_status["reason"].replace("MARKET_CLOSED_","").replace("_"," ").title())
    status_columns[2].metric(ui("Local time","Yerel saat"),market_status["local_time"])
    status_columns[3].metric(ui("Next order execution","Sonraki emir uygulaması"),ui("Allowed now"," Şu anda izinli") if market_status["can_execute_orders"] else ui("Not allowed until a valid BIST session","Geçerli BIST seansına kadar izin yok"))
    live_positions=cached_live_positions(position_cache_key(data["positions"]))
    live=live_portfolio_summary(cash=summary["cash"],positions=live_positions,
        initial_capital=summary["initial_capital"],bot_recorded_equity=summary["bot_recorded_equity"])
    total_pnl=live["live_equity"]-summary["initial_capital"]
    total_return=total_pnl/summary["initial_capital"]*100 if summary["initial_capital"] else 0
    st.header(t("overview"),anchor=False)
    values=((ui("Starting capital","Başlangıç sermayesi"),money(summary["initial_capital"])),(ui("Current live equity","Güncel canlı özsermaye"),money(live["live_equity"])),
        (ui("Total net P&L","Toplam net K/Z"),money(total_pnl)),(ui("Total net return","Toplam net getiri"),percentage(total_return)),
        (ui("Realized P&L","Gerçekleşen K/Z"),money(summary["realized"])),(ui("Unrealized P&L","Gerçekleşmemiş K/Z"),money(live["live_unrealized"])),
        (ui("Winning closed trades","Kârlı kapanan işlemler"),summary["winning_closed_trades"]),(ui("Losing closed trades","Zararlı kapanan işlemler"),summary["losing_closed_trades"]),
        (ui("Win rate","Kazanma oranı"),percentage(summary["win_rate"])))
    summary_cols=st.columns(3)
    for index,(label,value) in enumerate(values): summary_cols[index%3].metric(label,value)

    st.header(t("regime"),anchor=False)
    regime=data.get("market_regime",{})
    if not regime: st.info(ui("No persisted market-regime refresh yet. Run a PAPER cycle.","Henüz kaydedilmiş piyasa rejimi yenilemesi yok. Bir PAPER döngüsü çalıştırın."))
    else:
        cycle_regime=(data.get("cycle",{}).get("summary",{}))
        provider_status=cycle_regime.get("macro_provider_status","UNKNOWN")
        cols=st.columns(4)
        regime_values=((ui("Provider status","Sağlayıcı durumu"),provider_status,None),(ui("Regime","Rejim"),regime.get("regime"),ui("Market-wide risk state derived from persisted macro signals.","Kayıtlı makro sinyallerden türetilen piyasa geneli risk durumu.")),
                (ui("Risk","Risk"),f"{regime.get('market_risk_score',0)}/100",ui("Aggregate market-risk score.","Toplam piyasa risk puanı.")),
                (ui("Confidence","Güven"),regime.get("confidence"),ui("Confidence in the current market-regime classification.","Mevcut piyasa rejimi sınıflandırmasının güveni.")),
                (ui("Last update","Son güncelleme"),regime.get("last_updated"),None))
        for index,(label,value,help_text) in enumerate(regime_values):
            cols[index%len(cols)].metric(label,value,help=help_text)
        categories=regime.get("risk_categories",{})
        st.subheader(ui("Risk categories","Risk kategorileri"))
        category_columns=st.columns(min(4,max(1,len(categories))))
        category_tr={"geopolitical_risk":"Jeopolitik risk","monetary_policy":"Para politikası","inflation":"Enflasyon",
            "currency_risk":"Kur riski","market_stress":"Piyasa stresi","political_risk":"Siyasi risk","external_risk":"Dış risk"}
        for index,(label,value) in enumerate(categories.items()):
            shown=category_tr.get(label,label.replace("_"," ").title()) if language=="TR" else label.replace("_"," ").title()
            category_columns[index%len(category_columns)].metric(shown,value)
        st.subheader(ui("Latest material macro events","Son önemli makro olaylar"))
        events=pd.DataFrame(regime.get("material_events",[]))
        if events.empty: st.info(ui("No material macro events in the current regime window.","Mevcut rejim penceresinde önemli makro olay yok."))
        else:
            desired=[name for name in ("published_at","source","title","materiality_score","direction","affected_sectors") if name in events]
            st.dataframe(events[desired].rename(columns={"published_at":"Timestamp","source":"Source","title":"Headline",
                "materiality_score":"Materiality","direction":"Direction","affected_sectors":"Affected sectors"}),
                use_container_width=True,hide_index=True,column_config=table_labels(["Timestamp","Source","Headline","Materiality","Direction","Affected sectors"]))

    st.header(t("history"),anchor=False)
    st.subheader(t("recent_activity"))
    activity=pd.DataFrame(data["trades"])
    if activity.empty: st.info(ui("No PAPER activity recorded.","Kaydedilmiş PAPER etkinliği yok."))
    else:
        activity=display_timestamps(activity,"timestamp")
        activity=activity.rename(columns={"timestamp":"Timestamp","side":"Action","symbol":"Symbol","quantity":"Quantity",
            "price":"Price","realized_pnl":"P&L for SELL","reason":"Reason"})
        activity["Reason"]=activity["Reason"].map(lambda value:display_reason(value,language))
        st.dataframe(activity[["Timestamp","Action","Symbol","Quantity","Price","P&L for SELL","Reason"]].style.format(
            {"Price":"₺{:,.2f}","P&L for SELL":lambda value:"" if pd.isna(value) else f"₺{value:+,.2f}"}),
            use_container_width=True,hide_index=True,column_config=table_labels(["Timestamp","Action","Symbol","Quantity","Price","P&L for SELL","Reason"]))

    st.subheader(t("trade_history"))
    trades=pd.DataFrame(data["trades"])
    if trades.empty: st.info(ui("No PAPER fills recorded.","Kaydedilmiş PAPER gerçekleşmesi yok."))
    else:
        trades=display_timestamps(trades,"timestamp")
        trades["holding_duration"]=trades["holding_seconds"].map(lambda value:"—" if pd.isna(value) else duration(value))
        trades=trades.rename(columns={"timestamp":"Timestamp","symbol":"Symbol","side":"Side","quantity":"Quantity",
            "price":"Price","gross_value":"Gross Value","commission":"Commission","slippage":"Slippage",
            "final_score":"Final / Entry Score","reason":"Reason","realized_pnl":"Realized P&L TL",
            "realized_pnl_pct":"Realized P&L %","holding_duration":"Holding Duration","order_id":"Order ID","trade_id":"Trade ID"})
        trades["Reason"]=trades["Reason"].map(lambda value:display_reason(value,language))
        trade_display=trades[["Timestamp","Symbol","Side","Quantity","Price","Gross Value","Commission","Slippage",
            "Final / Entry Score","Reason","Realized P&L TL","Realized P&L %","Holding Duration","Order ID","Trade ID"]]
        trade_style=trade_display.style.format({"Price":"₺{:,.2f}","Gross Value":"₺{:,.2f}",
            "Commission":"₺{:,.2f}","Slippage":"₺{:,.2f}","Realized P&L TL":"₺{:+,.2f}",
            "Realized P&L %":"{:+.2f}%"},na_rep="")
        st.dataframe(trade_style,use_container_width=True,hide_index=True,column_config=table_labels(trade_display.columns))

    st.subheader(t("closed_positions"))
    closed=pd.DataFrame(data["closed_positions"])
    if closed.empty: st.info(ui("No completed PAPER positions recorded.","Kaydedilmiş kapanmış PAPER pozisyonu yok."))
    else:
        closed=display_timestamps(closed,"opened_at"); closed=display_timestamps(closed,"closed_at")
        closed["holding_time"]=closed["holding_seconds"].map(duration)
        closed=closed.rename(columns={"symbol":"Symbol","opened_at":"Opened At","closed_at":"Closed At",
            "buy_quantity":"Buy Quantity","average_buy_price":"Average Buy Price","average_sell_price":"Average Sell Price",
            "gross_buy_value":"Gross Buy Value","gross_sell_value":"Gross Sell Value","commission_costs":"Commission / Costs",
            "realized_pnl":"Realized P&L TL","realized_pnl_pct":"Realized P&L %","holding_time":"Holding Time",
            "entry_score":"Entry Score","exit_reason":"Exit Reason"})
        closed["Exit Reason"]=closed["Exit Reason"].map(lambda value:display_reason(value,language))
        closed_display=closed[["Symbol","Opened At","Closed At","Buy Quantity","Average Buy Price","Average Sell Price",
            "Gross Buy Value","Gross Sell Value","Commission / Costs","Realized P&L TL","Realized P&L %","Holding Time",
            "Entry Score","Exit Reason"]]
        closed_style=closed_display.style.format({"Average Buy Price":"₺{:,.2f}","Average Sell Price":"₺{:,.2f}",
            "Gross Buy Value":"₺{:,.2f}","Gross Sell Value":"₺{:,.2f}","Commission / Costs":"₺{:,.2f}",
            "Realized P&L TL":"₺{:+,.2f}","Realized P&L %":"{:+.2f}%"},na_rep="")
        st.dataframe(closed_style,use_container_width=True,hide_index=True,column_config=table_labels(closed_display.columns))

    st.header(t("portfolio"),anchor=False)
    risk=data["risk"]
    st.subheader(t("paper_controls"))
    control_cols=st.columns(3)
    controls=((ui("PAPER cash","PAPER nakit"),money(summary["cash"]),ui("Available PAPER cash recorded in the read-only database snapshot.","Salt okunur veritabanı anlık görüntüsündeki kullanılabilir PAPER nakdi.")),
        (ui("Open positions","Açık pozisyonlar"),summary["open_positions"],None),
        (ui("Effective max open positions","Etkin azami açık pozisyon"),risk["max_open_positions"],ui("The effective limit after applying any runtime override.","Varsa çalışma zamanı geçersiz kılması uygulandıktan sonraki etkin sınır.")),
        (ui("Configured max","Yapılandırılan azami"),risk.get("configured_max_open_positions",risk["max_open_positions"]),ui("The configured limit before a runtime override.","Çalışma zamanı geçersiz kılmasından önceki yapılandırılmış sınır.")),
        (ui("Runtime override","Geçici sınır"),ui("Unavailable","Kullanılamıyor") if risk.get("max_open_positions_override") is None else str(risk["max_open_positions_override"]),ui("A persisted override, if present. This dashboard cannot change it.","Varsa kaydedilmiş geçici sınır. Dashboard bunu değiştiremez.")))
    for index,(label,value,help_text) in enumerate(controls): control_cols[index%3].metric(label,value,help=help_text)
    st.caption(t("read_only"))

    st.subheader(t("open_positions"))
    positions=pd.DataFrame(live_positions)
    if positions.empty: st.info(ui("No open PAPER positions.","Açık PAPER pozisyonu yok."))
    else:
        sort_options={ui("P&L %","K/Z %"):"live_unrealized_pnl_pct",ui("Market value","Piyasa değeri"):"live_market_value",ui("Current score","Güncel puan"):"current_score",ui("Symbol","Sembol"):"symbol"}
        sort_label=st.selectbox(ui("Sort positions by","Pozisyonları sırala"),list(sort_options))
        ascending=sort_options[sort_label]=="symbol"; positions=positions.sort_values(sort_options[sort_label],ascending=ascending,na_position="last")
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
        styled=display[columns_to_show].style.format({"Average Entry Price":"₺{:,.2f}","Live Price":"₺{:,.2f}",
            "Live Market Value":"₺{:,.2f}","Live P&L TL":"₺{:+,.2f}","Live P&L %":"{:+.2f}%",
            "Stop Price":"₺{:,.2f}","Take Profit Price":"₺{:,.2f}","Trailing Stop":"₺{:,.2f}",
            "Highest Price Since Entry":"₺{:,.2f}"}).map(
                lambda value:"color:#159957;font-weight:600" if isinstance(value,(int,float)) and value>0 else
                             "color:#d9534f;font-weight:600" if isinstance(value,(int,float)) and value<0 else "",
                subset=["Live P&L TL","Live P&L %"])
        st.dataframe(styled,use_container_width=True,hide_index=True,column_config=table_labels(columns_to_show))

        st.subheader(ui("Position detail","Pozisyon ayrıntısı"))
        selected=st.selectbox(ui("Symbol","Sembol"),positions["symbol"].tolist()); item=next(row for row in live_positions if row["symbol"]==selected)
        details={ui("Entry price","Giriş fiyatı"):money(item["average_entry"]),ui("Live price","Canlı fiyat"):money(item["live_price"]),ui("Quantity","Adet"):item["quantity"],
            ui("Live position value","Canlı pozisyon değeri"):money(item["live_market_value"]),ui("Live P&L","Canlı K/Z"):f"{money(item['live_unrealized_pnl'])} ({percentage(item['live_unrealized_pnl_pct'])})",
            ui("Entry score","Giriş puanı"):item["entry_score"],ui("Current score","Güncel puan"):item["current_score"],ui("Stop","Zarar durdur"):money(item["stop_price"]),
            ui("Take profit","Kâr al"):money(item["take_profit_price"]),ui("Trailing stop","İzleyen stop"):money(item["trailing_stop"]),
            ui("Highest since entry","Girişten beri en yüksek"):money(item["highest_price"]),ui("Opened at","Açılış"):item["opened_at"],ui("Updated at","Güncelleme"):item["updated_at"],
            ui("Price timestamp","Fiyat zamanı"):item["price_timestamp_display"],ui("Price age","Fiyat yaşı"):item["price_age"],ui("Price status","Fiyat durumu"):item["price_status"]}
        detail_columns=st.columns(4)
        for index,(label,value) in enumerate(details.items()): detail_columns[index%4].metric(label,value)
        st.info(ui("Per-symbol historical prices are not persisted. No synthetic position chart is shown.","Sembol bazında tarihsel fiyatlar saklanmıyor; yapay pozisyon grafiği gösterilmiyor."))

    st.subheader(t("performance"))
    history=pd.DataFrame(data["history"])
    if history.empty: st.info(ui("No stored portfolio snapshots available.","Kayıtlı portföy anlık görüntüsü yok."))
    else:
        history["timestamp"]=parse_dashboard_timestamps(history["timestamp"])
        invalid=int(history["timestamp"].isna().sum()); history=history.dropna(subset=["timestamp"])
        if invalid: st.warning(ui(f"Ignored {invalid} malformed portfolio timestamp row(s); persisted data was not changed.",f"{invalid} bozuk portföy zaman damgası satırı yok sayıldı; kayıtlı veri değiştirilmedi."))
        if history.empty: st.info(ui("No valid stored portfolio snapshots available.","Geçerli kayıtlı portföy anlık görüntüsü yok."))
        else:
            history["timestamp"]=history["timestamp"].dt.tz_convert("Europe/Istanbul")
            starting_label=ui("Starting capital","Başlangıç sermayesi")
            history[starting_label]=summary["initial_capital"]
            st.line_chart(history.set_index("timestamp")[["equity","cash",starting_label]].rename(
                columns={"equity":ui("Portfolio equity","Portföy özsermayesi"),"cash":ui("Cash","Nakit")}))

    st.header(t("candidates"),anchor=False)
    st.subheader(t("bot_activity"))
    cycle=data["cycle"]; cycle_summary=cycle.get("summary",{})
    if not cycle: st.info(ui("No completed-cycle audit record yet. It will appear after the next bot cycle.","Henüz tamamlanmış döngü denetim kaydı yok; sonraki bot döngüsünden sonra görünecek."))
    else:
        fields=("market_data_success","symbols_valid","scanner_candidates","llm_candidates","llm_api_attempts",
            "buy_signals","sell_signals","entry_orders","exit_orders","news_provider_status","kap_provider_status","llm_provider_status")
        st.caption(ui("Raw cycle diagnostics (canonical field names)","Ham döngü tanılama verisi (kanonik alan adları)"))
        st.json({"last_cycle":cycle["timestamp"],**{field:cycle_summary.get(field) for field in fields}})
    decisions=pd.DataFrame(data["decisions"])
    st.subheader("Son kararlar" if language=="TR" else "Recent decisions")
    if decisions.empty: st.info(ui("No persisted cycle decisions available.","Kaydedilmiş döngü kararı yok."))
    else:
        decisions["explanation"]=[human_reason(row.get("reason_code"),row.get("reason"),language) for row in data["decisions"]]
        desired=[name for name in ("symbol","scanner_score","final_score","decision","signal_mode","explanation") if name in decisions]
        st.dataframe(decisions[desired],use_container_width=True,hide_index=True,column_config={
            "symbol":ui("Symbol","Sembol"),
            "scanner_score":st.column_config.NumberColumn("Scanner",format="%.2f"),
            "final_score":st.column_config.NumberColumn(ui("Final","Nihai"),format="%.2f"),
            "decision":ui("Decision","Karar"),"signal_mode":ui("Signal mode","Sinyal modu"),
            "explanation":st.column_config.TextColumn("Açıklama" if language=="TR" else "Explanation",width="large"),
        })
    render_investment_analysis(data["decisions"])

    st.header(t("risk"),anchor=False)
    cols=st.columns(4)
    risk_values=((ui("Cash %","Nakit %"),percentage(risk["cash_pct"])),(ui("Invested %","Yatırılmış %"),percentage(risk["invested_pct"])),
        (ui("Open / max","Açık / azami"),f"{summary['open_positions']} / {risk['max_open_positions']}"),(ui("Minimum cash","Asgari nakit"),f"{risk['min_cash_pct']:.2f}%"),
        (ui("Daily loss limit","Günlük zarar sınırı"),f"{risk['daily_loss_limit']:.2f}%"),(ui("Weekly loss limit","Haftalık zarar sınırı"),f"{risk['weekly_loss_limit']:.2f}%"),
        (ui("Drawdown limit","Gerileme sınırı"),f"{risk['drawdown_limit']:.2f}%"))
    for index,(label,value) in enumerate(risk_values): cols[index%len(cols)].metric(label,value)


def run_dashboard_app():
    if hasattr(st,"fragment"):
        st.fragment(run_every=f"{REFRESH_SECONDS}s")(dashboard)()
    else:
        # Streamlit <1.37 compatibility. This refreshes only the browser page;
        # dashboard() remains read-only outside explicit confirmed control handlers.
        st.markdown(f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">',unsafe_allow_html=True)
        dashboard()


if __name__=="__main__":run_dashboard_app()

from __future__ import annotations

from datetime import datetime,timezone
from hashlib import sha256
import re
import unicodedata

from bistbot.app.models import MacroEvent,MacroRiskCategory,TimestampSource

# Ordered, deterministic topic rules. Materiality expresses market importance
# only; source reliability remains a separate signal.
TOPIC_RULES = (
    ("FX_CAPITAL_CONTROL_POLICY",98,("capital control","sermaye kontrol","döviz işlemlerine kısıtlama","currency restriction")),
    ("CENTRAL_BANK_RATE_DECISION",95,("faiz oranlarına ilişkin basın duyurusu","unexpected rate decision","interest rate decision",
        "interest-rate decision","fomc statement","issues fomc statement","monetary policy decisions","policy rate decision")),
    ("BANKING_SYSTEM_REGULATION",88,("banking system regulation","bankacılık sistemi düzenleme","bank resolution","deposit freeze","mevduat dondur")),
    ("MACROPRUDENTIAL_POLICY",86,("makroihtiyati çerçeve","macroprudential framework","macro-prudential policy")),
    ("LIQUIDITY_POLICY",82,("türk lirası likidite yönetimi","liquidity management","emergency liquidity","likidite politikası")),
    ("INFLATION_REPORT",80,("enflasyon raporu","inflation report","inflation outlook report")),
    ("ECONOMIC_PROJECTIONS",78,("economic projections","summary of economic projections","ekonomik projeksiyon")),
    ("CENTRAL_BANK_MINUTES",76,("para politikası kurulu toplantı özeti","minutes of the federal open market committee",
        "monetary policy meeting account","meeting of the monetary policy committee","discount rate meetings")),
    ("WAR_ESCALATION",92,("war escalation","savaş ilan","military escalation","invasion")),
    ("SHIPPING_DISRUPTION",82,("shipping route closed","boğaz kapat","suez closed","hormuz closed")),
    ("MAJOR_EARTHQUAKE",90,("major earthquake","büyük deprem","7.0 earthquake")),
    ("BANKING_SYSTEM_EVENT",94,("bank run","banking crisis","bank failure")),
    ("SOVEREIGN_CREDIT_EVENT",86,("sovereign default","ülke temerrüt","credit rating downgrade")),
    ("POSITIVE_POLICY_SHOCK",78,("credible disinflation package","unexpected stimulus","sovereign rating upgrade")),
    ("MARKET_PRICE_SHOCK",82,("fx shock","currency crash","kur şoku","oil shock","brent shock","vix shock","yield shock","market crash")),
    ("DIGITAL_CURRENCY_OPERATIONAL",30,("digital currency","dijital türk lirası","digital euro app","tokenised financial market")),
    ("PAYMENT_SYSTEM_ADMIN",22,("fast sistemi işlem tutar","payment system","ödeme ve elektronik para","hesap sahipliği sorgulama")),
    ("SPEECH_INTERVIEW",45,("speech","interview","konuşma","sunumu","panel remarks","outlook for the")),
    ("ACADEMIC_CEREMONIAL",7,("makale yarışması","essay competition","academic competition","ödül töreni","ceremonial")),
    ("PUBLIC_EVENT",3,("concert","konser","open air","invite the public")),
)

ROUTINE=("routine","toplantı takvimi","meeting schedule","technical maintenance","sertifika değişikliği")

def canonical_event_id(source: str,source_id: str,title: str) -> str:
    normalized=re.sub(r"\W+"," ",title.lower()).strip()
    return sha256(f"{source.lower()}|{source_id}|{normalized}".encode()).hexdigest()[:24]

def freshness_weight(age_hours: float,timestamp_source: TimestampSource|str=TimestampSource.SOURCE) -> float:
    if TimestampSource(timestamp_source) is TimestampSource.FETCH_FALLBACK:return 0.0
    if age_hours<6:return 1.0
    if age_hours<24:return .85
    if age_hours<72:return .60
    if age_hours<168:return .25
    return .05

def classify_macro_event(event: MacroEvent,*,now: datetime|None=None) -> MacroEvent:
    text=_normalize(f"{event.title} {event.body}"); event_type="ROUTINE"; score=15
    for candidate,value,terms in TOPIC_RULES:
        if any(term in text for term in terms):event_type,score=candidate,value; break
    if event_type=="ROUTINE" and not any(term in text for term in ROUTINE):score=35
    now=_aware(now or event.fetched_at); published=_aware(event.published_at)
    age=max(0,(now-published).total_seconds()/3600); fresh=freshness_weight(age,event.timestamp_source)
    reliability_factor=.5+.5*event.source_reliability/100
    confirmation_factor=.5+.5*event.confirmation_score/100
    effective=round(score*fresh*reliability_factor*confirmation_factor,2)
    level="HIGH" if score>=70 else "MEDIUM" if score>=40 else "LOW"
    if event_type=="POSITIVE_POLICY_SHOCK":direction="POSITIVE"
    elif event_type in {"WAR_ESCALATION","SHIPPING_DISRUPTION","MAJOR_EARTHQUAKE","BANKING_SYSTEM_EVENT",
                        "SOVEREIGN_CREDIT_EVENT","MARKET_PRICE_SHOCK","FX_CAPITAL_CONTROL_POLICY"}:direction="NEGATIVE"
    elif event_type in {"ROUTINE","DIGITAL_CURRENCY_OPERATIONAL","PAYMENT_SYSTEM_ADMIN","SPEECH_INTERVIEW","ACADEMIC_CEREMONIAL","PUBLIC_EVENT"}:direction="NEUTRAL"
    else:direction="MIXED"
    contribution=effective*(1 if direction=="NEGATIVE" else .75 if direction=="MIXED" else 0)
    return event.model_copy(update={"event_type":event_type,"materiality_score":float(score),"materiality":level,
        "direction":direction,"age_hours":round(age,2),"freshness_weight":fresh,
        "effective_materiality":effective,"regime_contribution":round(contribution,2)})

def category_for_text(text: str) -> MacroRiskCategory:
    value=_normalize(text)
    if any(x in value for x in ("usdtry","eurtry","currency"," kur ","döviz")):return MacroRiskCategory.FX
    if any(x in value for x in ("oil","brent","gold","petrol","altın")):return MacroRiskCategory.COMMODITY
    if any(x in value for x in ("war","savaş","geopolit","invasion")):return MacroRiskCategory.GEOPOLITICAL
    if any(x in value for x in ("earthquake","deprem","banking crisis")):return MacroRiskCategory.SYSTEMIC_EVENT
    if any(x in value for x in ("fed","fomc","ecb","s&p","nasdaq","global")):return MacroRiskCategory.GLOBAL_MARKET
    if any(x in value for x in ("spk","bist","tax","vergi","regulation")):return MacroRiskCategory.DOMESTIC_POLICY
    return MacroRiskCategory.TURKEY_MACRO

def _normalize(value: str) -> str:
    # Python lowercases Turkish capital İ to i + combining dot. Remove only
    # combining marks so deterministic Turkish keyword matching remains stable.
    lowered=unicodedata.normalize("NFC",value.lower().replace("i\u0307","i"))
    return re.sub(r"\s+"," ",lowered).strip()
def _aware(value: datetime) -> datetime:return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

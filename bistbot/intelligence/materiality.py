from __future__ import annotations

import re

from bistbot.app.models import EventItem,EventSourceType


RULES: tuple[tuple[str,int,tuple[str,...]],...]=(
    ("CONTRACT",85,("sözleşme","contract","sipariş","order")),
    ("M&A",90,("satın alma","acquisition","birleşme","merger","devral")),
    ("INVESTMENT",80,("yatırım","investment","yeni tesis","new facility")),
    ("CAPACITY_INCREASE",80,("kapasite art","capacity expansion","capacity increase")),
    ("EARNINGS",75,("finansal sonuç","financial result","net kar","net profit","earnings","profit warning")),
    ("FINANCING",70,("finansman","financing","borç yapılandır","debt restructuring","kredi")),
    ("DIVIDEND",75,("temettü","dividend")),
    ("BUYBACK",75,("geri alım","buyback")),
    ("REGULATORY_DECISION",65,("düzenleyici onay","regulatory approval","spk onay","rekabet kurulu")),
    ("PRODUCTION",75,("üretim dur","production disruption","üretime ara","production")),
    ("LICENSE_PERMIT",70,("lisans","license","ruhsat","permit")),
)


def classify_event(event: EventItem) -> EventItem:
    """Deterministic quality classification; it does not infer direction."""
    text=" ".join(f"{event.title} {event.body}".lower().split())
    event_type,materiality,reason="OTHER",30,"no configured material theme"
    low_rules=(
        ("CIRCUIT_BREAKER",12,("devre kesici","circuit breaker")),
        ("SUSTAINABILITY_REPORT",15,("sürdürülebilirlik raporu","sustainability report")),
        ("ROUTINE_MKK_NOTICE",10,("merkezi kayıt kuruluşu","mkk", "dönüşüm")),
        ("GENERIC_ADMINISTRATIVE_NOTICE",15,("bildirim sorgu","genel kurul tescil","administrative notice")),
    )
    for category,score,terms in low_rules:
        if any(term in text for term in terms):
            event_type,materiality,reason=category,score,"routine/market administrative context"
            break
    else:
        for category,score,terms in RULES:
            matched=[term for term in terms if term in text]
            if matched:
                event_type,materiality,reason=category,score,f"matched material theme: {matched[0]}"
                break
    # A title with no meaningful body must not receive high confidence by itself.
    if len(event.body.strip())<30 and materiality>=60:
        materiality=min(materiality,45); reason+="; insufficient disclosure content"
    reliability=event.trust_score
    verification="PRIMARY_CONFIRMED" if event.source_type is EventSourceType.KAP else "SOURCE_ONLY"
    return event.model_copy(update={"event_type":event_type,"materiality_score":materiality,
        "source_reliability":reliability,"verification":verification,"materiality_reason":reason})


def classify_events(events: list[EventItem]) -> list[EventItem]:
    return [classify_event(event) for event in events]

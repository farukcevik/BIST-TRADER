from __future__ import annotations

from hashlib import sha256
import json

from bistbot.app.models import MacroEvent,MacroLLMAnalysis

SYSTEM_PROMPT="""You analyze only supplied verified, high-materiality macro events for PAPER risk context.
Return strict JSON with: regime, market_risk_score, confidence, event_summary,
expected_market_direction, expected_duration, affected_sectors, positive_sectors,
negative_sectors, uncertainty, source_ids. Never recommend or execute a trade.
Do not invent company exposure or sources."""

class MacroLLMAnalyst:
    def __init__(self,provider,repository,*,model_name: str,threshold: float=70):
        self.provider=provider; self.repository=repository; self.model_name=model_name; self.threshold=threshold
        self.api_calls=0; self.cache_avoided_calls=0; self.last_sent_ids: set[str]=set(); self.last_analyzed_ids: set[str]=set()
    def analyze(self,events:list[MacroEvent],*,event_driven:bool=False)->MacroLLMAnalysis|None:
        eligible=sorted((event for event in events if event.materiality_score>=self.threshold
            and event.freshness_weight>0 and event.effective_materiality>=self.threshold*.5),key=lambda e:e.canonical_event_id)
        self.last_sent_ids=set(); self.last_analyzed_ids=set()
        if not eligible or getattr(self.provider,"provider_mode","DISABLED")!="REAL":return None
        self.last_sent_ids={e.canonical_event_id for e in eligible}
        signature=sha256("|".join(f"{e.canonical_event_id}:{e.materiality_score}" for e in eligible).encode()).hexdigest()
        cached=self.repository.get_llm(signature,self.model_name)
        if cached:self.cache_avoided_calls+=1; self.last_analyzed_ids={e.canonical_event_id for e in eligible}; return cached
        if event_driven and all(self.repository.has_event(e.canonical_event_id) for e in eligible):
            self.cache_avoided_calls+=1; return None
        payload=[{"canonical_event_id":e.canonical_event_id,"source_id":e.source_id,"source":e.source,
            "headline":e.title,"body":e.body,"timestamp":e.published_at.isoformat(),"category":e.category.value,
            "materiality":e.materiality_score,"freshness_weight":e.freshness_weight,
            "effective_materiality":e.effective_materiality} for e in eligible]
        self.api_calls+=1
        completion=self.provider.complete(system_prompt=SYSTEM_PROMPT,input_json=json.dumps({"events":payload},ensure_ascii=False))
        analysis=MacroLLMAnalysis.model_validate_json(completion.content)
        supplied={e.source_id for e in eligible}
        if not analysis.source_ids or not set(analysis.source_ids)<=supplied:raise ValueError("macro LLM cited unknown source_ids")
        self.repository.add_llm(signature,self.model_name,analysis); self.last_analyzed_ids={e.canonical_event_id for e in eligible}
        return analysis

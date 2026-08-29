from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from typing import Protocol

from pydantic import ValidationError

from bistbot.app.models import Action, LLMAnalysis, LLMAnalysisInput, LLMCompletion,LLMStatus
from bistbot.storage.repositories import LLMAnalysisRepository


class LLMProvider(Protocol):
    def complete(self, *, system_prompt: str, input_json: str) -> LLMCompletion: ...


class MockLLMProvider:
    provider_mode = "MOCK"
    def __init__(self, responses: Sequence[LLMCompletion | Exception]):
        self.responses, self.calls = list(responses), []

    def complete(self, *, system_prompt: str, input_json: str) -> LLMCompletion:
        self.calls.append({"system_prompt":system_prompt,"input_json":input_json})
        if not self.responses: raise RuntimeError("no mock response configured")
        response = self.responses.pop(0)
        if isinstance(response, Exception): raise response
        return response


PROMPT_VERSION = "llm-analysis-v3-material-evidence"
SYSTEM_PROMPT = """You are a bounded BIST evidence analyst. Use only the supplied technical data and events.
Return one JSON object matching the requested schema. Never invent source IDs. Do not size or execute orders,
change risk limits, or introduce facts absent from the input. If evidence is insufficient, return HOLD with
confidence at most 40. Never infer the meaning of an unclear disclosure title. If the supplied disclosure
content does not establish its meaning, explicitly state UNKNOWN/INSUFFICIENT_CONTEXT. Ambiguous administrative
events are neither bullish nor bearish. Assess only facts explicitly present in event content; a title alone
cannot support high confidence. Scores are 0-100, except sentiment is -100 to 100."""


class LLMAnalyst:
    def __init__(self, provider: LLMProvider, repository: LLMAnalysisRepository,
                 *, model_name: str, prompt_version: str = PROMPT_VERSION, max_candidates: int = 10,
                 clock=time.monotonic):
        if max_candidates <= 0: raise ValueError("max_candidates must be positive")
        self.provider, self.repository, self.model_name = provider, repository, model_name
        self.prompt_version, self.max_candidates, self.clock = prompt_version, max_candidates, clock
        self.api_calls = 0; self.api_analyses_completed = 0; self.cache_avoided_calls=0

    def analyze(self,candidates:Sequence[LLMAnalysisInput],*,event_driven:bool=False)->list[LLMAnalysis]:
        if len(candidates) > self.max_candidates:
            raise ValueError(f"candidate limit exceeded: {len(candidates)} > {self.max_candidates}")
        return [self._analyze_one(candidate,event_driven=event_driven) for candidate in candidates]

    def _analyze_one(self,candidate:LLMAnalysisInput,*,event_driven:bool=False)->LLMAnalysis:
        self._validate_input(candidate)
        event_hashes = sorted({event.hash for event in candidate.events})
        market_hash = self._market_state_hash(candidate)
        input_hash = self._input_hash(candidate.symbol, event_hashes, market_hash)
        cached = self.repository.get(input_hash, self.model_name, self.prompt_version)
        if cached is not None:self.cache_avoided_calls+=1; return cached
        if event_driven and event_hashes:
            cached=self.repository.get_for_event_hashes(event_hashes,self.model_name,self.prompt_version)
            if cached is not None:self.cache_avoided_calls+=1; return cached
        if not candidate.events:
            analysis = hold_analysis(candidate.symbol,"No relevant news/KAP event; LLM not required",confidence=0,
                                     status=LLMStatus.NOT_REQUIRED)
            self.repository.add(input_hash=input_hash,market_state_hash=market_hash,event_hashes=event_hashes,
                model_name=self.model_name,prompt_version=self.prompt_version,analysis=analysis,latency_ms=0,
                input_tokens=None,output_tokens=None,total_tokens=None,status="INSUFFICIENT_EVIDENCE")
            return analysis
        payload = self._prompt_payload(candidate); started = self.clock()
        try:
            self.api_calls += 1
            completion = self.provider.complete(system_prompt=SYSTEM_PROMPT, input_json=json.dumps(payload,sort_keys=True,ensure_ascii=False))
            latency_ms = max(0.0,(self.clock()-started)*1000)
            analysis = validate_analysis(completion.content,candidate.symbol,{event.id for event in candidate.events})
            status = "VALID" if analysis.summary != "Invalid LLM response" else "INVALID_OUTPUT"
            if status == "VALID": self.api_analyses_completed += 1
            tokens = (completion.input_tokens,completion.output_tokens,completion.total_tokens)
            actual_model = completion.model_name
            if actual_model != self.model_name:
                analysis = hold_analysis(candidate.symbol,"Unexpected LLM model response",confidence=0)
                status = "MODEL_MISMATCH"
        except Exception as error:
            latency_ms = max(0.0,(self.clock()-started)*1000)
            analysis = hold_analysis(candidate.symbol,f"LLM provider unavailable: {type(error).__name__}",confidence=0,
                                     status=LLMStatus.UNAVAILABLE)
            status, tokens = "PROVIDER_ERROR", (None,None,None)
        self.repository.add(input_hash=input_hash,market_state_hash=market_hash,event_hashes=event_hashes,
            model_name=self.model_name,prompt_version=self.prompt_version,analysis=analysis,latency_ms=latency_ms,
            input_tokens=tokens[0],output_tokens=tokens[1],total_tokens=tokens[2],status=status)
        return analysis

    @staticmethod
    def _validate_input(candidate: LLMAnalysisInput) -> None:
        if candidate.technical_signal.symbol != candidate.symbol: raise ValueError("technical signal symbol mismatch")
        if any(event.symbol != candidate.symbol for event in candidate.events): raise ValueError("event symbol mismatch")

    def _input_hash(self,symbol: str,event_hashes: list[str],market_hash: str) -> str:
        value = {"symbol":symbol,"events":event_hashes,"market":market_hash,
                 "model":self.model_name,"prompt_version":self.prompt_version}
        return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _market_state_hash(candidate: LLMAnalysisInput) -> str:
        signal = candidate.technical_signal
        state = {"technical_score":round(signal.technical_score,4),"momentum_score":round(signal.momentum_score,4),
            "volume_score":round(signal.volume_score,4),"liquidity_score":round(signal.liquidity_score,4),
            "volatility_score":round(signal.volatility_score,4),"overall_scanner_score":round(signal.overall_scanner_score,4),
            "metrics":{key:round(value,4) if isinstance(value,float) else value for key,value in sorted(signal.metrics.items())},
            "portfolio_exposure_pct":None if candidate.portfolio_exposure_pct is None else round(candidate.portfolio_exposure_pct,2)}
        return hashlib.sha256(json.dumps(state,sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _prompt_payload(candidate: LLMAnalysisInput) -> dict:
        return {"symbol":candidate.symbol,"technical":candidate.technical_signal.model_dump(mode="json"),
            "events":[{"id":event.id,"hash":event.hash,"source":event.source,"source_type":event.source_type.value,
                       "title":event.title,"body":event.body,"published_at":event.published_at.isoformat(),
                       "event_type":event.event_type,"materiality":event.materiality_score,
                       "source_reliability":event.source_reliability,"verification":event.verification}
                      for event in candidate.events],"portfolio_exposure_pct":candidate.portfolio_exposure_pct,
            "required_output_schema":LLMAnalysis.model_json_schema()}


def validate_analysis(payload: str, symbol: str, allowed_source_ids: set[str] | None = None) -> LLMAnalysis:
    try:
        result = LLMAnalysis.model_validate_json(payload)
        if result.symbol != symbol: raise ValueError("symbol mismatch")
        allowed = allowed_source_ids or set()
        if not set(result.source_ids).issubset(allowed): raise ValueError("fabricated source id")
        if result.action_bias is not Action.HOLD and not result.source_ids: raise ValueError("directional bias lacks evidence")
        if not allowed and (result.action_bias is not Action.HOLD or result.confidence > 40):
            raise ValueError("insufficient evidence")
        return result
    except (ValidationError,ValueError,TypeError):
        return hold_analysis(symbol,"Invalid LLM response",confidence=0,status=LLMStatus.INVALID)


def hold_analysis(symbol: str,reason: str,confidence: int=0,status: LLMStatus=LLMStatus.INVALID) -> LLMAnalysis:
    return LLMAnalysis(symbol=symbol,sentiment=0,importance=0,catalyst_score=0,priced_in_probability=50,
        risk_score=50,confidence=min(confidence,40),time_horizon="none",action_bias=Action.HOLD,
        summary=reason,bull_case="",bear_case="",risks=[],source_ids=[],llm_status=status)

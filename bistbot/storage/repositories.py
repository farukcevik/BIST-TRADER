from __future__ import annotations

import json
from bistbot.app.models import EventItem, LLMAnalysis, NewsItem, RankedEventCandidate, RiskDecision, TechnicalSignal, TradeSignal
from .database import Database


class NewsRepository:
    def __init__(self, database: Database, table: str = "news_items"):
        if table not in {"news_items", "kap_items"}: raise ValueError("unsupported table")
        self.database, self.table = database, table

    def add(self, item: NewsItem) -> bool:
        cursor = self.database.execute(f"INSERT OR IGNORE INTO {self.table}(source_id,source,url,timestamp,title,body,symbol,relevance,content_hash) VALUES(?,?,?,?,?,?,?,?,?)",
            (item.source_id,item.source,item.url,item.timestamp.isoformat(),item.title,item.body,item.symbol,item.relevance,item.content_hash))
        return cursor.rowcount == 1


class DecisionRepository:
    def __init__(self, database: Database): self.database = database

    def add_signal(self, signal: TradeSignal) -> None:
        self.database.execute("INSERT INTO trade_signals VALUES(?,?,?,?,?,?,?,?)", (str(signal.id), signal.timestamp.isoformat(),
            signal.symbol, signal.action.value, signal.score, signal.reason, signal.strategy_version, signal.requested_price))

    def add_risk_decision(self, decision: RiskDecision) -> None:
        self.database.execute("INSERT INTO risk_decisions(timestamp,signal_id,approved,reason,quantity,payload) VALUES(?,?,?,?,?,?)",
            (decision.timestamp.isoformat(), str(decision.signal_id), int(decision.approved), decision.reason,
             decision.approved_quantity, json.dumps(decision.metadata)))


class TechnicalSignalRepository:
    def __init__(self, database: Database): self.database = database

    def add(self, signal: TechnicalSignal, rank: int) -> None:
        payload = signal.model_dump(mode="json")
        payload["rank"] = rank
        self.database.execute("INSERT INTO technical_signals(timestamp,symbol,score,payload) VALUES(?,?,?,?)",
            (signal.timestamp.isoformat(), signal.symbol, signal.overall_scanner_score, json.dumps(payload)))


class EventRepository:
    def __init__(self, database: Database): self.database = database

    def add(self, event: EventItem) -> bool:
        cursor = self.database.execute("INSERT OR IGNORE INTO event_items VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (event.id,event.symbol,event.source,event.source_type.value,event.title,event.body,event.url,
             event.published_at.isoformat(),event.fetched_at.isoformat(),event.hash,event.trust_score))
        return cursor.rowcount == 1


class IntelligenceRankingRepository:
    def __init__(self, database: Database): self.database = database

    def add(self, cycle_id: str, rank: int, item: RankedEventCandidate, created_at) -> None:
        self.database.execute("INSERT INTO intelligence_rankings(cycle_id,created_at,rank,symbol,scanner_score,event_score,combined_score,payload) VALUES(?,?,?,?,?,?,?,?)",
            (cycle_id,created_at.isoformat(),rank,item.symbol,item.scanner_score,item.event_score,item.combined_score,item.model_dump_json()))

    def add_error(self, cycle_id: str, source: str, reason: str, created_at) -> None:
        self.database.execute("INSERT INTO system_events(timestamp,level,event_type,message,payload) VALUES(?,?,?,?,?)",
            (created_at.isoformat(),"WARNING","INTELLIGENCE_PROVIDER_ERROR",f"{source} unavailable",json.dumps({"cycle_id":cycle_id,"reason":reason})))


class LLMAnalysisRepository:
    def __init__(self, database: Database): self.database = database

    def get(self, input_hash: str, model_name: str, prompt_version: str) -> LLMAnalysis | None:
        rows = self.database.query("SELECT output FROM llm_analysis_cache WHERE input_hash=? AND model_name=? AND prompt_version=?",
                                   (input_hash,model_name,prompt_version))
        return LLMAnalysis.model_validate_json(rows[0]["output"]) if rows else None

    def add(self, *, input_hash: str, market_state_hash: str, event_hashes: list[str], model_name: str,
            prompt_version: str, analysis: LLMAnalysis, latency_ms: float, input_tokens: int | None,
            output_tokens: int | None, total_tokens: int | None, status: str) -> None:
        from datetime import datetime
        self.database.execute("INSERT OR REPLACE INTO llm_analysis_cache(created_at,input_hash,market_state_hash,event_hashes,symbol,model_name,prompt_version,output,latency_ms,input_tokens,output_tokens,total_tokens,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (datetime.now().isoformat(),input_hash,market_state_hash,json.dumps(event_hashes),analysis.symbol,model_name,
             prompt_version,analysis.model_dump_json(),latency_ms,input_tokens,output_tokens,total_tokens,status))


class RiskDecisionRepository:
    def __init__(self,database: Database): self.database=database

    def add(self,decision: RiskDecision) -> None:
        self.database.execute("INSERT INTO risk_decision_records(timestamp,signal_id,outcome,reason_code,reason,requested_quantity,approved_quantity,payload) VALUES(?,?,?,?,?,?,?,?)",
            (decision.timestamp.isoformat(),str(decision.signal_id),decision.outcome.value,decision.reason_code.value,
             decision.reason,decision.requested_quantity,decision.approved_quantity,json.dumps(decision.metadata)))


class SystemStateRepository:
    def __init__(self,database: Database): self.database=database
    def get(self,key: str,default: str="") -> str:
        rows=self.database.query("SELECT value FROM metadata WHERE key=?",(key,))
        return rows[0]["value"] if rows else default
    def set(self,key: str,value: str) -> None:
        self.database.execute("INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,value))

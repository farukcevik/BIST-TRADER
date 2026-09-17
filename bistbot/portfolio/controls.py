from __future__ import annotations

import json
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation
from pydantic import BaseModel

from bistbot.app.config import RiskSettings
from bistbot.storage.database import Database

MAX_OPEN_POSITIONS_KEY="paper.max_open_positions"
CASH_VERSION_KEY="paper.cash.version"


class PaperPortfolioControlState(BaseModel):
    cash: Decimal
    cash_version: int
    configured_max_open_positions: int
    max_open_positions_override: int|None
    effective_max_open_positions: int
    max_open_positions_version: int


class PaperPortfolioControlService:
    """Transactional PAPER-only control plane; never places or liquidates orders."""
    def __init__(self,database:Database,*,mode:str,configured_max_open_positions:int):
        if mode.strip().upper()!="PAPER": raise PermissionError("PAPER_CONTROLS_MODE_REQUIRED")
        if not 1<=configured_max_open_positions<=20: raise ValueError("configured max_open_positions out of range")
        self.database=database; self.configured_max_open_positions=configured_max_open_positions

    def state(self)->PaperPortfolioControlState:
        rows={row["key"]:row for row in self.database.query(
            "SELECT key,value,version FROM runtime_settings WHERE key IN (?,?)",
            (MAX_OPEN_POSITIONS_KEY,CASH_VERSION_KEY))}
        cash_rows=self.database.query("SELECT value FROM metadata WHERE key='paper_cash'")
        if not cash_rows: raise RuntimeError("PAPER_CASH_UNINITIALIZED")
        override_row=rows.get(MAX_OPEN_POSITIONS_KEY); cash_version=rows.get(CASH_VERSION_KEY)
        override=self._validated_override(override_row["value"] if override_row else None)
        return PaperPortfolioControlState(cash=Decimal(cash_rows[0]["value"]),
            cash_version=int(cash_version["value"]) if cash_version else 0,
            configured_max_open_positions=self.configured_max_open_positions,
            max_open_positions_override=override,
            effective_max_open_positions=override or self.configured_max_open_positions,
            max_open_positions_version=int(override_row["version"]) if override_row else 0)

    def set_cash(self,amount,*,expected_version:int,idempotency_token:str,actor:str="dashboard"):
        self._validate_request(expected_version,idempotency_token)
        try: cash=Decimal(str(amount))
        except (InvalidOperation,TypeError,ValueError) as error: raise ValueError("invalid cash amount") from error
        if not cash.is_finite() or cash<0: raise ValueError("cash must be finite and non-negative")
        payload=json.dumps({"amount":str(cash)},sort_keys=True)
        def mutate(connection,now):
            row=connection.execute("SELECT value FROM runtime_settings WHERE key=?",(CASH_VERSION_KEY,)).fetchone()
            version=int(row["value"]) if row else 0
            if version!=expected_version: raise RuntimeError("STALE_CONTROL_VERSION")
            next_version=version+1
            updated=connection.execute("UPDATE metadata SET value=? WHERE key='paper_cash'",(str(cash),))
            if updated.rowcount!=1: raise RuntimeError("PAPER_CASH_UNINITIALIZED")
            connection.execute("INSERT INTO runtime_settings(key,value,updated_at,version) VALUES(?,?,?,1) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at,version=runtime_settings.version+1",
                (CASH_VERSION_KEY,str(next_version),now))
            return {"old_version":version,"new_version":next_version,"cash":str(cash)}
        self._mutate("SET_CASH",payload,idempotency_token,actor,mutate)
        return self.state()

    def set_max_open_positions(self,value:int,*,expected_version:int,idempotency_token:str,actor:str="dashboard"):
        self._validate_request(expected_version,idempotency_token)
        if isinstance(value,bool) or not isinstance(value,int) or not 1<=value<=20:
            raise ValueError("max_open_positions must be an integer from 1 to 20")
        payload=json.dumps({"value":value},sort_keys=True)
        def mutate(connection,now):
            row=connection.execute("SELECT version FROM runtime_settings WHERE key=?",(MAX_OPEN_POSITIONS_KEY,)).fetchone()
            version=int(row["version"]) if row else 0
            if version!=expected_version: raise RuntimeError("STALE_CONTROL_VERSION")
            connection.execute("INSERT INTO runtime_settings(key,value,updated_at,version) VALUES(?,?,?,1) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at,version=runtime_settings.version+1",
                (MAX_OPEN_POSITIONS_KEY,str(value),now))
            return {"old_version":version,"new_version":version+1,"value":value}
        self._mutate("SET_MAX_OPEN_POSITIONS",payload,idempotency_token,actor,mutate)
        return self.state()

    def reset_max_open_positions(self,*,expected_version:int,idempotency_token:str,actor:str="dashboard"):
        self._validate_request(expected_version,idempotency_token)
        payload="{}"
        def mutate(connection,now):
            row=connection.execute("SELECT version FROM runtime_settings WHERE key=?",(MAX_OPEN_POSITIONS_KEY,)).fetchone()
            version=int(row["version"]) if row else 0
            if version!=expected_version: raise RuntimeError("STALE_CONTROL_VERSION")
            connection.execute("INSERT INTO runtime_settings(key,value,updated_at,version) VALUES(?,'',?,1) ON CONFLICT(key) DO UPDATE SET value='',updated_at=excluded.updated_at,version=runtime_settings.version+1",
                (MAX_OPEN_POSITIONS_KEY,now))
            return {"old_version":version,"new_version":version+1,"configured":self.configured_max_open_positions}
        self._mutate("RESET_MAX_OPEN_POSITIONS",payload,idempotency_token,actor,mutate)
        return self.state()

    def effective_risk_settings(self,base:RiskSettings)->RiskSettings:
        row=self.database.query("SELECT value FROM runtime_settings WHERE key=?",(MAX_OPEN_POSITIONS_KEY,))
        override=self._validated_override(row[0]["value"] if row else None)
        return base if override is None else base.model_copy(update={"max_open_positions":override})

    def _mutate(self,operation,payload,token,actor,callback):
        connection=self.database.connection; now=datetime.now(timezone.utc).isoformat()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing=connection.execute("SELECT operation,payload FROM control_idempotency WHERE token=?",(token,)).fetchone()
            if existing:
                if existing["operation"]!=operation or existing["payload"]!=payload:
                    raise ValueError("IDEMPOTENCY_TOKEN_REUSE")
                connection.rollback(); return
            details=callback(connection,now)
            connection.execute("INSERT INTO control_idempotency VALUES(?,?,?,?)",(token,operation,payload,now))
            connection.execute("INSERT INTO system_events(timestamp,level,event_type,message,payload) VALUES(?,?,?,?,?)",
                (now,"INFO","PAPER_PORTFOLIO_CONTROL",operation,json.dumps({"actor":actor,"token":token,**details},sort_keys=True)))
            connection.commit()
        except Exception:
            connection.rollback(); raise

    @staticmethod
    def _validate_request(expected_version,idempotency_token):
        if isinstance(expected_version,bool) or not isinstance(expected_version,int) or expected_version<0:
            raise ValueError("expected_version must be a non-negative integer")
        if not isinstance(idempotency_token,str) or not idempotency_token.strip() or len(idempotency_token)>200:
            raise ValueError("idempotency_token is required")

    @staticmethod
    def _validated_override(raw):
        if raw in (None,""): return None
        try:value=int(raw)
        except (TypeError,ValueError) as error: raise RuntimeError("INVALID_RUNTIME_SETTING") from error
        if not 1<=value<=20: raise RuntimeError("INVALID_RUNTIME_SETTING")
        return value

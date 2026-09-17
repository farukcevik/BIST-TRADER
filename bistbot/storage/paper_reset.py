from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from bistbot.storage.database import Database


DATABASE_ENVIRONMENT_KEY = "database.environment"
CASH_VERSION_KEY = "paper.cash.version"


# Runtime and derived state only. Schema, metadata outside the PAPER accounting
# keys, the configured symbol universe, and runtime configuration are preserved.
PAPER_RESET_TABLES = (
    "control_idempotency",
    "market_snapshots",
    "technical_signals",
    "fundamental_snapshots",
    "fundamental_scores",
    "kap_financial_cache",
    "kap_fundamental_refresh",
    "kap_processed_disclosures",
    "kap_raw_filing_periods",
    "kap_member_cache",
    "technical_levels",
    "potential_assessments",
    "news_items",
    "kap_items",
    "event_items",
    "intelligence_rankings",
    "llm_analyses",
    "llm_analysis_cache",
    "trade_signals",
    "risk_decisions",
    "risk_decision_records",
    "orders",
    "trades",
    "positions",
    "paper_orders",
    "paper_fills",
    "paper_positions",
    "paper_portfolio_snapshots",
    "portfolio_snapshots",
    "system_events",
    "macro_events",
    "market_regime_states",
    "macro_llm_cache",
)

# Legacy PAPER portfolio data may exist in databases created by older
# migrations even though these tables are not part of the current schema.
PAPER_OPTIONAL_RESET_TABLES = (
    "imported_positions",
    "portfolio_reviews",
)

# Only durable execution/accounting state can make an unmarked database unsafe
# to adopt as PAPER. Analysis, signals, intelligence and risk audit records may
# legitimately be written before the broker is constructed in a cycle.
EXECUTION_IDENTITY_TABLES = (
    "orders","trades","positions",
    "paper_orders","paper_fills","paper_positions","paper_portfolio_snapshots",
    "portfolio_snapshots",
)


def assert_paper_database_adoptable(connection) -> None:
    """Refuse unknown populated databases before any PAPER identity is written."""
    identity = connection.execute(
        "SELECT value FROM metadata WHERE key=?", (DATABASE_ENVIRONMENT_KEY,)
    ).fetchone()
    if identity is not None:
        if str(identity["value"]).strip().upper() != "PAPER":
            raise PermissionError(f"PAPER operation refused: database is marked {identity['value']}")
        return
    accounting_keys = {row["key"] for row in connection.execute(
        "SELECT key FROM metadata WHERE key IN "
        "('paper_cash','paper_initial_capital','paper_realized_pnl')"
    )}
    legacy_paper = {"paper_cash", "paper_initial_capital"} <= accounting_keys
    contains_runtime_state = any(
        connection.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone() is not None
        for table in EXECUTION_IDENTITY_TABLES
    )
    if contains_runtime_state and not legacy_paper:
        raise PermissionError(
            "PAPER operation refused: unmarked database contains runtime state "
            "without legacy PAPER accounting identity"
        )


def reset_paper_state(database: Database, *, mode: str, capital: object) -> Decimal:
    """Atomically clear PAPER runtime state and restore its configured capital."""
    if not isinstance(mode, str) or mode.strip().upper() != "PAPER":
        raise PermissionError("PAPER reset refused: trading mode must be PAPER")
    try:
        normalized_capital = Decimal(str(capital)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("PAPER reset capital must be a finite positive number") from error
    if not normalized_capital.is_finite() or normalized_capital <= 0:
        raise ValueError("PAPER reset capital must be a finite positive number")

    connection = database.connection
    try:
        connection.execute("BEGIN IMMEDIATE")
        assert_paper_database_adoptable(connection)
        cash_version = connection.execute(
            "SELECT value FROM runtime_settings WHERE key=?", (CASH_VERSION_KEY,)
        ).fetchone()
        try:
            next_cash_version = int(cash_version["value"]) + 1 if cash_version else 1
        except (TypeError, ValueError) as error:
            raise RuntimeError("invalid PAPER cash version in database") from error
        if next_cash_version < 1:
            raise RuntimeError("invalid PAPER cash version in database")
        for table in PAPER_RESET_TABLES:
            connection.execute(f'DELETE FROM "{table}"')
        existing_optional_tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?)",
                PAPER_OPTIONAL_RESET_TABLES,
            )
        }
        for table in PAPER_OPTIONAL_RESET_TABLES:
            if table in existing_optional_tables:
                connection.execute(f'DELETE FROM "{table}"')
        for key, value in (
            ("paper_cash", str(normalized_capital)),
            ("paper_initial_capital", str(normalized_capital)),
            ("paper_realized_pnl", "0.00"),
        ):
            connection.execute(
                "INSERT INTO metadata(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        connection.execute(
            "INSERT INTO metadata(key,value) VALUES(?, 'PAPER') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (DATABASE_ENVIRONMENT_KEY,),
        )
        connection.execute(
            "INSERT INTO runtime_settings(key,value,updated_at,version) VALUES(?,?,?,1) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at,"
            "version=runtime_settings.version+1",
            (CASH_VERSION_KEY, str(next_cash_version), datetime.now(timezone.utc).isoformat()),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return normalized_capital

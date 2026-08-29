# BISTBOT V1

Private, single-user, paper-only BIST trading application for Python 3.12+ and SQLite. The runtime uses real market/intelligence providers when configured, evidence-bounded LLM analysis, deterministic risk, and SQLite-backed paper execution. It intentionally contains no real broker connectivity.

## Run

```bash
source .venv/bin/activate
pip install -r requirements.txt

# Long-running PAPER bot
python main.py --capital 200000

# One PAPER cycle / portfolio status
python main.py --capital 200000 --once



# Local read-only dashboard
streamlit run dashboard.py

# Tests
pytest -q
```

With no mode flag the process runs continuously at `schedule_seconds`. `--once` executes one complete cycle and exits. External providers fail safely without turning missing evidence into negative sentiment. `PaperBroker` is the only usable broker. Instantiating `RealBroker` always raises `NotImplementedError`.

## Local dashboard

The Streamlit dashboard reads the existing `bistbot.db` in SQLite read-only/query-only mode and refreshes about every 30 seconds. Refreshing the page never starts a bot cycle, calls OpenAI, or places an order. It contains no BUY/SELL controls and always displays `MODE: PAPER`.

Portfolio, positions, persisted performance snapshots, PAPER fills, recent cycle decisions, provider status, and configured risk limits are shown when those records exist. Per-symbol price history is not fabricated when it has not been persisted.

## Boundaries

- `app`: shared Pydantic contracts, validated config, process scheduler.
- `market` and `intelligence`: provider protocols and policy-free utilities.
- `strategy`: extension protocol plus configurable weighted-score utility; no policy.
- `risk`: deterministic approval and sizing protocols. Risk has final authority.
- `portfolio`, `broker`, `storage`: accounting, paper execution, SQLite persistence.
- `notifications`, `reporting`: output adapter contracts.

Dependencies point inward toward `app.models`. Providers never execute orders. Strategy only proposes `TradeSignal`; risk returns `RiskDecision`; only an approved decision may be passed to `Broker` by a future application orchestrator.
# Market regime overlay

BISTBOT now keeps broad macro risk separate from company intelligence. `MacroNewsProvider`
implementations feed official or licensed sources into a deterministic materiality gate. Only
HIGH-materiality events are eligible for an optional LLM interpretation; unchanged events are
cached by `canonical_event_id`.

The resulting `RISK_ON`, `NORMAL`, `CAUTION`, `RISK_OFF`, or `CRISIS` state adjusts entry scores,
buy thresholds and paper position sizing. Sector and market adjustments are reported separately.
`CRISIS` blocks new entries, while the deterministic Risk Engine remains the final authority.

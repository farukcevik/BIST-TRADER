# BISTBOT V1 architecture skeleton

Private, single-user, paper-only BIST trading application for Python 3.12+ and SQLite. It composes a deterministic demo market feed, scanner, resilient news/KAP ranking, evidence-bounded LLM analysis, configured final scoring, deterministic risk, and SQLite-backed paper execution. It intentionally contains no real broker connectivity.

## Run

```bash
python3 -m pip install -r requirements.txt
python3 main.py --capital 200000
python3 main.py --capital 200000 --once
python3 main.py --status
python3 -m pytest -q
```

With no mode flag the process runs continuously at `schedule_seconds`. `--once` executes one complete cycle and exits. The default external news/KAP and LLM adapters fail safely to evidence-free HOLD, while deterministic demo market data keeps the pipeline testable without credentials. `PaperBroker` is the only usable broker. Instantiating `RealBroker` always raises `NotImplementedError`.

## Boundaries

- `app`: shared Pydantic contracts, validated config, process scheduler.
- `market` and `intelligence`: provider protocols and policy-free utilities.
- `strategy`: extension protocol plus configurable weighted-score utility; no policy.
- `risk`: deterministic approval and sizing protocols. Risk has final authority.
- `portfolio`, `broker`, `storage`: accounting, paper execution, SQLite persistence.
- `notifications`, `reporting`: output adapter contracts.

Dependencies point inward toward `app.models`. Providers never execute orders. Strategy only proposes `TradeSignal`; risk returns `RiskDecision`; only an approved decision may be passed to `Broker` by a future application orchestrator.

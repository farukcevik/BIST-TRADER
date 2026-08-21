# BISTBOT V1 architecture skeleton

Private, single-user, paper-only BIST trading architecture for Python 3.12+ and SQLite. This repository currently defines integration boundaries; it intentionally does not implement a trading strategy or any real broker connectivity.

## Run

```bash
python3 -m pip install -r requirements.txt
python3 main.py --status
python3 main.py --once
python3 -m pytest -q
```

`--once` reports readiness until concrete market, intelligence, strategy, and risk implementations are composed. `PaperBroker` is the only usable broker. Instantiating `RealBroker` always raises `NotImplementedError`.

## Boundaries

- `app`: shared Pydantic contracts, validated config, process scheduler.
- `market` and `intelligence`: provider protocols and policy-free utilities.
- `strategy`: extension protocol plus configurable weighted-score utility; no policy.
- `risk`: deterministic approval and sizing protocols. Risk has final authority.
- `portfolio`, `broker`, `storage`: accounting, paper execution, SQLite persistence.
- `notifications`, `reporting`: output adapter contracts.

Dependencies point inward toward `app.models`. Providers never execute orders. Strategy only proposes `TradeSignal`; risk returns `RiskDecision`; only an approved decision may be passed to `Broker` by a future application orchestrator.


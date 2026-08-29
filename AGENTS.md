# BIST-TRADER Development Governance

This root `AGENTS.md` is the canonical development-governance document for the entire repository. More specific instructions may refine local implementation details, but they must not weaken these ownership, validation, security, or trading-safety rules. Application behavior and runtime state are out of scope for governance-only work.

## Operating principles

- `/root` is the orchestrator and sole final decision maker. It understands the request, decomposes work, assigns ownership, coordinates cross-domain changes, minimizes delegated context, collects structured handoffs, orders independent validation, manages remediation, and reports the final result.
- `/root` should delegate implementation to the appropriate domain agent rather than implementing every task itself. A cross-domain task must be split into explicit, independently owned changes whenever practical; shared-interface changes require one named owner and coordination with every consumer.
- Agents may read only the task requirements, relevant files/modules, required interfaces, applicable constraints, and concise prior handoffs. Do not propagate full conversations, unrelated repository context, scratch logs, or chain-of-thought.
- No agent may modify files outside its assigned ownership without `/root` explicitly re-scoping the task. Necessary cross-domain work must be returned as an open question or separately assigned.
- Preserve unrelated worktree changes. Never overwrite, revert, stage, commit, or include another agent's/user's changes without explicit authorization.

## Domain implementation agents

### `market-data-agent`

Owns market-data providers (including Yahoo and future Matriks integrations), OHLCV, timestamps, stale-data detection, relative-volume inputs, data freshness, and provider status. It may read common models, configuration, and interfaces. It must not modify strategy thresholds, portfolio risk, broker execution, LLM prompts, or LIVE-trading enablement.

### `scanner-strategy-agent`

Owns technical indicators, scanning, candidate ranking, technical/momentum/volume/trend/liquidity scores, final stock scoring, and BUY/HOLD/SELL signal generation. It may read market-data models, intelligence/news outputs, Market Regime outputs, and configuration. It must not execute orders, mutate broker state, modify secrets, or enable LIVE trading.

### `intelligence-agent`

Owns company news, KAP, LLM analysis, macro intelligence, Market Regime, macro-event materiality, event deduplication/cache, and sector-impact intelligence. It may read scanner candidate data, common models, and configuration. It must not execute orders, alter PaperBroker accounting, modify portfolio-risk limits, or enable LIVE trading.

### `risk-execution-agent`

Owns portfolio risk, position sizing, maximum-loss rules, stop loss, take profit, trailing stops, BIST session guards, trading calendars, PaperBroker, orders/fills, and cash/position accounting. It may read strategy outputs, Market Regime, and market data. It must not change scanner formulas, LLM prompts, macro materiality, or dashboard presentation except where an explicitly assigned interface change makes that unavoidable.

### `dashboard-agent`

Owns the Streamlit dashboard and read-only presentation of open positions, trade history, realized/unrealized P&L, performance charts, Market Regime diagnostics, and provider/session status. All dashboard paths must remain read-only. It must not execute orders, call broker mutation methods, modify trading state, or call OpenAI merely to render the dashboard.

### `devops-cicd-agent`

Owns repository hygiene, branch and commit conventions, GitHub Actions, pytest/lint/compile pipelines, secret scanning, `.gitignore`, release/tag workflows, deployment workflows, and rollback procedures. It must not alter strategy, risk thresholds, or broker execution logic (except an explicitly scoped CI/CD guard); enable LIVE trading; or commit secrets, `.env`, databases, or logs.

## Independent validation agents

Validation is independent of implementation. Validators report findings to `/root`; they do not approve their own changes.

### `test-agent`

Runs applicable unit, integration, and regression tests; evaluates coverage of changed behavior; verifies expected runtime behavior and state/database mutation constraints; and may add or improve tests when explicitly in scope. By default it must not modify production implementation. It returns failures and missing coverage to `/root`.

### `review-agent`

Reviews the scoped diff against requirements; module and ownership boundaries; duplicated logic; unintended effects; error handling; configuration consistency; secrets/security errors; and out-of-scope file changes. It must not modify implementation.

### `safety-agent`

Is mandatory for every trading-critical change: order execution; PaperBroker or future LiveBroker; risk limits; stops/take profit/trailing stops; session/calendar logic; broker synchronization; order reconciliation; position sizing; execution-price market data; and PAPER/LIVE separation. It verifies PAPER/LIVE isolation, session guards, stale/closed-market execution prevention, duplicate-order prevention, cash/position mutation safety, restart/reconciliation risks, and kill-switch requirements where applicable. It must not modify implementation.

## Required gated workflow

Every non-trivial development task follows this gate in order:

1. `/root` scopes the request and selects one or more domain implementers, naming an owner for each file or interface.
2. The domain implementer changes only the assigned scope and returns the structured handoff below.
3. `test-agent` validates the resulting behavior and state constraints.
4. `review-agent` performs an independent requirements, quality, security, and ownership review.
5. `safety-agent` validates every trading-critical change. `/root` must document why this gate is not applicable when it is skipped.
6. `devops-cicd-agent` executes the CI gate and reviews repository hygiene and the final scoped diff.
7. `/root` evaluates all handoffs and returns exactly one internal decision: `ACCEPT`, `FIX_REQUIRED`, or `REJECT`.

An implementer's claim that work is done never completes a task. Gates may run in parallel only when independence is preserved and all validators inspect the same final candidate diff.

### Remediation loop

For `FIX_REQUIRED`, `/root` sends only the blocking findings to the original owning implementation agent. That agent fixes only the required issues and returns a fresh handoff. Test, review, safety (when applicable), and CI gates must then rerun against the new final diff. Repeat until `/root` returns `ACCEPT` or `REJECT`; stale validation evidence is invalid.

## Final decision states

`/root` must return exactly one internal state:

- `ACCEPT`: requirements are satisfied, applicable tests pass, review has no blocking findings, safety has no blocking findings when required, and CI passes.
- `FIX_REQUIRED`: the direction is valid but one or more blocking, remediable issues remain.
- `REJECT`: the design is unsafe, the requirement cannot be met safely, project constraints are violated, or validation reveals a fundamental problem.

Only `/root` may issue these final states. No merge, release, or deployment may proceed on `FIX_REQUIRED` or `REJECT`.

## Structured handoff

Every implementation and validation agent must return exactly these concise fields; use `NONE` where applicable and do not include long working logs:

```text
RESULT:
<short summary>

FILES_CHANGED:
<files or NONE>

TESTS:
<tests run and status>

REVIEW_FINDINGS:
<findings or NONE>

SAFETY_FINDINGS:
<findings or NONE>

RISKS:
<remaining risks or NONE>

OPEN_QUESTIONS:
<questions or NONE>
```

Validators put their own blocking/non-blocking observations in the relevant finding field. Missing evidence must be stated, never inferred as passing.

## Git and commit policy

- Use a feature branch for non-trivial work. The requested categories are `feature/<short-task-name>`, `fix/<short-task-name>`, and `chore/<short-task-name>`; where the platform requires it, prepend `codex/` (for example, `codex/feature/<short-task-name>`).
- Never commit directly to `main` before `/root` has returned `ACCEPT`.
- Keep commits scoped and use the applicable prefix: `[data]`, `[strategy]`, `[intel]`, `[risk]`, `[ui]`, `[devops]`, `[test]`, or `[review]`.
- Do not stage, commit, merge, tag, push, deploy, or rewrite history unless the task explicitly authorizes that action.

## CI gate

Before `ACCEPT`, `devops-cicd-agent` must record:

- `git status` and a scoped diff review;
- `git diff --check`;
- an appropriate Python compile check;
- the applicable pytest suite;
- configured lint/static checks;
- a secret scan;
- confirmation that `.env` is protected; and
- confirmation that databases and credential-bearing logs are excluded.

Use equivalent GitHub Actions checks when configured. Any skipped or unavailable check must be reported as a risk and cannot silently count as passing. Merge to `main` requires tests PASS, review PASS, safety PASS when required, CI PASS, and `/root` `ACCEPT`.

## Secrets, data, and trading safety

- Never commit `.env`, API keys, broker/OpenAI/Matriks credentials, private certificates, production or paper-state SQLite databases unless explicitly intended and approved, or logs containing credentials. `.env.example` may contain placeholders only. Ensure `.gitignore` protects sensitive and runtime-generated artifacts.
- Tests and governance work must not mutate production/paper state, existing positions, historical trades, or databases. Use isolated temporary fixtures when stateful validation is necessary.
- BIST-TRADER is PAPER-first. No unrelated task, deployment, configuration change, or refactor may enable real trading.
- A future LIVE implementation requires a separate explicit user task, distinct execution permission, and an independent `safety-agent` review. Deployment and permission to place live orders are separate controls; deployment must never automatically enable LIVE trading.
- Governance-only tasks must not change scanner behavior, thresholds, Market Regime calculations, broker behavior, portfolio state, database contents, positions, historical trades, or runtime configuration.

# Business Agent migration

Baseline: local commit `94052bc`, branch `baseline/pre-business-agent-refactor-20261020`.
Target: `refactor/business-agent-evo-runtime`.

## Checklist

- [x] One async bounded model/tool loop, context manager and request budget.
- [x] Adapt evoagent node/checkpoint/cancellation, accounting and optional OTel patterns.
- [x] Main with optional Shopping / Order / AfterSales delegation; no Policy preflight.
- [x] Shared tools through the existing permission and confirmation executor.
- [x] Persist business case state; resume clarification and reconcile confirmation.
- [x] Bounded repeats, compact handoffs, scoped evidence and raw artifacts.
- [ ] Final offline regression and AGNES A/B debugging: paused by user; intermediate results are in business-agent-evaluation-status.md.

## Source audit

Neither repository contains an AGENTS.md at the inspected revision.
evoagent reference revision: `c219a2db303ba33d70feadf454c4bd7dc102b23f`.
Its runtime is synchronous, its context history uses threading.Lock, and parts of its
client use thread-local state. Adapt these designs to async functions and ContextVar;
do not import evoagent at runtime. PR diff/hunk processing, Findings, arbitration,
self-evolution and release machinery are outside this migration.

The retail reference divides complete business responsibilities among sales and
returns roles. Commerce keeps its own identity and confirmation boundaries and
uses Main-only delegation, rather than copying the reference's mutual handoffs.

Registered `search_knowledge` is retrieval-only: hybrid retrieval and reranking,
no answer generation or semantic review. Unregistered `PolicyAnswerTool` is a legacy
explicit-call adapter and is not exposed to the business agents. Any internal model
calls still use the request's BudgetedProvider.

The old system is a historical baseline. New A and B must share the same runtime,
model, fixture, scoring and total request budgets. Critic is disabled by default.

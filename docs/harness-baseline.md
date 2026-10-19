# Harness baseline and audit

Baseline: `a1317f20dde5f81e3d2a28bc6f98ddb9b445546d`, preserved as `baseline/pre-harness-a1317f2`. Implementation branch: `feat/lightweight-agent-harness`. Initial checkout clean; no AGENTS.md found in workspace. Python 3.12.14, Node 24.19.0. Fresh baseline run: 162 pytest tests passed. Previous onboarding also verified frontend 3 tests, build, Ruff and mypy; these are environment evidence, not quality metrics.

## Observed capabilities

- `agents/graph.py` builds LangGraph; guardrail → intent → complexity route → retrieval or planner/executor → generation → faithfulness → memory. Intent labels also drive fallback and skills.
- Retrieval: Markdown/optional Unstructured ingestion, BM25+dense/RRF, query rewrite, rerank, bounded SelfRAG. `search_knowledge` currently bypasses full rerank/reflection.
- Tools: Pydantic input schemas, risk checks in executor, repository ownership checks, retries/fallback paths. Registry itself has no enforcement.
- Memory: messages saved, but summary starts empty each request; generation ignores history; compression runs after generation.
- Persistence: SQLite sessions/messages/idempotency, request-local DB context. Session reuse lacks ownership enforcement. Action proposals are process-local with monotonic TTL; restart loses them.
- API: JSON chat + SSE, strategy/citations/pending action contracts. SSE exposes graph nodes then chunks the final answer; this is not native token streaming.
- MCP: local handlers and custom HTTP POST `{name, arguments}`; not standard MCP discovery/JSON-RPC.

## Model and external prerequisites

`AGNES` is present, with saved destination `apihub.agnes-ai.com`. No secret values read into artifacts. User selects `agnes3.0flash`. Root serves a web application; `/openapi.json` returns Cloudflare 403. Need authoritative base URL/protocol before authenticated generation; offline implementation continues independently. No other paid provider will be selected.

## Reference sources

Resolve and read official OpenCode and Hermes sources at pinned commits; record concrete files and relevant mechanisms in the architecture document. No code copied by default.

## Evaluation policy

Keep existing retrieval/generation/intent datasets. Distinguish deterministic mock tests from real calls; no historical README percentages are reused. A/B/C comparisons require identical endpoint/model, corpus and retrieval parameters. Unknown or unavailable real-model measurements stay explicitly unmeasured.

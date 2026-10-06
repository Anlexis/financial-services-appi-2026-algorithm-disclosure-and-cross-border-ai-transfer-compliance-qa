# Design Specification — FIN-C2-145
# APPI 2026 Algorithm Disclosure & Cross-Border AI Transfer Compliance Q&A

## Position in AgentCore Architecture

| Field | Value |
|---|---|
| Template ID | FIN-C2-145 |
| Agent Class | `AppiComplianceQAAgent` |
| Category | Cat 2 — domain-specific pipeline (FIN industry) |
| Pattern | Retrieval Q&A over a curated regulatory knowledge base |
| L1 Base (framework base class) | `AgentBaseGraph` (outer) + `BaseGraph` (inner) — direct framework inheritance |

Three-layer separation:

- State: flat `TypedDict` (`State(AgentState)`) — no Pydantic (msgpack-safe)
- Node: `FunctionNode` inheritance — `execute(self, state: dict) -> dict`
- Graph: `AgentBaseGraph` (outer, 5-node backbone) + `BaseGraph` (inner, custom topology)

## Architecture Overview

### Cat 2 Two-Layer Nested Architecture

```
Outer backbone (AgentBaseGraph — fixed, add_edges() not overridden):
  START → InitializeNode → PreProcessNode(VERIFIED_EXTERNAL) →
          AppiComplianceGraphNode(GraphNode) →
          PostProcessNode(ANONYMOUS, output gate) → FinalizeNode → END

Inside AppiComplianceGraphNode.get_subgraph():
  DomainWorkflowGraph (BaseGraph):
    START → retrieve → rank → generate → END
```

The inner topology is linear — no conditional edges are wired, and the
ABC-required `route()` is annotated with this graph's own `State` schema
(the annotation is the input schema a path callable would receive; the base
`AgentState` annotation would project domain fields away if a conditional
edge were ever added).

### Node Configuration

| Slot | Class | Responsibility | Trust Level |
|------|-------|---------------|-------------|
| initialize | `InitializeNode` (default) | Session init, schema_version | — |
| pre_process | `PreProcessNode` | Caller-contract validation, instruction-override + personal-data screens | VERIFIED_EXTERNAL |
| main | `AppiComplianceGraphNode` | Wraps inner domain workflow | ANONYMOUS |
| post_process | `PostProcessNode` | Release-invariant output gate, disclaimer injection | ANONYMOUS |
| finalize | `FinalizeNode` (default) | Response metadata, timing | — |

### Inner Domain Nodes (all `TrustLevel.ANONYMOUS`)

| Node key | Class | Responsibility |
|----------|-------|---------------|
| `retrieve` | `RetrieveNode` | Parse the request envelope; retrieve APPI 2026 article chunks (topic-restricted when requested) |
| `rank` | `RankNode` | Rerank by relevance score; truncate to the effective top-K |
| `generate` | `GenerateNode` | Compose the compliance answer with article citations, risk level, deadline flag |

### Caller-Data Contract

`/invoke` accepts the question text plus a structured `input_context`
channel. Every field is validated in `PreProcessNode` against explicit
bounds and fails CLOSED; rejected values are never echoed:

| Field | Type | Constraint | Effect |
|-------|------|------------|--------|
| `channel` | `str` | `^[a-z0-9_]{1,32}$` | Caller channel identifier (audit only) |
| `topic_filter` | `str` | one of `cross_border_transfer`, `automated_decisions`, `algorithm_disclosure`, `vendor_contracts` | Restrict retrieval to one topic area |
| `top_k` | `int` | integer, 1–6 (bool and non-finite forms refused) | Bound the number of cited articles |

Unknown fields are refused (counted, never echoed). Every string in the
structure — mapping keys included — passes the instruction-override screen
before any field is read.

The framework does not forward the outer `input_context` into a nested
inner graph, so `AppiComplianceGraphNode.extract_input()` JSON-encodes the
validated question and the accepted per-request fields into an envelope;
`RetrieveNode` deserializes it (a plain string stays a plain query, so the
inner graph remains directly invocable in tests).

### Runtime Configuration

`config/agent.yaml` is the flat static manifest (identity, entry point,
`requires` gates — this template declares `requires.secrets: []` and
`requires.extras: []`). Runtime parameters live in `config/config.yaml`
and are passed to the graph constructor (by the platform registry, and
equally by the standalone server in `src/api/server.py`):

| Key | Default | Reader |
|---|---|---|
| `max_retry` | `3` | Framework backbone retry loop |
| `default_top_k` | `5` | `RankNode` truncation when the caller supplies no `top_k` |
| `enforcement_deadline` | `"2026-07-06"` | `GenerateNode` checklist line + `PostProcessNode` HIGH-risk warning |

`AppiComplianceGraphNode._parent_config()` forwards the declared values to
the inner graph under `config["configurable"]`;
`DomainWorkflowGraph._extra_initial_state()` republishes them into inner
state, which is what makes them reachable by the domain nodes. Precedence
for the truncation count: caller `top_k` > declared `default_top_k` >
literal in-code default.

### Data Flow

```
[Caller] question (NL string) + input_context (validated)
   ↓ user_input / input_context
[PreProcessNode]
   • type / empty / length guards
   • instruction-override screen (raw + whitespace-collapsed)
   • personal-data screen (email / phone / credit card)
   • input_context: unknown-field refusal, depth-first structure screen
     (keys included), per-field bounds
   ↓ validated_input (+ topic_filter, top_k when supplied)
[AppiComplianceGraphNode.extract_input()]
   ↓ JSON envelope → inner_graph.invoke()
[RetrieveNode]
   • Parse envelope; restrict corpus by topic_filter when present
   • Deterministic keyword-scored retrieval over the APPI 2026 KB stub
   ↓ retrieved_chunks: List[{article_id, topic, text, relevance_score}]
[RankNode]
   • Sort by relevance_score DESC, truncate to effective top-K
   ↓ ranked_chunks
[GenerateNode]
   • Compose answer with article citations + action checklist
   • Classify risk_level (HIGH/MEDIUM/LOW) from chunk topics
   • Flag enforcement_deadline_warning when HIGH
   ↓ compliance_answer, cited_articles, risk_level, enforcement_deadline_warning
[AppiComplianceGraphNode.merge_output()]
   ↓ result, cited_articles, risk_level, enforcement_deadline_warning, enforcement_deadline
[PostProcessNode]
   • Append deadline warning (HIGH risk) + mandatory legal disclaimer
   • Enforce the release invariant on the FINAL composed text
   ↓ formatted_output
[FinalizeNode]
   ↓ output (caller receives)
```

### State Definition

| Field | Type | Layer | Purpose |
|-------|------|-------|---------|
| validated_input | Optional[str] | Both | Validated compliance question (clean query; inner copy published by RetrieveNode after envelope parsing) |
| topic_filter | Optional[str] | Outer | Validated caller topic restriction |
| top_k | Optional[int] | Both | Validated caller article count |
| default_top_k | Optional[int] | Inner | Declared default from config/config.yaml |
| enforcement_deadline | Optional[str] | Both | Declared ISO enforcement date |
| retrieved_chunks | Optional[List[Dict]] | Inner | KB chunks from RetrieveNode |
| ranked_chunks | Optional[List[Dict]] | Inner | Reranked top-K chunks |
| compliance_answer | Optional[str] | Inner | Generated answer (pre-gate) |
| cited_articles | Optional[List[str]] | Both | Cited APPI article IDs |
| risk_level | Optional[str] | Both | HIGH / MEDIUM / LOW |
| enforcement_deadline_warning | Optional[bool] | Both | HIGH-risk warning flag |
| result | Optional[str] | Outer | Answer under gate; released form after PostProcessNode |

State constraints:

- Flat TypedDict — no Pydantic, no dataclass, no nested objects
- No JWT, API keys, or personal data in State (checkpoint DB leakage risk)
- InvocationContext via `config["configurable"]` only (never in State)

## Security Design

### Trust gate

- `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL` —
  the only trust barrier for external callers.
- All inner domain nodes and the post_process slot run `ANONYMOUS` behind it.
- The standalone server (`src/api/server.py`) is the entry-point auth
  boundary: with `INVOKE_AUTH_TOKEN` set, unauthenticated callers receive a
  generic 401; authenticated callers run at VERIFIED_EXTERNAL. Trust
  established by upstream middleware is never demoted.

### Input boundary (PreProcessNode + src/nodes/content_screens.py)

The screens are the template's own guarantee, enforced in the node that
owns the caller contract regardless of any platform-side scan, and proven
by calling `execute()` directly in tests:

1. Type / empty / length guards (2,000-character cap)
2. Instruction-override screen — anchored on full phrases aimed at the
   model (override verb + instruction noun, model-role reassignment,
   prompt exfiltration, chat-template control tokens `<|…|>` / `[INST]` /
   `<<SYS>>`, Japanese directive forms). Screened raw AND after whitespace
   collapse. Compliance prose full of directive verbs ("delete personal
   data from our training set", "act as a data processor") is NOT refused —
   probed both directions in tests.
3. Personal-data screen (domain rule): email, phone (JP/US; day-month-year
   dates excluded), credit card — questions describe situations, they never
   carry the data itself.
4. Structured channel: unknown fields refused by count; depth-first
   injection screen over every string including mapping keys (JSON escape
   forms are decoded before the scan); per-field bounds as in the
   caller-data contract table; the numeric field goes through a fail-closed
   bounded-integer parser (bool, NaN/Infinity and out-of-range forms all
   refused).
5. Refusals name the screen or field, never the value; audit events carry
   reason codes and counts only.

### Output boundary (PostProcessNode)

This template renders NO monetary aggregates of its own: the answer quotes
the curated regulatory knowledge base — including statutory figures —
verbatim, so a numeric rounding grid is not applicable and would corrupt
legally accurate values. The release invariant this template owns instead:

1. Released answers are never empty.
2. Every released answer carries the mandated legal disclaimer; HIGH-risk
   answers carry the enforcement-deadline warning.
3. No credential material leaves the agent (patterns shared with the input
   boundary via `content_screens.py`).

The gate runs on the FINAL composed text. On a violation the node returns
an error AND clears every output-bearing field (`result`,
`formatted_output`, `cited_articles`) — the response envelope falls back to
`state["result"]` even on an error status, so clearing is what actually
contains the blocked text.

### Audit trail

`emit_trace_event(event, payload, state)` (positional form) on every
execute() path of every node — reason codes, counts and identifiers only,
never caller text or answer bodies.

### Credential safety

- No credentials in State; secrets via `InvocationContext` / config
- No hardcoded API keys or tokens; `requires.secrets: []` — this template
  constructs no external clients

## Import Isolation Confirmation

- [x] No platform-internal SDK imports anywhere under `src/`
- [x] Import targets: `framework.*` and `shared.*` only

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| Base type | AgentBaseGraph | AutonomousBaseGraph | AgentBaseGraph | Fixed pipeline: retrieve → rank → generate (no autonomous loop) |
| Cat pattern | Cat 1 flat | Cat 2 nested | Cat 2 nested | FIN-industry-specific multi-step domain workflow |
| Inner graph base | BaseGraph | AgentBaseGraph | BaseGraph | Fully custom topology; no 5-node backbone needed inside |
| Input gate mechanism | framework hook | execute() + shared screen module | execute() + shared screen module | Template-owned guarantee, active regardless of platform-side scan configuration |
| Numeric output policy | rounding grid | verbatim statutory figures | verbatim | The KB quotes statutory values; rounding would corrupt legally accurate figures |
| PostProcessNode trust | ANONYMOUS | VERIFIED_EXTERNAL | ANONYMOUS | Backbone post_process slot; caller trust already validated at pre_process |
| Caller fields | free text | inert identifiers + bounded int | inert identifiers + bounded int | Caller strings that reach retrieval or logs stay inert; the one numeric fails closed |

## APPI 2026 Compliance Scope

This template covers APPI 2026 AI-specific obligations effective July 6, 2026:

1. **Algorithm Disclosure** (第三者提供, 算出ロジック開示): AI-driven credit
   scoring / investment recommendation systems must disclose algorithm
   logic to regulators and, in some cases, data subjects.
2. **Cross-Border AI Transfer** (第24条): Personal data used for AI
   training / inference that crosses national borders requires explicit
   consent or an adequate-protection mechanism.
3. **Third-Party AI Vendor Contracts** (第18条): Financial institutions
   using external AI vendors must have APPI-compliant data processing
   agreements covering audit rights and deletion obligations.
4. **Automated Decision-Making** (第17条改正): High-risk AI decisions
   (credit denial, investment recommendation) require human-review
   escalation paths under the new rules.

**KB structure** (in-process deterministic stub in this implementation;
production: vector store):

- `appi_2026_algorithm_disclosure`: Algorithm disclosure requirements by use-case type
- `appi_2026_cross_border_transfer`: Cross-border transfer rules, exceptions, adequacy list
- `appi_2026_vendor_contracts`: Vendor contract requirements and template clauses
- `appi_2026_automated_decisions`: High-risk AI decision-making rules and escalation paths

Risk classification is code-owned (`GenerateNode`): the two topic sets that
map to HIGH (cross-border transfer, automated decisions) and MEDIUM
(algorithm disclosure, vendor contracts) are fixed alongside the KB topic
taxonomy they classify — they are not runtime-configurable.

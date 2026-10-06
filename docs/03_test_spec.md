# Test Specification — FIN-C2-145
# APPI 2026 Algorithm Disclosure & Cross-Border AI Transfer Compliance Q&A

## Overview

All tests run against the deterministic in-process knowledge base — no LLM
or vector-store infrastructure is required. The suite has four layers:

| Layer | File(s) | Focus |
|-------|---------|-------|
| Screen unit tests | `tests/unit/test_content_screens.py` | Every screen probed in BOTH directions |
| Node unit tests | `tests/unit/test_pre_process_node.py`, `tests/unit/test_domain_nodes.py`, `tests/unit/test_graph.py`, `tests/unit/test_framework_compliance_tc06_tc07.py` | Node contracts via direct `execute()` |
| Integration | `tests/integration/test_e2e_invoke.py` | End-to-end through the real ASGI `/invoke` with Bearer auth |
| Proof of boundary | `tests/proof_of_boundary/` | Import isolation, backbone invoke order, state safety, interrupt propagation |

Security-relevant assertions are behavioural (error status, nothing
released, nothing echoed) — never a gate's wording.

---

## Content Screens (`tests/unit/test_content_screens.py`)

| Group | Cases |
|-------|-------|
| Injection refused | Chat-template control tokens (`<\|im_start\|>`, `<\|system\|>`, `[INST]`, `<<SYS>>`), EN override directives (ignore/disregard/forget + instruction nouns), role reassignment aimed at the model, prompt exfiltration, `new system prompt:` header, JA directive forms (指示/命令/ルール無視, システムプロンプト表示), script tags, whitespace-spread directives (collapse pass) |
| Domain prose passes | Deletion verbs ("delete personal data from our training set"), legal roles ("act as a data processor", "joint data controller"), ignore-an-obligation phrasing (EN + JA), "system:" in ordinary prose, the operation guide's real example queries, instruction nouns without an override verb |
| Personal data | email / JP+US phone / credit card refused with category name; day-month-year dates, ISO dates and article numbers pass |
| Credentials | assignment forms, Bearer tokens, JWTs, prefixed API keys, AWS key IDs refused; clean regulatory prose passes |
| Structure scan | hostile nested values caught, hostile mapping KEYS caught, clean contract passes, >8-deep nesting refused |
| Bounded-int parser | 1/3/6 accepted; 0, 7, negatives, bool, float (incl. 3.0), strings, None, NaN/±Infinity (string and raw float), containers refused |
| Inert bounds | channel pattern (case, length, punctuation), topic allowlist ↔ internal topic mapping |

## PreProcessNode (`tests/unit/test_pre_process_node.py`)

Direct `execute()` — no framework wrapper in front, so the refusals proven
here are the template's own.

| Group | Cases |
|-------|-------|
| Success | valid question; status as plain string; whitespace stripped; accepted context fields forwarded; omitted fields left unset (declared defaults apply); audit event; VERIFIED_EXTERNAL trust |
| Question guards | non-string (int/float/None/list/dict/bool), empty/blank, >2000 chars, exactly-at-limit passes |
| Injection screen | attacks refused and never echoed; legitimate directive-verb question passes; rejection audited |
| Personal data | category named, value never echoed (error log AND audit payload) |
| Context contract | non-mapping refused; unknown field refused by count (name not echoed); hostile value refused; hostile KEY refused; `\u`-escaped directive refused post-parse; invalid channel forms; invalid topic_filter forms |
| top_k fail-closed | parametrized matrix per the bounded-int parser (0, 7, -3, bools, floats, strings, NaN/±Infinity string and raw, None, list); in-range 1/4/6 accepted |
| Audit coverage | every rejection path emits exactly one `pre_process_rejected` |

## Domain Nodes (`tests/unit/test_domain_nodes.py`)

| Node | Cases |
|------|-------|
| RetrieveNode | valid query success; cross-border → 第24条; positive scoring; empty query error; JSON envelope parsed (query/top_k/topic_filter, clean query republished); topic filter restricts corpus; plain string still works; envelope with empty query errors; chunks JSON-serializable; audit; ANONYMOUS trust |
| RankNode | success; descending order; literal default truncation (5); zero-relevance chunks excluded; caller top_k > declared default_top_k > literal precedence; malformed state count falls back; empty input → empty ranked + success; audit; ANONYMOUS trust |
| GenerateNode | answer from chunks; topic → risk matrix (2×HIGH, 2×MEDIUM); no topics → LOW; empty chunks → fallback answer; citations deduplicated; zero-score chunk not cited; declared deadline rendered; absent/malformed deadline falls back to the literal; audit; ANONYMOUS trust |
| PostProcessNode | success + disclaimer (JA + EN); HIGH → deadline warning (default and declared date); MEDIUM → no warning; credential forms blocked AND every output-bearing field cleared (`result`, `formatted_output`, `cited_articles`) with nothing of the blocked text in the delta; blocked release audited; empty result error; gate function raises on missing disclaimer / passes composed text; ANONYMOUS trust |

## Graph (`tests/unit/test_graph.py`)

| Group | Cases |
|-------|-------|
| Outer agent | instantiation; `Graph` alias; name; state schema; accepts runtime config |
| GraphNode | extract_input builds the JSON envelope; omits absent fields; falls back to user_input; empty state; merge_output maps all coupled fields; defaults on missing fields; get_subgraph returns the inner graph; `_parent_config` forwards declared values / degrades to `{}` |
| Inner graph | instantiation; name; state schema; `_extra_initial_state` seeds declared values / empty without config / skips malformed; route annotated with the graph's own State; get_output shape; get_output ↔ merge_output coupling end-to-end |

## Framework Compliance (`tests/unit/test_framework_compliance_tc06_tc07.py`)

The framework's final input/output gate methods cannot be overridden by
subclasses. Required for every template with FunctionNode subclasses.

## Integration (`tests/integration/test_e2e_invoke.py`)

Real ASGI `/invoke`, Bearer auth, real compiled agent:

| Group | Cases |
|-------|-------|
| Happy path | HIGH question → real >100-char answer, 第24条 cited, disclaimer + deadline warning, credential-free; MEDIUM question → no warning; LOW question → fallback answer, still disclaimed; topic_filter restricts cited articles; top_k bounds article sections |
| Rejection | invalid topic_filter → error, nothing released, value not echoed; control-token injection → error, nothing released; personal data never appears in the response envelope (the installed framework masks it before the node gate; the template's own refusal is proven at unit level); raw `NaN` / `-Infinity` JSON literals refused; float top_k refused; >256KB input_context → 413 |
| Auth boundary | missing token → 401 generic body; wrong token → 401 generic body |
| Declared config arrival | config file values live on the compiled agent; a distinct declared `enforcement_deadline` renders in the released answer; declared `default_top_k` bounds the answer; per-request `top_k` wins over the declared default |
| Containment | a blocked release ships no answer fragment, no traceback, no source paths |

## Proof of Boundary (`tests/proof_of_boundary/`)

| File | Purpose |
|------|---------|
| `test_import_isolation.py` | No platform-internal SDK imports under `src/` |
| `test_pb_invoke_order.py` | Full `invoke()` with VERIFIED_EXTERNAL: success + backbone node order |
| `test_state_safety.py` | State schema safety rules |
| `test_pb7_hitl_interrupt_propagation.py` | Interrupt-propagation skip stub (no `propagate_hitl=True` in this template) |

---

## Test Infrastructure Notes

- Unit tests monkeypatch `emit_trace_event` in each node module's namespace
  when counting audit events; the integration suite neutralises the audit
  sink for all node modules.
- The integration suite drives the app over a hand-rolled ASGI call — no
  HTTP test-client dependency.
- Declared-config arrival tests substitute the runtime-config path with a
  temporary file, proving the value travels config file → graph →
  inner state → rendered answer.

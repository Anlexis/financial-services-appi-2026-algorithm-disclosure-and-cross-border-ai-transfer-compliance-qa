"""AgentCore Platform v1.0"""

# State must be a flat TypedDict — never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects
# cause silent corruption.  Extend AgentState with agent-specific
# fields only.  Do NOT add credentials, secrets, or Pydantic models.
#
# FIN-C2-145 — APPI 2026 Algorithm Disclosure & Cross-Border AI Transfer
#               Compliance Q&A
#
# Two-layer nested Cat 2 graph: outer backbone (AgentBaseGraph) + inner
# domain workflow (BaseGraph).  Fields below cover both layers.
#
# Security notes:
#   - validated_input: sanitised NL question; raw user_input discarded after
#     the input gate
#   - compliance_answer: retained in State for the output gate only; never
#     stored persistently
#   - No API keys, JWT tokens, or personal data in State (checkpoint DB
#     leakage risk)
#   - Credentials are accessed via config["configurable"] / InvocationContext only

from typing import Any, Dict, List, Optional

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Flat TypedDict for FIN-C2-145 APPI 2026 Compliance Q&A Agent.

    All shared fields (user_input, status, session_id, node_history,
    error_log, hitl_*, output, etc.) are inherited from AgentState.
    """

    # ------------------------------------------------------------------
    # Outer layer — set by PreProcessNode / AppiComplianceGraphNode.merge_output
    # ------------------------------------------------------------------

    # Validated compliance question produced by the PreProcessNode gate.
    # Raw user_input is NOT persisted beyond PreProcessNode.
    # Contains the cleaned NL query string (no personal data, no
    # instruction-override patterns).
    validated_input: Optional[str]

    # Validated caller fields from input_context (set only when supplied):
    # topic_filter restricts retrieval to one topic area; top_k bounds the
    # number of cited articles per request.
    topic_filter: Optional[str]
    top_k: Optional[int]

    # Final compliance answer mapped by merge_output() from inner graph output.
    # Also held in state["result"] for the PostProcessNode output gate.
    result: Optional[str]

    # Cited APPI article IDs surfaced to the outer state by merge_output().
    # e.g. ["第24条", "第17条改正", "自動化意思決定"]
    cited_articles: Optional[List[str]]

    # Compliance risk level surfaced by merge_output().
    # Values: "HIGH" | "MEDIUM" | "LOW"
    risk_level: Optional[str]

    # True when risk_level is HIGH and the enforcement deadline warning
    # must be injected into the output by PostProcessNode.
    enforcement_deadline_warning: Optional[bool]

    # ------------------------------------------------------------------
    # Declared runtime config (config/config.yaml) — seeded into inner
    # state by DomainWorkflowGraph._extra_initial_state()
    # ------------------------------------------------------------------

    # Default number of cited articles when the caller supplies no top_k.
    default_top_k: Optional[int]

    # Declared enforcement date (ISO form, e.g. "2026-07-06") rendered into
    # the answer checklist and the HIGH-risk deadline warning.
    enforcement_deadline: Optional[str]

    # ------------------------------------------------------------------
    # Inner layer — domain nodes (DomainWorkflowGraph)
    # ------------------------------------------------------------------

    # RetrieveNode output — APPI 2026 KB chunks matching the query.
    # Each entry: {
    #   "article_id": str,      — APPI article identifier (e.g. "第24条")
    #   "topic": str,           — compliance topic key
    #   "text": str,            — article excerpt text
    #   "relevance_score": float,  — BM25/embedding similarity score (0.0–1.0)
    # }
    # No credentials in retrieved_chunks.
    retrieved_chunks: Optional[List[Dict[str, Any]]]

    # RankNode output — retrieved_chunks reranked by relevance_score DESC,
    # truncated to the effective top-K.
    ranked_chunks: Optional[List[Dict[str, Any]]]

    # GenerateNode output — generated compliance answer string.
    # Must pass the PostProcessNode credential scan before surfacing.
    compliance_answer: Optional[str]

    # ------------------------------------------------------------------
    # Tracing / audit — framework-managed; do NOT write from node code
    # ------------------------------------------------------------------

    trace_id: Optional[str]
    correlation_id: Optional[str]
    # node_history inherited from AgentState; listed here for clarity.
    # node_history: Optional[List[str]]

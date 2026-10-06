"""AgentCore Platform v1.0"""

# FIN-C2-145 — RankNode
# Inner domain node — chunk reranking step.
# Reranks the retrieved APPI 2026 knowledge chunks by relevance to the query.
#
# In production, this node calls a cross-encoder reranker model (e.g.
# Cohere Rerank, BGE-reranker) to improve retrieval precision. This
# implementation uses a deterministic stub that sorts by the relevance_score
# set by RetrieveNode and applies a score threshold filter.
#
# Truncation count precedence: per-request top_k (validated caller field,
# forwarded by RetrieveNode) > default_top_k (declared in
# config/config.yaml, seeded into inner state) > the literal default below.
#
# required_trust_level = ANONYMOUS: inner domain node; the outer
# pre_process gate has already verified the caller.
#
# Audit: emit_trace_event("rank_complete", payload, state) on every path.

import logging
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.content_screens import TOP_K_MAX, TOP_K_MIN, parse_bounded_int

logger = logging.getLogger(__name__)

# Relevance threshold — only chunks with a strictly positive score pass.
# A zero score means the chunk shares nothing with the question; letting it
# through would pad answers with unrelated articles and let their topics
# drive the risk classification (every broad answer would classify HIGH).
_SCORE_THRESHOLD = 0.0

# Literal fallback for the number of chunks passed to GenerateNode when
# neither the caller nor config/config.yaml supplies one (context window guard).
_DEFAULT_TOP_K = 5


def _effective_top_k(state: Dict[str, Any]) -> int:
    """Resolve the truncation count: caller value > declared config > literal."""
    for key in ("top_k", "default_top_k"):
        value = parse_bounded_int(state.get(key), TOP_K_MIN, TOP_K_MAX)
        if value is not None:
            return value
    return _DEFAULT_TOP_K


class RankNode(FunctionNode):
    """Rerank retrieved APPI 2026 KB chunks by relevance to the query.

    Inner domain node.  Accepts retrieved_chunks (from RetrieveNode), applies
    relevance-score-based reranking, score filtering, and top-K truncation.

    Input state keys:
        retrieved_chunks: list of {article_id, topic, text, relevance_score}
        top_k:            (optional) validated per-request truncation count
        default_top_k:    (optional) declared default from config/config.yaml

    Output state keys (partial dict):
        ranked_chunks: top-K reranked chunks (list of dicts)
        status:        AgentStatus.SUCCESS.value or AgentStatus.ERROR.value
        error_log:     (on error) list of error message strings
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:  # noqa: ARG002
        """Rerank and filter retrieved chunks. Returns ONLY changed state keys."""
        retrieved: List[Dict[str, Any]] = state.get("retrieved_chunks") or []

        if not retrieved:
            # No chunks to rank — surface empty ranked_chunks (not an error;
            # GenerateNode will produce a "no matching articles" answer).
            emit_trace_event(
                "rank_complete",
                {"input_count": 0, "output_count": 0, "status": "empty_input"},
                state,
            )
            logger.info("RankNode: no retrieved chunks to rank; returning empty ranked_chunks")
            return {
                "ranked_chunks": [],
                "status": AgentStatus.SUCCESS.value,
            }

        # Filter by score threshold, then sort descending, then top-K
        above_threshold = [
            c
            for c in retrieved
            if isinstance(c.get("relevance_score"), (int, float)) and c["relevance_score"] > _SCORE_THRESHOLD
        ]

        # Sort by relevance_score descending; stable sort preserves KB order for ties
        above_threshold.sort(key=lambda c: c.get("relevance_score", 0.0), reverse=True)

        top_k = _effective_top_k(state)
        ranked = above_threshold[:top_k]

        logger.info(
            "RankNode: %d chunks in → %d above threshold → top-%d selected",
            len(retrieved),
            len(above_threshold),
            top_k,
        )

        # Audit trail — counts only
        emit_trace_event(
            "rank_complete",
            {
                "input_count": len(retrieved),
                "above_threshold": len(above_threshold),
                "output_count": len(ranked),
                "top_k": top_k,
                "top_article_id": ranked[0]["article_id"] if ranked else None,
            },
            state,
        )

        return {
            "ranked_chunks": ranked,
            "status": AgentStatus.SUCCESS.value,
        }

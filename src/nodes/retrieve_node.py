"""AgentCore Platform v1.0"""

# FIN-C2-145 — RetrieveNode
# Inner domain node — VectorRAG retrieval step.
# Retrieves relevant APPI 2026 compliance article chunks from the KB stub
# based on the validated compliance question.
#
# In production, this node calls a vector store (e.g. Pinecone, Milvus,
# OpenSearch) using the configured embedding model. This implementation
# uses a deterministic keyword-based stub that covers the APPI 2026
# topic areas defined in docs/02_design.md.
#
# Input arrives either as a plain question string or as the JSON envelope
# built by the outer graph's extract_input() ({"query": ..., "top_k": ...,
# "topic_filter": ...}) — the framework does not forward the outer caller
# metadata into the inner graph, so validated per-request fields travel in
# that envelope.
#
# required_trust_level = ANONYMOUS: inner domain node called exclusively from
# within DomainWorkflowGraph; the outer PreProcessNode gate has already
# verified the caller trust level.
#
# Audit: emit_trace_event("retrieve_complete", payload, state) on every path.

import json
import logging
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.content_screens import TOP_K_MAX, TOP_K_MIN, TOPIC_FILTERS, parse_bounded_int

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# APPI 2026 KB stub — deterministic article chunks.
# Structure: {"article_id": str, "topic": str, "text": str, "keywords": list}
# In production this is replaced by a vector store embedding index.
# ---------------------------------------------------------------------------
_APPI_KB: List[Dict[str, Any]] = [
    {
        "article_id": "第17条改正",
        "topic": "appi_2026_automated_decisions",
        "text": (
            "APPI 2026 第17条改正: 自動化意思決定（AI信用スコアリング・投資推薦等）において、"
            "事業者はデータ主体の要求に応じてアルゴリズムの主要因子を開示する義務を負う。"
            "信用拒否・投資推薦においては人間によるレビューパスを設けることが要件とされる。"
        ),
        "keywords": [
            "automated",
            "decision",
            "credit",
            "score",
            "algorithm",
            "disclosure",
            "自動化",
            "意思決定",
            "信用",
            "スコア",
            "アルゴリズム",
            "開示",
        ],
    },
    {
        "article_id": "第三者提供",
        "topic": "appi_2026_algorithm_disclosure",
        "text": (
            "APPI 2026 第三者提供規制: 金融AI事業者が第三者（外部AIベンダー等）にデータを"
            "提供する際は、本人同意または正当根拠が必要。AI学習・推論に使用する個人データの"
            "第三者提供には提供先の監督義務が課せられる。"
        ),
        "keywords": [
            "third party",
            "provision",
            "vendor",
            "AI",
            "training",
            "consent",
            "第三者",
            "提供",
            "ベンダー",
            "学習",
            "同意",
        ],
    },
    {
        "article_id": "第24条",
        "topic": "appi_2026_cross_border_transfer",
        "text": (
            "APPI 2026 第24条: 個人データの外国への提供（クロスボーダー転送）には、"
            "十分な保護措置を有する国・地域への移転または本人の明示的同意が必要。"
            "AI学習用データの海外転送は本条の対象となる。外国の第三者への提供時は"
            "定期的な体制確認義務あり（年1回以上）。"
        ),
        "keywords": [
            "cross-border",
            "transfer",
            "foreign",
            "overseas",
            "international",
            "クロスボーダー",
            "外国",
            "越境",
            "転送",
            "海外",
        ],
    },
    {
        "article_id": "第18条",
        "topic": "appi_2026_vendor_contracts",
        "text": (
            "APPI 2026 第18条: 個人データの取扱いを委託する場合、委託先に対して必要かつ"
            "適切な監督を行う義務を負う。外部AIベンダーとの契約には監査権・削除義務・"
            "再委託禁止条項を含めることが推奨される。"
        ),
        "keywords": [
            "vendor",
            "contract",
            "audit",
            "delegation",
            "supervision",
            "委託",
            "ベンダー",
            "契約",
            "監査",
            "監督",
        ],
    },
    {
        "article_id": "算出ロジック開示",
        "topic": "appi_2026_algorithm_disclosure",
        "text": (
            "APPI 2026 アルゴリズム開示要件: 信用スコアリング・ローン審査・投資推薦に"
            "使用するAIシステムは、審査拒否時に主要判定要因（特徴量上位5項目等）を"
            "申請者に開示しなければならない（2026年7月6日施行）。"
        ),
        "keywords": [
            "algorithm",
            "disclosure",
            "credit scoring",
            "loan",
            "investment",
            "recommendation",
            "feature",
            "factor",
            "アルゴリズム",
            "開示",
            "信用スコア",
            "ローン",
            "投資推薦",
            "特徴量",
        ],
    },
    {
        "article_id": "リスク管理",
        "topic": "appi_2026_automated_decisions",
        "text": (
            "APPI 2026 高リスクAIリスク管理: 与信・保険引受・採用等に使用するAIシステムは"
            "リスク評価・バイアス監査を実施し、結果を個人情報保護委員会に報告する義務がある。"
            "2026年7月6日施行。"
        ),
        "keywords": [
            "risk",
            "management",
            "bias",
            "audit",
            "high risk",
            "insurance",
            "リスク",
            "管理",
            "バイアス",
            "監査",
            "高リスク",
            "保険",
        ],
    },
]

# Number of chunks to retrieve before ranking
_RETRIEVE_TOP_N = 10


def _score_chunk(chunk: Dict[str, Any], query_lower: str) -> float:
    """Score a KB chunk against the query using keyword overlap (BM25-stub)."""
    score = 0.0
    for kw in chunk.get("keywords", []):
        if kw.lower() in query_lower:
            score += 1.0
    # Partial match bonus: check if any word in the query appears in article text
    text_lower = chunk.get("text", "").lower()
    for word in query_lower.split():
        if len(word) >= 3 and word in text_lower:
            score += 0.3
    return round(score, 3)


def _parse_envelope(raw: str) -> Tuple[str, Optional[int], Optional[str]]:
    """Split the inner-graph input into (query, top_k, topic_filter).

    The outer graph JSON-encodes the validated question plus any
    per-request fields; a plain (non-JSON-object) string is treated as the
    query itself so the inner graph stays directly invocable in tests.
    Envelope fields are re-validated against the same bounds the input gate
    uses; a value that fails them is discarded in favour of the declared
    defaults (the strict caller-facing refusal already happened at the
    gate).
    """
    query = raw
    top_k: Optional[int] = None
    topic_filter: Optional[str] = None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return query, None, None
    if not isinstance(parsed, dict) or "query" not in parsed:
        return query, None, None
    query = str(parsed.get("query") or "")
    top_k = parse_bounded_int(parsed.get("top_k"), TOP_K_MIN, TOP_K_MAX)
    raw_topic = parsed.get("topic_filter")
    if isinstance(raw_topic, str) and raw_topic in TOPIC_FILTERS:
        topic_filter = raw_topic
    return query, top_k, topic_filter


class RetrieveNode(FunctionNode):
    """VectorRAG retrieval step for the APPI 2026 compliance KB.

    Inner domain node — retrieves relevant APPI 2026 article chunks from the
    knowledge base stub based on the validated compliance question. When the
    caller restricted the question to a topic area (topic_filter), only
    chunks of that topic are considered.

    Input state keys:
        validated_input: str — compliance question (when run in the outer state)
        user_input:      str — plain question or the outer graph's JSON envelope

    Output state keys (partial dict):
        retrieved_chunks: list of chunk dicts sorted by relevance_score DESC
        top_k:            (only when the envelope carried one) validated per-request count
        status:           AgentStatus.SUCCESS.value or AgentStatus.ERROR.value
        error_log:        (on error) list of error message strings
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:  # noqa: ARG002
        """Retrieve APPI 2026 KB chunks matching the compliance question."""
        raw: str = state.get("validated_input") or state.get("user_input") or ""

        query, top_k, topic_filter = _parse_envelope(raw)

        if not query or not query.strip():
            emit_trace_event(
                "retrieve_complete",
                {"status": "error", "reason": "empty_query", "chunk_count": 0},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["RetrieveNode: no query available for retrieval"],
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                "formatted_output": "Request could not be completed. "
                + ("RetrieveNode: no query available for retrieval"),
            }

        query_lower = query.lower()

        # Restrict the corpus when the caller narrowed the question to one
        # topic area; otherwise score the full KB.
        corpus = _APPI_KB
        if topic_filter is not None:
            internal_topic = TOPIC_FILTERS[topic_filter]
            corpus = [chunk for chunk in _APPI_KB if chunk["topic"] == internal_topic]

        # Score every candidate chunk against the query
        scored: List[Dict[str, Any]] = []
        for chunk in corpus:
            score = _score_chunk(chunk, query_lower)
            scored.append(
                {
                    "article_id": chunk["article_id"],
                    "topic": chunk["topic"],
                    "text": chunk["text"],
                    "relevance_score": score,
                }
            )

        # Sort by relevance descending; take top N
        scored.sort(key=lambda c: c["relevance_score"], reverse=True)
        retrieved = scored[:_RETRIEVE_TOP_N]

        logger.info("RetrieveNode: retrieved %d chunks for query (len=%d)", len(retrieved), len(query))

        # Audit trail — counts and identifiers only
        emit_trace_event(
            "retrieve_complete",
            {
                "query_length": len(query),
                "chunk_count": len(retrieved),
                "topic_filter": topic_filter,
                "top_article_id": retrieved[0]["article_id"] if retrieved else None,
            },
            state,
        )

        delta: Dict[str, Any] = {
            "retrieved_chunks": retrieved,
            # Publish the clean question so downstream nodes never see the
            # JSON envelope form.
            "validated_input": query,
            "status": AgentStatus.SUCCESS.value,
        }
        if top_k is not None:
            delta["top_k"] = top_k
        return delta

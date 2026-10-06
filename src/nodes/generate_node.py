"""AgentCore Platform v1.0"""

# FIN-C2-145 — GenerateNode
# Inner domain node — compliance answer generation step.
# Composes a compliance answer from ranked APPI 2026 KB chunks, extracts
# cited article IDs, determines risk_level, and flags the enforcement
# deadline warning for HIGH-risk findings.
#
# In production, this node calls an LLM with the ranked context and a
# system prompt that instructs it to cite specific APPI articles and
# classify risk level. This implementation uses a deterministic stub that
# composes the answer from the top-ranked chunks.
#
# The enforcement deadline rendered into the answer comes from
# config/config.yaml (`enforcement_deadline`, seeded into inner state);
# the literal below is the fallback when no value is declared.
#
# required_trust_level = ANONYMOUS: inner domain node; the outer
# pre_process gate has already verified the caller.
#
# Audit: emit_trace_event("generate_complete", payload, state) on success path.

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

logger = logging.getLogger(__name__)

# Risk classification: topics that carry HIGH risk under APPI 2026.
_HIGH_RISK_TOPICS = frozenset(
    {
        "appi_2026_cross_border_transfer",
        "appi_2026_automated_decisions",
    }
)

# Topics with MEDIUM risk.
_MEDIUM_RISK_TOPICS = frozenset(
    {
        "appi_2026_algorithm_disclosure",
        "appi_2026_vendor_contracts",
    }
)

# Fallback enforcement date (ISO form) when config/config.yaml declares none.
_DEFAULT_ENFORCEMENT_DEADLINE = "2026-07-06"

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def format_deadline_ja(iso_date: Optional[str]) -> str:
    """Render an ISO enforcement date as a Japanese date string.

    Falls back to the default enforcement date when the declared value is
    absent or not an ISO date — the deadline printed in a compliance answer
    must never be blank or malformed.
    """
    candidate = iso_date if isinstance(iso_date, str) else ""
    m = _ISO_DATE_RE.match(candidate)
    if m is None:
        m = _ISO_DATE_RE.match(_DEFAULT_ENFORCEMENT_DEADLINE)
        assert m is not None  # the literal above is always ISO
    year, month, day = (int(part) for part in m.groups())
    return f"{year}年{month}月{day}日"


def _classify_risk(topics: List[str]) -> str:
    """Return HIGH / MEDIUM / LOW based on the topics present in ranked chunks."""
    topic_set = set(topics)
    if topic_set & _HIGH_RISK_TOPICS:
        return "HIGH"
    if topic_set & _MEDIUM_RISK_TOPICS:
        return "MEDIUM"
    return "LOW"


def _compose_answer(query: str, chunks: List[Dict[str, Any]], deadline_ja: str) -> str:
    """Compose a structured compliance answer from the top-ranked chunks.

    This deterministic stub concatenates chunk text with article citations.
    In production, this is replaced by an LLM call with the chunks as context.
    """
    if not chunks:
        return (
            "APPI 2026に関するご質問を承りましたが、"
            "ご質問に対応する規制条文が見つかりませんでした。"
            "より具体的な質問内容（例：クロスボーダー転送、アルゴリズム開示、ベンダー契約等）"
            "を含めて再度お問い合わせください。"
        )

    parts = [
        "【APPI 2026 コンプライアンス回答】",
        "",
        f"ご質問：{query[:100]}{'...' if len(query) > 100 else ''}",
        "",
        "【適用条文・義務】",
    ]

    for chunk in chunks:
        article_id = chunk.get("article_id", "")
        text = chunk.get("text", "").strip()
        parts.append(f"▶ {article_id}")
        parts.append(text)
        parts.append("")

    parts.append("【アクションチェックリスト】")
    topics = [c.get("topic", "") for c in chunks]
    if "appi_2026_cross_border_transfer" in topics:
        parts.append("□ 個人データの越境転送の実態を確認し、第24条の適切な措置を実施する")
    if "appi_2026_algorithm_disclosure" in topics or "appi_2026_automated_decisions" in topics:
        parts.append("□ AIシステムによる自動意思決定のアルゴリズム開示体制を整備する")
    if "appi_2026_vendor_contracts" in topics:
        parts.append("□ 外部AIベンダー契約に監査権・削除義務・再委託禁止条項を追加する")
    parts.append(f"□ {deadline_ja}施行日に向けた社内規程改訂計画を策定する")

    return "\n".join(parts)


class GenerateNode(FunctionNode):
    """Generate the APPI 2026 compliance answer from ranked KB chunks.

    Inner domain node.  Composes a structured answer with article citations,
    risk classification, and enforcement deadline warning for HIGH-risk findings.

    Input state keys:
        ranked_chunks:        top-K reranked APPI 2026 KB chunks (from RankNode)
        validated_input:      compliance question string
        enforcement_deadline: (optional) declared ISO date from config/config.yaml

    Output state keys (partial dict):
        compliance_answer:            generated compliance answer string
        cited_articles:               list of cited APPI article ID strings
        risk_level:                   "HIGH" | "MEDIUM" | "LOW"
        enforcement_deadline_warning: True if risk_level == "HIGH"
        status:                       AgentStatus.SUCCESS.value or AgentStatus.ERROR.value
        error_log:                    (on error) list of error message strings
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:  # noqa: ARG002
        """Generate the compliance answer. Returns ONLY changed state keys."""
        ranked_chunks: List[Dict[str, Any]] = state.get("ranked_chunks") or []
        # validated_input carries the clean question (published by
        # RetrieveNode after envelope parsing); user_input is the direct
        # unit-test fallback.
        query: str = state.get("validated_input") or state.get("user_input") or ""
        deadline_ja = format_deadline_ja(state.get("enforcement_deadline"))

        # Extract cited article IDs from the ranked chunks
        cited_articles: List[str] = [
            c["article_id"] for c in ranked_chunks if c.get("article_id") and c.get("relevance_score", 0.0) > 0.0
        ]
        # Preserve order, deduplicate
        seen: set[str] = set()
        unique_articles: List[str] = []
        for art in cited_articles:
            if art not in seen:
                seen.add(art)
                unique_articles.append(art)

        # Classify risk level from the topics of ranked chunks
        topics = [c.get("topic", "") for c in ranked_chunks]
        risk_level = _classify_risk(topics)
        enforcement_deadline_warning = risk_level == "HIGH"

        # Compose the compliance answer
        compliance_answer = _compose_answer(query, ranked_chunks, deadline_ja)

        logger.info(
            "GenerateNode: generated answer (len=%d) risk=%s articles=%s",
            len(compliance_answer),
            risk_level,
            unique_articles,
        )

        # Audit trail — identifiers and counts only
        emit_trace_event(
            "generate_complete",
            {
                "answer_length": len(compliance_answer),
                "cited_article_count": len(unique_articles),
                "cited_articles": unique_articles,
                "risk_level": risk_level,
                "enforcement_deadline_warning": enforcement_deadline_warning,
            },
            state,
        )

        return {
            "compliance_answer": compliance_answer,
            "cited_articles": unique_articles,
            "risk_level": risk_level,
            "enforcement_deadline_warning": enforcement_deadline_warning,
            "status": AgentStatus.SUCCESS.value,
        }

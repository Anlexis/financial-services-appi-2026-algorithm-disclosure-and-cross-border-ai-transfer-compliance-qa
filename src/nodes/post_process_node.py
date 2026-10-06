"""AgentCore Platform v1.0"""

# FIN-C2-145 — PostProcessNode
# Outer backbone post_process slot — output gate on the exact text that
# leaves the agent.
#
# This template renders NO monetary aggregates of its own: the answer text
# quotes the curated regulatory knowledge base — including statutory figures
# — verbatim, so a numeric rounding grid would corrupt legally accurate
# values. The output invariant this template owns instead is:
#
#   1. Released answers are never empty.
#   2. Every released answer carries the mandated legal disclaimer, and
#      HIGH-risk answers carry the enforcement-deadline warning; both are
#      appended here and re-verified on the final composed text before
#      release (fail-closed).
#   3. No credential material leaves the agent (patterns shared with the
#      input boundary via src/nodes/content_screens.py).
#
# On a violation the node must not merely fail: the response envelope falls
# back to state["result"] even on an error status, so the blocked text has
# to be CLEARED from every output-bearing field or it would still ship
# inside the error envelope.
#
# The framework's own final output gate also runs automatically — this is
# an additional domain check on top of it.

import logging
from typing import Any, ClassVar, Dict, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.content_screens import scan_credentials
from src.nodes.generate_node import format_deadline_ja
from src.services.llm_factory import resolve_llm
from src.services.llm_review import render_review, review_result
from src.services.source_disclosure import source_label

logger = logging.getLogger(__name__)

# Mandatory disclaimer for all compliance answers
_DISCLAIMER = (
    "\n\n---\n"
    "【免責事項】本回答はAPPI 2026に関する一般的な情報提供を目的とするものであり、"
    "法的助言を構成するものではありません。"
    "個別案件への適用については、資格を有する日本の法律専門家にご確認ください。"
    " (Confirm with qualified Japanese legal counsel.)"
)

# Enforcement deadline warning injected for HIGH-risk findings; the date is
# filled from the declared enforcement_deadline (config/config.yaml).
_DEADLINE_WARNING_TEMPLATE = (
    "\n\n⚠️ 【緊急】APPI 2026施行日: {deadline}\n"
    "本回答に含まれる義務はすでに施行済みです。直ちに対応を開始してください。"
)


# Module-level domain gate (not a class method — the framework's own gate
# methods are final and must not be overridden).
def _security_gate_output(formatted: str, high_risk: bool, deadline_ja: str) -> None:
    """Enforce the release invariant on the final composed text.

    Raises ValueError when the answer is empty, when the mandated
    disclaimer is missing, when a HIGH-risk answer lacks the
    enforcement-deadline warning, or when credential material is present.
    Error messages name the violated rule, never the content.
    """
    if not formatted.strip():
        raise ValueError("output gate: released answer must not be empty")
    if _DISCLAIMER not in formatted:
        raise ValueError("output gate: mandated legal disclaimer missing from answer")
    if high_risk and _DEADLINE_WARNING_TEMPLATE.format(deadline=deadline_ja) not in formatted:
        raise ValueError("output gate: enforcement-deadline warning missing from high-risk answer")
    if scan_credentials(formatted) is not None:
        raise ValueError("output gate: credential pattern detected in response")


class PostProcessNode(FunctionNode):
    """Output gate for FIN-C2-145 APPI 2026 Compliance Q&A.

    Outer backbone post_process slot. Reads state["result"] (the compliance
    answer merged from the inner workflow) and:
      1. Verifies the answer is non-empty
      2. Appends the enforcement-deadline warning (HIGH risk only)
      3. Appends the mandatory legal disclaimer
      4. Enforces the release invariant on the FINAL composed text
         (non-empty, disclaimer present, warning present when required,
         credential-free) — fail closed, clearing every output-bearing
         field on violation
      5. Emits an audit event on every path

    Input state keys:
        result:                       compliance answer string (from merge_output)
        risk_level:                   "HIGH" | "MEDIUM" | "LOW"
        enforcement_deadline_warning: True if HIGH risk
        enforcement_deadline:         (optional) declared ISO date

    Output state keys (partial dict — ONLY changed keys):
        result:           released compliance answer (disclaimer appended)
        formatted_output: same as result (response envelope source)
        status:           AgentStatus.SUCCESS.value or AgentStatus.ERROR.value
        error_log:        (on error) list of error message strings
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:  # noqa: ARG002
        """Compose and gate the released answer. Returns ONLY changed state keys."""
        result: str = state.get("result") or ""
        risk_level: str = state.get("risk_level") or "MEDIUM"
        deadline_warning: bool = bool(state.get("enforcement_deadline_warning"))
        deadline_ja = format_deadline_ja(state.get("enforcement_deadline"))

        # Non-empty check on the inner answer before composition
        if not result or not result.strip():
            emit_trace_event(
                "post_process_complete",
                {"outcome": "blocked", "reason": "empty_result"},
                state,
            )
            logger.warning("PostProcessNode: result is empty")
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PostProcessNode: result is empty — no compliance answer generated"],
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                "formatted_output": "Request could not be completed. "
                + ("PostProcessNode: result is empty — no compliance answer generated"),
            }

        # Compose final output: answer + [deadline warning if HIGH] + disclaimer
        # Say where the answer came from. This agent answers from a corpus defined inside
        # its own module; a reader seeing a citation has no way to tell that from a live query
        # against the system of record, and the review round rated that confusion its most
        # serious finding. Added before the gate below so it passes the same checks the answer
        # does.
        result = result + source_label(state)
        high_risk = deadline_warning or risk_level == "HIGH"

        _llm, _ = resolve_llm(None, state)
        _remarks = review_result(
            _llm,
            user_input=str(state.get("user_input") or ""),
            result=result,
            domain="FIN APPI2026ComplianceQAAgent",
        )
        _review = render_review(_remarks)
        # Remarks are LLM text derived from the caller's raw words, so they go through the
        # same release invariant the answer does. This gate signals by raising, so the merge
        # is attempted and abandoned rather than tested: a tripped review is dropped on its
        # own, because an advisory reader that could withhold a correct answer would be
        # changing the outcome, and the design rests on it being unable to.
        if _review and isinstance(result, str):
            try:
                _security_gate_output(
                    result
                    + _review
                    + (_DEADLINE_WARNING_TEMPLATE.format(deadline=deadline_ja) if high_risk else "")
                    + _DISCLAIMER,
                    high_risk,
                    deadline_ja,
                )
            except ValueError:
                pass
            else:
                result = result + _review
        formatted = result
        if high_risk:
            formatted = formatted + _DEADLINE_WARNING_TEMPLATE.format(deadline=deadline_ja)
        formatted = formatted + _DISCLAIMER

        # Release invariant on the exact text that leaves the agent. On a
        # violation, CLEAR every output-bearing field — the response
        # envelope falls back to state["result"] even on error status, so
        # returning an error alone would still ship the blocked text.
        try:
            _security_gate_output(formatted, high_risk, deadline_ja)
        except ValueError as violation:
            emit_trace_event(
                "post_process_complete",
                {"outcome": "blocked", "reason": "release_invariant_violation"},
                state,
            )
            logger.error("PostProcessNode: output blocked — %s", violation)
            return {
                "status": AgentStatus.ERROR.value,
                "result": "",
                "formatted_output": "",
                "cited_articles": [],
                "error_log": [f"PostProcessNode: {violation}"],
            }

        logger.info(
            "PostProcessNode: output gate PASS — len=%d risk=%s deadline_warn=%s",
            len(formatted),
            risk_level,
            deadline_warning,
        )

        # Audit trail — length and flags only, never the response body
        emit_trace_event(
            "post_process_complete",
            {
                "outcome": "released",
                "result_length": len(formatted),
                "risk_level": risk_level,
                "enforcement_deadline_warning": deadline_warning,
                "disclaimer_injected": True,
            },
            state,
        )

        return {
            "result": formatted,
            "formatted_output": formatted,
            "status": AgentStatus.SUCCESS.value,
        }

"""AgentCore Platform v1.0"""

# FIN-C2-145 — PreProcessNode
# Outer backbone pre_process slot — the external-facing input validation gate.
#
# Gate sequence (all checks run inside execute(); patterns live in
# src/nodes/content_screens.py so the input and output boundaries share one
# definition):
#   (1) type / empty / length guards on the question text
#   (2) instruction-override screen (raw + whitespace-collapsed)
#   (3) personal-data screen (email / phone / credit card) — domain rule:
#       compliance questions describe situations, they never carry the data
#   (4) structured caller channel (input_context): unknown fields refused,
#       depth-first injection screen over every string INCLUDING keys, then
#       per-field bounds — channel (inert identifier), topic_filter
#       (fixed allowlist), top_k (fail-closed bounded integer)
#   (5) audit event on EVERY path — reason codes and counts only, never the
#       caller's text
#
# required_trust_level = VERIFIED_EXTERNAL: the outer backbone gate is the
# only trust barrier for external callers. Inner domain nodes use ANONYMOUS.

import logging
from typing import Any, ClassVar, Dict, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.content_screens import (
    CHANNEL_RE,
    TOP_K_MAX,
    TOP_K_MIN,
    TOPIC_FILTERS,
    parse_bounded_int,
    scan_context_structure,
    scan_injection,
    scan_pii,
)


# The Marketplace runner seeds input_context with its own conversation history on every
# invocation (shared/bootstrap/marketplace_app.py); the caller neither sends that key nor can
# suppress it, and the build_input_context hook can only overwrite its value, never remove it.
# It is platform plumbing rather than caller data, so it is dropped here, before the caller
# contract runs: the unknown-field guard below stays strict for everything a caller can
# actually send, and no value screen is ever asked to judge a transcript that contains this
# agent's own earlier answers. The value may also be None, which this tolerates.
_PLATFORM_CONTEXT_KEYS = frozenset({"conversation_history"})


def _without_platform_context(raw: Any) -> Any:
    """The caller-supplied half of input_context, platform-injected keys removed."""
    if not isinstance(raw, dict):
        return raw
    return {k: v for k, v in raw.items() if k not in _PLATFORM_CONTEXT_KEYS}


logger = logging.getLogger(__name__)

# Maximum accepted question length (characters). Real compliance questions
# are a few hundred characters; the cap refuses bulk payloads long before
# they reach retrieval or rendering.
_MAX_QUERY_LENGTH = 2000

# The complete caller contract for the structured channel. Anything else in
# input_context is refused — unknown fields are counted, never echoed.
_ALLOWED_CONTEXT_FIELDS = frozenset({"channel", "topic_filter", "top_k"})


def _reject(reason: str, message: str, state: Dict[str, Any]) -> Dict[str, Any]:
    """Uniform refusal: audit the reason code, name the field, echo nothing."""
    emit_trace_event(
        "pre_process_rejected",
        {"reason": reason},
        state,
    )
    logger.warning("PreProcessNode: rejected (%s)", reason)
    return {
        "status": AgentStatus.ERROR.value,
        "error_log": [f"PreProcessNode: {message}"],
        # The runner surfaces `formatted_output or result` as `output`. A reason left only in
        # error_log reaches no one: the terminal result carries just `status`, and get_output()
        # does not copy error_log out of the graph -- the caller sees a blank spinner.
        "formatted_output": "Request could not be completed. " + (f"PreProcessNode: {message}"),
    }


class PreProcessNode(FunctionNode):
    """Input validation gate for FIN-C2-145 APPI 2026 Compliance Q&A.

    Validates the caller's compliance question and the structured caller
    channel before the inner domain workflow runs. Every caller field is
    validated against explicit bounds and fails CLOSED; rejected values are
    never echoed into errors or logs (the field or reason is named, the
    value is not).

    The refusal screens are the template's own guarantee, enforced here
    regardless of any platform-side input scan.

    Input state keys:
        user_input:    compliance question string (NL query)
        input_context: structured caller metadata (channel, topic_filter,
                       top_k) — read-only

    Output state keys (partial dict):
        validated_input: cleaned query string (on success)
        topic_filter:    validated topic identifier (only when supplied)
        top_k:           validated article count (only when supplied)
        status:          AgentStatus.SUCCESS.value or AgentStatus.ERROR.value
        error_log:       (on error) list of error message strings
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:  # noqa: ARG002
        """Validate the compliance question. Returns ONLY changed state keys."""
        user_input = state.get("user_input", "")
        input_context = _without_platform_context(state.get("input_context", {}))  # read-only

        # (1) Type guard: user_input must be a non-empty string
        if not isinstance(user_input, str):
            return _reject(
                "non_string_input",
                f"user_input must be a string, got {type(user_input).__name__}",
                state,
            )

        stripped = user_input.strip()

        if not stripped:
            return _reject("empty_input", "user_input is empty or blank", state)

        if len(stripped) > _MAX_QUERY_LENGTH:
            return _reject(
                "input_too_long",
                f"user_input exceeds maximum length ({len(stripped)} chars, max {_MAX_QUERY_LENGTH})",
                state,
            )

        # (2) Instruction-override screen — raw and whitespace-collapsed.
        # The screen name is audited; neither it nor the text is echoed.
        if scan_injection(stripped) is not None:
            return _reject(
                "injection_pattern",
                "input rejected — disallowed pattern detected",
                state,
            )

        # (3) Personal-data screen (domain rule). The refusal names the
        # CATEGORY only.
        pii_type = scan_pii(stripped)
        if pii_type is not None:
            return _reject(
                "pii_detected",
                f"input rejected — personal data detected ({pii_type}). "
                f"Do not include personal data in compliance questions.",
                state,
            )

        # (4) Structured caller channel
        if not isinstance(input_context, dict):
            return _reject("context_not_mapping", "input_context must be an object", state)

        unknown = [key for key in input_context if key not in _ALLOWED_CONTEXT_FIELDS]
        if unknown:
            # Field names are caller-controlled text — report the count only.
            return _reject(
                "unknown_context_field",
                f"input_context carries {len(unknown)} unrecognized field(s)",
                state,
            )

        # Screen the whole structure — keys included — before any field is
        # read. JSON escape forms are already decoded at this point, so an
        # escaped directive cannot slip past the scan.
        if scan_context_structure(input_context) is not None:
            return _reject(
                "context_injection_pattern",
                "input_context rejected by the instruction-override screen",
                state,
            )

        delta: Dict[str, Any] = {}

        channel = "unknown"
        if "channel" in input_context:
            channel = input_context["channel"]
            if not isinstance(channel, str) or not CHANNEL_RE.match(channel):
                return _reject(
                    "invalid_channel",
                    "input_context.channel is not an allowed channel identifier",
                    state,
                )

        if "topic_filter" in input_context:
            topic = input_context["topic_filter"]
            if not isinstance(topic, str) or topic not in TOPIC_FILTERS:
                return _reject(
                    "invalid_topic_filter",
                    "input_context.topic_filter is not a recognized topic identifier",
                    state,
                )
            delta["topic_filter"] = topic

        if "top_k" in input_context:
            top_k = parse_bounded_int(input_context["top_k"], TOP_K_MIN, TOP_K_MAX)
            if top_k is None:
                return _reject(
                    "invalid_top_k",
                    f"input_context.top_k must be an integer between {TOP_K_MIN} and {TOP_K_MAX}",
                    state,
                )
            delta["top_k"] = top_k

        # (5) Audit trail — success path. Counts and identifiers only.
        emit_trace_event(
            "pre_process_validated",
            {
                "query_length": len(stripped),
                "channel": channel,
                "topic_filter": delta.get("topic_filter"),
                "top_k": delta.get("top_k"),
            },
            state,
        )

        logger.info("PreProcessNode: validated compliance query (%d chars)", len(stripped))

        delta.update(
            {
                "validated_input": stripped,
                "status": AgentStatus.SUCCESS.value,
            }
        )
        return delta

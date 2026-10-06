"""AgentCore Platform v1.0"""

# FIN-C2-145 — shared content screens for the two boundary nodes.
#
# PreProcessNode (input gate) refuses instruction-override content and
# personal data in the caller's question and screens every string of the
# structured caller channel (input_context, keys included); PostProcessNode
# (output gate) refuses credential material in the text that leaves the
# agent. Both import their patterns from this module so the two boundaries
# can never drift apart.
#
# Design rules (both directions probed in tests/unit/test_content_screens.py):
#
#   * These screens are the template's own guarantee, not the framework's.
#     The platform input scan covers only the instruction text and only on
#     hosts where it is active; wherever it is absent the template would
#     otherwise fail OPEN. Refusal is therefore enforced in the node that
#     owns the caller contract and proven by calling execute() directly.
#   * Every directive alternative is anchored on a full phrase aimed at the
#     MODEL (an override verb plus an instruction noun, or a model role), or
#     on a chat-template control token, which has no legitimate reading in a
#     compliance question. Data-protection prose is full of directive verbs
#     ("do we have to delete personal data from our training set?", "may the
#     vendor act as a data processor?") — a substring screen would refuse
#     real questions, so bare verbs such as "delete", "act as" or SQL-style
#     keywords are deliberately NOT screened on their own. This pipeline has
#     no SQL surface: the knowledge base is an in-process structure.
#   * Text is screened both as received and after whitespace normalization,
#     the only transform this pipeline applies to caller text, so a directive
#     cannot ride through on unusual spacing.
#   * Refusals never echo the matched text — callers and logs receive the
#     screen NAME only, so rejected content cannot leak through error
#     channels.

import re
from typing import Callable, Dict, List, Optional, Tuple

Screen = Tuple[str, "re.Pattern[str]", Optional[Callable[[str], bool]]]

# ---------------------------------------------------------------------------
# Instruction-override screens (input boundary)
# ---------------------------------------------------------------------------

INJECTION_SCREENS: List[Screen] = [
    (
        # Chat-template control tokens: <|im_start|>, <|system|>, [INST],
        # <<SYS>>. These are wire-format artifacts of chat-tuned models and
        # never appear in a legitimate compliance question.
        "control_token",
        re.compile(r"<\|[a-z_]{2,32}\|>|\[/?INST\]|<</?SYS>>", re.IGNORECASE),
        None,
    ),
    (
        # "ignore/disregard/forget ... previous/system instructions/rules".
        # The noun class is instruction-nouns ONLY — never obligations, fees
        # or requirements — so "can we ignore the notification requirement?"
        # stays an ordinary question while "ignore all previous instructions"
        # is refused.
        "instruction_override",
        re.compile(
            r"\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+|the\s+|your\s+|my\s+)*"
            r"(?:previous|prior|above|earlier|preceding|original|system)\s+"
            r"(?:instruction|instructions|prompt|prompts|rule|rules|direction|directions)\b"
            r"|\b(?:ignore|disregard)\s+all\s+(?:rules|instructions)\b",
            re.IGNORECASE,
        ),
        None,
    ),
    (
        # "system: you are / system: act as" — requires the role-header form,
        # so prose like "under the data management system: how is consent
        # recorded" passes.
        "system_role_directive",
        re.compile(r"system\s*:\s*(?:you\s+are|act\s+as)", re.IGNORECASE),
        None,
    ),
    (
        # Role reassignment aimed at the MODEL. "you are now a joint data
        # controller" (a legal role) does not match — the role class is
        # model roles only.
        "role_override",
        re.compile(
            r"\byou\s+are\s+now\s+(?:a\s+|an\s+)?(?:different\s+|unrestricted\s+|new\s+|jailbroken\s+)?"
            r"(?:assistant|ai|chatbot|language\s+model|llm|dan)\b",
            re.IGNORECASE,
        ),
        None,
    ),
    (
        # Prompt exfiltration: "reveal/print your system prompt" and the
        # qualified "the system/initial/hidden instructions" forms.
        "prompt_exfiltration",
        re.compile(
            r"\b(?:reveal|show|print|repeat|output|disclose|display|dump)\s+(?:me\s+)?"
            r"(?:your\s+(?:system\s+|initial\s+|hidden\s+)?(?:prompt|prompts|instructions)"
            r"|the\s+(?:system|initial|hidden)\s+(?:prompt|prompts|instructions|message))\b",
            re.IGNORECASE,
        ),
        None,
    ),
    (
        # "new system prompt:" header form.
        "system_prompt_header",
        re.compile(r"\b(?:new|updated)\s+system\s+(?:prompt|instructions)\s*[:=]", re.IGNORECASE),
        None,
    ),
    (
        # Japanese directive forms: 「以前の指示を無視」「これまでのルールをすべて無視」
        # and system-prompt exfiltration 「システムプロンプトを表示/開示/出力」.
        # The object class is instruction nouns (指示/命令/ルール/プロンプト) —
        # 「通知義務を無視できますか」 (about an obligation) does not match.
        "ja_instruction_override",
        re.compile(
            r"(?:以前|前述|上記|これまで|今まで)の(?:指示|命令|ルール|プロンプト)(?:を|は)?(?:すべて|全て|全部)?無視"
            r"|(?:指示|命令|ルール)(?:を|は)(?:すべて|全て|全部)無視"
            r"|システムプロンプト(?:を|の)?(?:表示|開示|出力|公開)"
        ),
        None,
    ),
    (
        "script_tag",
        re.compile(r"</?script[^>]*>", re.IGNORECASE),
        None,
    ),
]

# ---------------------------------------------------------------------------
# Personal-data screens (input boundary — domain rule)
# ---------------------------------------------------------------------------
# Compliance questions must describe situations, never carry actual personal
# data: this agent's whole subject matter is the handling of such data, and a
# question that embeds a real address or card number would itself be an
# uncontrolled disclosure. Matches are refused, and the refusal names only
# the CATEGORY (email / phone / credit_card) — never the matched text.

PII_SCREENS: List[Screen] = [
    (
        "email",
        re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", re.IGNORECASE),
        None,
    ),
    (
        # Japanese (+81 international / 0X-XXXX-XXXX domestic) and US phone
        # forms. The domestic branch excludes year-tailed segments such as
        # "07-06-2026" — a date written day-month-year is legitimate prose in
        # questions about the enforcement window, not a phone number.
        "phone",
        re.compile(
            r"(?:"
            r"\+81[-\s.]?(?:\(0\))?[0-9]{1,4}[-\s.][0-9]{1,4}[-\s.][0-9]{4}"
            r"|0\d{1,4}[-\s.]\d{1,4}[-\s.](?!(?:19|20)\d{2}(?!\d))\d{4}"
            r"|\+?1[-.\s]?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}"
            r")"
        ),
        None,
    ),
    (
        "credit_card",
        re.compile(
            r"\b(?:"
            r"4[0-9]{12}(?:[0-9]{3})?"
            r"|(?:5[1-5][0-9]{2}|222[1-9]|22[3-9][0-9]|2[3-6][0-9]{2}|27[01][0-9]|2720)[0-9]{12}"
            r"|3[47][0-9]{13}"
            r"|3(?:0[0-5]|[68][0-9])[0-9]{11}"
            r"|6(?:011|5[0-9]{2})[0-9]{12}"
            r")\b"
        ),
        None,
    ),
]

# ---------------------------------------------------------------------------
# Credential screens (output boundary)
# ---------------------------------------------------------------------------

CREDENTIAL_SCREENS: List[Screen] = [
    (
        "credential_assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|password|passwd|access[_-]?token|access[_-]?key|private[_-]?key|token)"
            r"\s*[:=]\s*\S+"
        ),
        None,
    ),
    ("bearer_token", re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{20,}"), None),
    (
        "jwt",
        re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),
        None,
    ),
    ("prefixed_api_key", re.compile(r"\b(?:sk|pk|ak)-[A-Za-z0-9]{16,}", re.IGNORECASE), None),
    ("aws_key", re.compile(r"\b(?:AKIA|AIPA|AIDA|AROA|ASIA)[A-Z0-9]{16}\b"), None),
]

# ---------------------------------------------------------------------------
# Inert bounds for structured caller fields
# ---------------------------------------------------------------------------

# Caller channel label: inert lowercase identifier, bounded length.
CHANNEL_RE = re.compile(r"^[a-z0-9_]{1,32}$")

# Topic-restriction selector: the caller may narrow a question to one topic
# area. The value must be one of these identifiers exactly — the mapping
# target is the internal knowledge-base topic key. Shared by the input gate
# and the retrieval node so the two checks cannot drift apart.
TOPIC_FILTERS: Dict[str, str] = {
    "cross_border_transfer": "appi_2026_cross_border_transfer",
    "automated_decisions": "appi_2026_automated_decisions",
    "algorithm_disclosure": "appi_2026_algorithm_disclosure",
    "vendor_contracts": "appi_2026_vendor_contracts",
}

# Bounds for the per-request article count (top_k).
TOP_K_MIN = 1
TOP_K_MAX = 6

_WS_RE = re.compile(r"\s+")


def parse_bounded_int(value: object, lo: int, hi: int) -> Optional[int]:
    """Fail-closed integer parser for caller-controlled numeric fields.

    Accepts only a real int within [lo, hi]. Everything else returns None:
    bool (a bool IS an int in Python — rejected explicitly), float (which is
    also how JSON NaN / Infinity arrive, and how a NaN would otherwise slide
    through a numeric comparison as silently-False), strings, and
    out-of-range magnitudes.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < lo or value > hi:
        return None
    return value


def _first_violation(text: str, screens: List[Screen]) -> Optional[str]:
    """Return the NAME of the first screen that fires, never the match."""
    for name, pattern, validator in screens:
        for match in pattern.finditer(text):
            if validator is None or validator(match.group(0)):
                return name
    return None


def scan_injection(text: str) -> Optional[str]:
    """Screen name if an instruction-override directive is present, else None.

    The text is screened both as received and after whitespace collapse —
    the only transform this pipeline applies to caller text — so a directive
    spread over unusual whitespace cannot re-assemble downstream unseen.
    """
    hit = _first_violation(text, INJECTION_SCREENS)
    if hit is not None:
        return hit
    collapsed = _WS_RE.sub(" ", text)
    if collapsed != text:
        return _first_violation(collapsed, INJECTION_SCREENS)
    return None


def scan_pii(text: str) -> Optional[str]:
    """Category name if personal data is present, else None."""
    return _first_violation(text, PII_SCREENS)


def scan_credentials(text: str) -> Optional[str]:
    """Screen name if credential material is present, else None."""
    return _first_violation(text, CREDENTIAL_SCREENS)


def scan_context_structure(value: object, depth: int = 0) -> Optional[str]:
    """Depth-first injection scan over a parsed caller structure.

    Walks mappings and sequences INCLUDING mapping keys — a hostile field
    name is caller text like any other, and JSON \\u-escapes are already
    decoded by the time the structure reaches this scan, so an escaped
    payload cannot slip past it. Returns the first screen name found, or
    None. Structures nested deeper than 8 levels are refused outright.
    """
    if depth > 8:
        return "context_too_deep"
    if isinstance(value, str):
        return scan_injection(value)
    if isinstance(value, dict):
        for key, item in value.items():
            hit = scan_context_structure(key, depth + 1)
            if hit is not None:
                return hit
            hit = scan_context_structure(item, depth + 1)
            if hit is not None:
                return hit
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            hit = scan_context_structure(item, depth + 1)
            if hit is not None:
                return hit
    return None

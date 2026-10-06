# FIN-C2-145 — PB-6: Backbone Invoke-Order Verification
#
# Verifies via a FULL Graph().invoke() with an external VERIFIED_EXTERNAL caller
# that the backbone executes in the correct order:
#
#   [InitializeNode, PreProcessNode, AppiComplianceGraphNode,
#    PostProcessNode, FinalizeNode]
#
# Also verifies that a VERIFIED_EXTERNAL caller succeeds through the
# PreProcessNode trust gate.
#
# ⚠️ CRITICAL: the caller MUST use TrustLevel.VERIFIED_EXTERNAL (not
#    for_internal()). An INTERNAL-context invoke masks the inner-node
#    trust-trap: INTERNAL(2) >= any inner ANONYMOUS(0), so the gate always
#    passes, hiding the real VERIFIED_EXTERNAL(1) path that a production
#    caller exercises.
#
# ⚠️ IMPORTANT: the SUCCESS path is required to verify the FULL backbone order.
#    A non-SUCCESS (e.g. ERROR) status short-circuits main→post_process and
#    skips the post_process slot, so node_history would be truncated.
#
# Template-specific slots filled below:
#   _MAIN_SLOT_NODE  = "AppiComplianceGraphNode"  (the `main` slot class name)
#   _VALID_PAYLOAD   = a SUCCESS-yielding APPI compliance query

import importlib

import pytest

# ── Template-specific configuration ──────────────────────────────────────────

# The class name of the node assigned to the `main` slot in graph.py.
# For FIN-C2-145 this is the GraphNode subclass wrapping DomainWorkflowGraph.
_MAIN_SLOT_NODE = "AppiComplianceGraphNode"

# A SUCCESS-yielding compliance question input.
# Must pass the PreProcessNode input gate (no personal data, no
# instruction-override content, length ≤ 2000 chars).
_VALID_PAYLOAD = "APPI 2026のアルゴリズム開示義務について教えてください"

# ── Helpers ───────────────────────────────────────────────────────────────────


def _try_import(path):
    """Import a module, skip the test if the SDK wheel is not installed."""
    try:
        return importlib.import_module(path)
    except ImportError as exc:
        pytest.skip(f"SDK wheel not installed — {path}: {exc}")


# ── PB-6: Full backbone invoke-order test ─────────────────────────────────────


class TestPB6BackboneInvokeOrder:
    """PB-6: Graph().invoke() must run backbone nodes in the correct order.

    Verifies the 5-node backbone execution order with a VERIFIED_EXTERNAL
    external-caller context, asserting node_history sequence and SUCCESS status.
    """

    def test_backbone_invoke_order_verified_external(self):
        """PB-6: VERIFIED_EXTERNAL invoke → SUCCESS + correct backbone order."""
        graph_mod = _try_import("src.graph.graph")
        fw_ctx = _try_import("framework.schemas.invocation_context")
        fw_tl = _try_import("framework.schemas.trust_level")
        fw_status = _try_import("framework.schemas.agent_status")

        InvocationContext = fw_ctx.InvocationContext
        TrustLevel = fw_tl.TrustLevel
        AgentStatus = fw_status.AgentStatus

        # Instantiate the outer graph with a VERIFIED_EXTERNAL caller.
        agent = graph_mod.AppiComplianceQAAgent()
        agent.compile()

        ctx = InvocationContext(
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
            caller_id="pb6-test",
            session_id="pb6-session-001",
        )

        result = agent.invoke(_VALID_PAYLOAD, ctx=ctx)

        # A VERIFIED_EXTERNAL invoke must succeed through the trust gate
        assert result.get("status") == AgentStatus.SUCCESS.value, (
            f"PB-6: VERIFIED_EXTERNAL invoke must return SUCCESS; "
            f"got status={result.get('status')!r} output={result.get('output', '')[:200]!r}"
        )

        # PB-6: Verify node_history order
        node_history = result.get("node_history", [])
        assert len(node_history) >= 5, f"PB-6: node_history must have ≥5 entries (5-node backbone); got {node_history}"

        # The backbone order must be:
        #   InitializeNode → PreProcessNode → AppiComplianceGraphNode
        #   → PostProcessNode → FinalizeNode
        expected_order = [
            "InitializeNode",
            "PreProcessNode",
            _MAIN_SLOT_NODE,
            "PostProcessNode",
            "FinalizeNode",
        ]

        # Match by checking that expected class names appear in node_history in order
        # (node_history entries may be full class names or short names)
        history_str = " ".join(str(n) for n in node_history)

        for node_name in expected_order:
            assert node_name in history_str, (
                f"PB-6: '{node_name}' not found in node_history.\n" f"node_history: {node_history}"
            )

        # Verify sequential order
        positions = {}
        for node_name in expected_order:
            for i, entry in enumerate(node_history):
                if node_name in str(entry):
                    positions[node_name] = i
                    break

        for i in range(len(expected_order) - 1):
            a, b = expected_order[i], expected_order[i + 1]
            if a in positions and b in positions:
                assert positions[a] < positions[b], (
                    f"PB-6: backbone order violation: '{a}' (pos {positions[a]}) "
                    f"must come before '{b}' (pos {positions[b]}); "
                    f"full node_history: {node_history}"
                )

    def test_output_is_string(self):
        """PB-6: result['output'] must be a non-empty string on SUCCESS path."""
        graph_mod = _try_import("src.graph.graph")
        fw_ctx = _try_import("framework.schemas.invocation_context")
        fw_tl = _try_import("framework.schemas.trust_level")
        fw_status = _try_import("framework.schemas.agent_status")

        InvocationContext = fw_ctx.InvocationContext
        TrustLevel = fw_tl.TrustLevel
        AgentStatus = fw_status.AgentStatus

        agent = graph_mod.AppiComplianceQAAgent()
        agent.compile()

        ctx = InvocationContext(
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
            caller_id="pb6-test-output",
        )

        result = agent.invoke(_VALID_PAYLOAD, ctx=ctx)

        if result.get("status") == AgentStatus.SUCCESS.value:
            # The invoke surface exposes `output` (not `formatted_output`)
            output = result.get("output")
            assert output is not None, "PB-6: result['output'] must not be None on SUCCESS path"
            assert isinstance(output, str), f"PB-6: result['output'] must be str; got {type(output)}"

    def test_status_key_is_agent_status_value_string(self):
        """PB-6: result['status'] carries the .value string, never the enum."""
        graph_mod = _try_import("src.graph.graph")
        fw_ctx = _try_import("framework.schemas.invocation_context")
        fw_tl = _try_import("framework.schemas.trust_level")
        fw_status = _try_import("framework.schemas.agent_status")

        InvocationContext = fw_ctx.InvocationContext
        TrustLevel = fw_tl.TrustLevel
        AgentStatus = fw_status.AgentStatus

        agent = graph_mod.AppiComplianceQAAgent()
        agent.compile()

        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        result = agent.invoke(_VALID_PAYLOAD, ctx=ctx)

        status = result.get("status")
        # State carries the .value string ("success" / "error"), never the enum
        assert status in (
            AgentStatus.SUCCESS.value,
            AgentStatus.ERROR.value,
        ), f"PB-6: result['status'] must be an AgentStatus .value string; got {status!r}"


# ── PB: Output-gate boundary ──────────────────────────────────────────────────


class TestPBOutputGateBoundary:
    """The output gate blocks credential leakage at the release boundary."""

    def test_credential_in_result_returns_error_and_clears_output(self):
        """Credential-shaped text in state['result'] → execute() returns ERROR
        with every output-bearing field cleared (the response envelope falls
        back to state['result'] even on error status)."""
        mod = _try_import("src.nodes.post_process_node")
        fw_status = _try_import("framework.schemas.agent_status")
        AgentStatus = fw_status.AgentStatus

        node = mod.PostProcessNode()
        polluted_state = {
            "result": "設定値 sk-abc123defghijklmnopqrstuvwxyz を使用してください",
            "risk_level": "MEDIUM",
        }
        result = node.execute(polluted_state)
        assert result.get("status") == AgentStatus.ERROR.value, (
            "credential in result must be blocked by the output gate "
            f"(expected status=ERROR); got {result.get('status')!r}"
        )
        assert result.get("result") == ""
        assert result.get("formatted_output") == ""

    def test_clean_result_passes_gate(self):
        """A clean composed answer passes the module-level gate function."""
        mod = _try_import("src.nodes.post_process_node")
        composed = "第24条に基づき、クロスボーダー転送には明示的な同意が必要です。" + mod._DISCLAIMER
        mod._security_gate_output(composed, False, "2026年7月6日")

"""AgentCore Platform v1.0"""

# FIN-C2-145 — Outer graph (AgentBaseGraph; Cat 2 two-layer nested architecture)
#
# Architecture (Cat 2):
#
#   Outer backbone (fixed — do NOT override add_edges()):
#     START → initialize → pre_process → main → {route} → post_process → finalize → END
#                                             ↓ (RETRY, max 3)
#                                          pre_process
#
#   `main` slot is a GraphNode subclass (AppiComplianceGraphNode) that delegates
#   the full APPI 2026 compliance Q&A domain workflow to DomainWorkflowGraph
#   (inner BaseGraph: retrieve → rank → generate).
#
#   Domain complexity is fully encapsulated inside the inner graph.
#   The outer backbone is never modified.
#
# Directory layout:
#   src/graph/graph.py                 ← outer graph (this file)
#   src/graph/domain_workflow_graph.py ← inner graph (retrieve → rank → generate)
#
# Class-name contract:
#   graph.py class:   AppiComplianceQAAgent  (this file)
#   config/agent.yaml class: "src.graph.graph.AppiComplianceQAAgent"  ← must match
#   src/api/server.py import: from src.graph.graph import AppiComplianceQAAgent  ← must match
#
# Rules enforced:
#   ✅ AppiComplianceQAAgent inherits AgentBaseGraph
#   ✅ super().register_nodes() called first (fills initialize + finalize)
#   ✅ AppiComplianceGraphNode assigned to self._nodes["main"]
#   ✅ merge_output() returns only changed keys
#   ❌ add_edges() NOT overridden on the outer graph
#   ❌ No platform-internal SDK imports

import json
from pathlib import Path
from typing import Any, ClassVar, Dict

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.trust_level import TrustLevel
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State

# Runtime-parameter file location: src/graph/graph.py -> parents[2] is the
# repo root. config/config.yaml holds the runtime parameters (max_retry,
# default_top_k, enforcement_deadline); config/agent.yaml is the static
# manifest and carries no runtime values.
_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


def _runtime_config() -> Dict[str, Any]:
    """Read the runtime parameters out of config/config.yaml.

    Returns an empty dict — never raises — when the file is absent,
    unreadable, not valid YAML, or not a mapping. Callers treat an empty
    result as "no declared runtime config to forward".
    """
    try:
        import yaml

        loaded = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(loaded) if isinstance(loaded, dict) else {}


class AppiComplianceGraphNode(GraphNode):
    """GraphNode subclass assigned to the `main` slot of AppiComplianceQAAgent.

    Wraps DomainWorkflowGraph (inner Cat 2 BaseGraph: retrieve → rank → generate).
    Called by the AgentBaseGraph backbone after pre_process and before post_process.

    Contracts:
      get_subgraph()    — instantiate DomainWorkflowGraph with the declared runtime config
      extract_input()   — JSON-encode the validated question + per-request fields
      merge_output()    — map sub_result fields into the outer state delta (changed keys only)
      error_strategy    — "propagate": re-raise inner errors as SubgraphError (fail-fast)
    """

    # "propagate": re-raise inner graph exceptions as SubgraphError (default — fail fast).
    error_strategy: ClassVar[str] = "propagate"

    # False: HITL interrupts are handled inside the inner graph only.
    propagate_hitl: ClassVar[bool] = False

    # Trust was verified at PreProcessNode (VERIFIED_EXTERNAL); the main slot
    # runs behind that gate.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def _parent_config(self) -> Dict[str, Any]:
        """Forward the declared runtime parameters to the inner graph.

        The domain-relevant keys of config/config.yaml (default_top_k,
        enforcement_deadline) are handed to DomainWorkflowGraph under
        config["configurable"]. The inner graph republishes them into inner
        state via _extra_initial_state(), which is what makes the declared
        values reachable by the domain nodes instead of dead config text.

        Degrades to {} — never raises — when the file is missing or
        malformed; the domain nodes keep their own literal defaults in that
        case.
        """
        config = _runtime_config()
        if not config:
            return {}
        return {"configurable": config}

    def get_subgraph(self) -> Any:
        """Instantiate and return the inner DomainWorkflowGraph.

        Imported lazily to avoid circular imports at module load time.
        The declared runtime config travels through the BaseGraph
        constructor; the domain NODES still take no constructor arguments.
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(config=self._parent_config())

    def extract_input(self, state: AgentState) -> str:
        """JSON-encode the validated question + per-request fields for the inner graph.

        The framework does not forward the outer input_context into the
        inner graph, so per-request values validated by PreProcessNode
        travel in this envelope instead. Only fields the caller actually
        supplied are included — omitted fields stay unset so the declared
        values seeded from config/config.yaml by _extra_initial_state()
        apply as defaults.

        Prefers validated_input (set by the PreProcessNode gate); falls back
        to user_input for unit-test contexts where pre_process has not run.
        """
        query = state.get("validated_input") or state.get("user_input") or ""
        envelope: Dict[str, Any] = {"query": query}
        top_k = state.get("top_k")
        if top_k is not None:
            envelope["top_k"] = int(top_k)
        topic_filter = state.get("topic_filter")
        if topic_filter is not None:
            envelope["topic_filter"] = str(topic_filter)
        return json.dumps(envelope, ensure_ascii=False)

    def merge_output(self, state: AgentState, sub_result: Dict[str, Any]) -> Dict[str, Any]:
        """Map inner graph sub_result back into the outer state delta.

        sub_result is the dict returned by DomainWorkflowGraph.get_output().
        Returns ONLY the keys this node changes — never the full state.

        Key coupling (designed together with DomainWorkflowGraph.get_output()):
          Inner get_output() emits  → "compliance_answer", "cited_articles",
                                      "risk_level", "enforcement_deadline_warning",
                                      "enforcement_deadline"
          This merge_output() reads → sub_result.get(...)
          PostProcessNode reads     → state["result"], state["risk_level"],
                                      state["enforcement_deadline_warning"],
                                      state["enforcement_deadline"]
        """
        return {
            "result": sub_result.get("compliance_answer"),
            "cited_articles": sub_result.get("cited_articles", []),
            "risk_level": sub_result.get("risk_level", "MEDIUM"),
            "enforcement_deadline_warning": sub_result.get("enforcement_deadline_warning", False),
            "enforcement_deadline": sub_result.get("enforcement_deadline"),
        }


class AppiComplianceQAAgent(AgentBaseGraph):
    """Outer graph for FIN-C2-145 APPI 2026 Compliance Q&A (Cat 2).

    Inherits AgentBaseGraph directly. Domain logic is fully encapsulated in
    AppiComplianceGraphNode (main slot), which delegates to
    DomainWorkflowGraph (inner BaseGraph: retrieve → rank → generate).

    Backbone (fixed):
        START → initialize → pre_process → main → post_process → finalize → END

    register_nodes() is the ONLY override:
      - super().register_nodes() fills: initialize, finalize (framework defaults)
      - pre_process: PreProcessNode (input validation gate)
      - main:        AppiComplianceGraphNode (delegates to DomainWorkflowGraph)
      - post_process: PostProcessNode (output gate + disclaimer injection)

    add_edges() is NOT overridden — backbone wiring belongs to the framework.
    """

    @property
    def name(self) -> str:
        """Agent identifier registered with AgentRegistry.

        Must match config/agent.yaml `name:` and the class path in `class:`.
        """
        return "AppiComplianceQAAgent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        """Fill all 5 backbone slots.

        super().register_nodes() MUST be called first — it injects the
        framework's default InitializeNode (sets schema_version, session_id,
        trust_level) and FinalizeNode (builds response_metadata, total_time_ms).
        """
        super().register_nodes()  # fills: initialize, finalize

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = AppiComplianceGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.


# Module-level alias so callers can import `from src.graph.graph import Graph`.
Graph = AppiComplianceQAAgent

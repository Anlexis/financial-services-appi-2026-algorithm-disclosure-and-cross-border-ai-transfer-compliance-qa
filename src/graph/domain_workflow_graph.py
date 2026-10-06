"""AgentCore Platform v1.0"""

# FIN-C2-145 — DomainWorkflowGraph (inner BaseGraph)
#
# This is the INNER graph for the Cat 2 two-layer nested architecture.
# Instantiated by AppiComplianceGraphNode.get_subgraph() (graph.py).
#
# Domain pipeline (linear):
#   START → retrieve → rank → generate → END
#
# get_output() shapes the sub_result dict consumed by merge_output() there.
#
# Rules enforced:
#   ✅ Inherits BaseGraph (fully custom topology — no forced backbone)
#   ✅ Implements all 7 BaseGraph ABC methods
#   ✅ register_nodes() does NOT call super() (abstract in BaseGraph)
#   ✅ Does NOT register initialize / finalize (outer backbone concerns)
#   ✅ get_output() designed together with AppiComplianceGraphNode.merge_output()
#   ✅ Each node's execute() returns only changed state keys
#   ✅ No platform-internal SDK imports
#   ✅ Inner nodes instantiated with NO ctor args (framework nodes have no __init__ args)

from typing import Any, Dict

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.nodes.generate_node import GenerateNode
from src.nodes.rank_node import RankNode
from src.nodes.retrieve_node import RetrieveNode
from src.schemas.state import State


class DomainWorkflowGraph(BaseGraph):
    """Inner domain workflow graph for FIN-C2-145 APPI 2026 Compliance Q&A.

    Inherits BaseGraph directly for a fully custom node topology.
    Called by AppiComplianceGraphNode.get_subgraph() in graph.py.

    Pipeline (linear):
        START
          → retrieve   (RetrieveNode — APPI 2026 KB retrieval)
          → rank       (RankNode — chunk reranking + top-K selection)
          → generate   (GenerateNode — compliance answer generation)
          → END

    All nodes are FunctionNode subclasses with required_trust_level = ANONYMOUS.
    initialize / finalize are outer backbone concerns — not registered here.

    Error strategy:
        Node errors surface via AgentStatus.ERROR.value in state["status"].
        Unhandled exceptions bubble up to AppiComplianceGraphNode.error_strategy
        ("propagate") in the outer graph.
    """

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        """Unique identifier for this inner graph."""
        return "fin_c2_145_appi_compliance_workflow"

    @property
    def state_schema(self) -> type:
        """TypedDict subclass shared across inner and outer graph."""
        return State

    # ── Config validation ─────────────────────────────────────────────────────

    def _validate_config(self) -> None:
        """Validate inner graph config before compilation.

        No mandatory config for this inner graph — AppiComplianceGraphNode
        forwards the declared runtime parameters (config/config.yaml) under
        config["configurable"], but every domain node keeps a literal
        fallback, so an empty config is not an error.
        """
        pass

    # ── Declared runtime config -> inner state ────────────────────────────────

    def _extra_initial_state(self) -> Dict[str, Any]:
        """Republish the forwarded runtime config into the inner initial state.

        AppiComplianceGraphNode._parent_config() puts the declared runtime
        parameters (config/config.yaml) under config["configurable"]; this
        hook seeds the domain-relevant keys (default_top_k,
        enforcement_deadline) into inner state so RankNode and GenerateNode
        can fall back to the declared values. Per-request values still win:
        they travel in the JSON envelope built by extract_input() and
        RetrieveNode applies them over these defaults.

        Only keys actually declared in config/config.yaml are seeded, so a
        missing or malformed file leaves the nodes on their own literal
        defaults.
        """
        configurable = (self.config or {}).get("configurable") or {}
        seed: Dict[str, Any] = {}
        if "default_top_k" in configurable:
            try:
                seed["default_top_k"] = int(configurable["default_top_k"])
            except (TypeError, ValueError):
                pass
        if "enforcement_deadline" in configurable:
            seed["enforcement_deadline"] = str(configurable["enforcement_deadline"])
        return seed

    # ── Node registration ─────────────────────────────────────────────────────

    def register_nodes(self) -> None:
        """Register all 3 domain nodes.

        No super() call — BaseGraph.register_nodes() is abstract.
        Do NOT register initialize or finalize; those are outer backbone
        concerns handled by AgentBaseGraph in graph.py.
        Every node is instantiated with NO constructor arguments; declared
        config reaches them through inner state (_extra_initial_state),
        never through ctor args.
        Every key registered here is referenced in add_edges().
        """
        self._nodes["retrieve"] = RetrieveNode()
        self._nodes["rank"] = RankNode()
        self._nodes["generate"] = GenerateNode()

    # ── Edge wiring ───────────────────────────────────────────────────────────

    def add_edges(self) -> None:
        """Wire the linear APPI 2026 compliance domain topology.

        Linear: retrieve → rank → generate → END.
        No conditional branching between domain nodes — GenerateNode handles
        empty-chunks gracefully (returns a "no articles found" answer).
        route() is implemented as required by the ABC but not used here.
        """
        self._sg.add_edge(START, "retrieve")
        self._sg.add_edge("retrieve", "rank")
        self._sg.add_edge("rank", "generate")
        self._sg.add_edge("generate", END)

    # ── Routing ───────────────────────────────────────────────────────────────

    def route(self, state: State) -> str:
        """Conditional routing — required by the BaseGraph ABC.

        Linear-topology invariant: add_conditional_edges() is never used
        so route() is never called at runtime. Implemented to satisfy the
        ABC; annotated with this graph's own State schema (the annotation is
        the input schema a path callable would receive). Both branches
        return END to prevent any re-entry risk.
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        return END

    # ── Output shape ──────────────────────────────────────────────────────────

    def get_output(self, state: AgentState) -> Dict[str, Any]:
        """Shape the output dict returned to the outer graph as sub_result.

        Received by AppiComplianceGraphNode.merge_output() in graph.py
        as the sub_result argument. Both methods are designed together:

            Inner get_output()   emits: "compliance_answer", "cited_articles",
                                        "risk_level", "enforcement_deadline_warning",
                                        "enforcement_deadline", "status", ...
            Outer merge_output() reads: sub_result.get("compliance_answer"),
                                        sub_result.get("cited_articles"),
                                        sub_result.get("risk_level"),
                                        sub_result.get("enforcement_deadline_warning"),
                                        sub_result.get("enforcement_deadline")

        PostProcessNode reads state["result"] (mapped from compliance_answer),
        state["risk_level"], state["enforcement_deadline_warning"], and
        state["enforcement_deadline"].
        """
        return {
            "compliance_answer": state.get("compliance_answer"),
            "cited_articles": state.get("cited_articles", []),
            "risk_level": state.get("risk_level", "MEDIUM"),
            "enforcement_deadline_warning": state.get("enforcement_deadline_warning", False),
            "enforcement_deadline": state.get("enforcement_deadline"),
            "status": state.get("status"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }

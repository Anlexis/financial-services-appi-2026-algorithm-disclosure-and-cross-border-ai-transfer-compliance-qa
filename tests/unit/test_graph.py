# FIN-C2-145 — Unit Tests: outer graph (AppiComplianceQAAgent /
# AppiComplianceGraphNode) and inner DomainWorkflowGraph

import importlib
import json

import pytest


def _load(mod_path):
    try:
        return importlib.import_module(mod_path)
    except ImportError as exc:  # pragma: no cover - environment guard
        pytest.skip(f"framework/shared not installed (SDK wheel required): {exc}")


class TestAppiComplianceQAAgentInstantiation:
    def test_agent_instantiates(self):
        mod = _load("src.graph.graph")
        agent = mod.AppiComplianceQAAgent()
        assert agent is not None

    def test_graph_alias_is_agent(self):
        mod = _load("src.graph.graph")
        assert mod.Graph is mod.AppiComplianceQAAgent

    def test_agent_name_property(self):
        mod = _load("src.graph.graph")
        assert mod.AppiComplianceQAAgent().name == "AppiComplianceQAAgent"

    def test_state_schema_is_state(self):
        mod = _load("src.graph.graph")
        state_mod = _load("src.schemas.state")
        assert mod.AppiComplianceQAAgent().state_schema is state_mod.State

    def test_agent_accepts_runtime_config(self):
        mod = _load("src.graph.graph")
        agent = mod.AppiComplianceQAAgent(config={"max_retry": 2})
        assert agent.config.get("max_retry") == 2


class TestAppiComplianceGraphNodeMethods:
    def test_extract_input_builds_json_envelope(self):
        mod = _load("src.graph.graph")
        node = mod.AppiComplianceGraphNode()
        envelope = node.extract_input(
            {"validated_input": "越境転送の義務", "top_k": 2, "topic_filter": "cross_border_transfer"}
        )
        parsed = json.loads(envelope)
        assert parsed == {
            "query": "越境転送の義務",
            "top_k": 2,
            "topic_filter": "cross_border_transfer",
        }

    def test_extract_input_omits_absent_fields(self):
        """Omitted per-request fields stay out of the envelope so declared
        config defaults apply downstream."""
        mod = _load("src.graph.graph")
        node = mod.AppiComplianceGraphNode()
        parsed = json.loads(node.extract_input({"validated_input": "質問"}))
        assert parsed == {"query": "質問"}

    def test_extract_input_falls_back_to_user_input(self):
        mod = _load("src.graph.graph")
        node = mod.AppiComplianceGraphNode()
        parsed = json.loads(node.extract_input({"user_input": "raw question"}))
        assert parsed["query"] == "raw question"

    def test_extract_input_empty_state(self):
        mod = _load("src.graph.graph")
        node = mod.AppiComplianceGraphNode()
        parsed = json.loads(node.extract_input({}))
        assert parsed["query"] == ""

    def test_merge_output_maps_all_coupled_fields(self):
        mod = _load("src.graph.graph")
        node = mod.AppiComplianceGraphNode()
        sub_result = {
            "compliance_answer": "回答本文",
            "cited_articles": ["第24条"],
            "risk_level": "HIGH",
            "enforcement_deadline_warning": True,
            "enforcement_deadline": "2026-07-06",
            "status": "success",
            "trace_id": "t-1",
        }
        delta = node.merge_output({}, sub_result)
        assert delta == {
            "result": "回答本文",
            "cited_articles": ["第24条"],
            "risk_level": "HIGH",
            "enforcement_deadline_warning": True,
            "enforcement_deadline": "2026-07-06",
        }

    def test_merge_output_defaults_on_missing_fields(self):
        mod = _load("src.graph.graph")
        node = mod.AppiComplianceGraphNode()
        delta = node.merge_output({}, {})
        assert delta["result"] is None
        assert delta["cited_articles"] == []
        assert delta["risk_level"] == "MEDIUM"
        assert delta["enforcement_deadline_warning"] is False

    def test_get_subgraph_returns_domain_workflow_graph(self):
        mod = _load("src.graph.graph")
        inner_mod = _load("src.graph.domain_workflow_graph")
        node = mod.AppiComplianceGraphNode()
        assert isinstance(node.get_subgraph(), inner_mod.DomainWorkflowGraph)

    def test_parent_config_forwards_declared_values(self, tmp_path, monkeypatch):
        mod = _load("src.graph.graph")
        substitute = tmp_path / "config.yaml"
        substitute.write_text('default_top_k: 2\nenforcement_deadline: "2027-01-15"\n', encoding="utf-8")
        monkeypatch.setattr(mod, "_RUNTIME_CONFIG_PATH", substitute)
        forwarded = mod.AppiComplianceGraphNode()._parent_config()
        assert forwarded == {"configurable": {"default_top_k": 2, "enforcement_deadline": "2027-01-15"}}

    def test_parent_config_degrades_to_empty(self, tmp_path, monkeypatch):
        mod = _load("src.graph.graph")
        monkeypatch.setattr(mod, "_RUNTIME_CONFIG_PATH", tmp_path / "absent.yaml")
        assert mod.AppiComplianceGraphNode()._parent_config() == {}


class TestDomainWorkflowGraph:
    def test_graph_instantiates(self):
        mod = _load("src.graph.domain_workflow_graph")
        assert mod.DomainWorkflowGraph() is not None

    def test_graph_name(self):
        mod = _load("src.graph.domain_workflow_graph")
        assert mod.DomainWorkflowGraph().name == "fin_c2_145_appi_compliance_workflow"

    def test_graph_state_schema(self):
        mod = _load("src.graph.domain_workflow_graph")
        state_mod = _load("src.schemas.state")
        assert mod.DomainWorkflowGraph().state_schema is state_mod.State

    def test_extra_initial_state_seeds_declared_values(self):
        mod = _load("src.graph.domain_workflow_graph")
        graph = mod.DomainWorkflowGraph(
            config={"configurable": {"default_top_k": 3, "enforcement_deadline": "2027-01-15"}}
        )
        seed = graph._extra_initial_state()
        assert seed == {"default_top_k": 3, "enforcement_deadline": "2027-01-15"}

    def test_extra_initial_state_empty_without_config(self):
        mod = _load("src.graph.domain_workflow_graph")
        assert mod.DomainWorkflowGraph()._extra_initial_state() == {}

    def test_extra_initial_state_skips_malformed_top_k(self):
        mod = _load("src.graph.domain_workflow_graph")
        graph = mod.DomainWorkflowGraph(config={"configurable": {"default_top_k": "many"}})
        assert graph._extra_initial_state() == {}

    def test_route_annotated_with_own_state(self):
        """The path-callable annotation is the input schema the graph runtime
        projects state to — it must be this graph's own State, never the
        base AgentState."""
        import typing

        mod = _load("src.graph.domain_workflow_graph")
        state_mod = _load("src.schemas.state")
        hints = typing.get_type_hints(mod.DomainWorkflowGraph.route)
        assert hints["state"] is state_mod.State

    def test_get_output_shape(self):
        mod = _load("src.graph.domain_workflow_graph")
        graph = mod.DomainWorkflowGraph()
        state = {
            "compliance_answer": "回答",
            "cited_articles": ["第24条"],
            "risk_level": "HIGH",
            "enforcement_deadline_warning": True,
            "enforcement_deadline": "2026-07-06",
            "status": "success",
        }
        out = graph.get_output(state)
        assert out["compliance_answer"] == "回答"
        assert out["cited_articles"] == ["第24条"]
        assert out["risk_level"] == "HIGH"
        assert out["enforcement_deadline_warning"] is True
        assert out["enforcement_deadline"] == "2026-07-06"

    def test_get_output_feeds_merge_output_end_to_end(self):
        """Inner get_output() and outer merge_output() are designed together —
        every key merge_output reads is emitted by get_output."""
        graph_mod = _load("src.graph.graph")
        inner_mod = _load("src.graph.domain_workflow_graph")
        inner_state = {
            "compliance_answer": "回答本文",
            "cited_articles": ["第18条"],
            "risk_level": "MEDIUM",
            "enforcement_deadline_warning": False,
            "enforcement_deadline": "2026-07-06",
            "status": "success",
        }
        sub_result = inner_mod.DomainWorkflowGraph().get_output(inner_state)
        delta = graph_mod.AppiComplianceGraphNode().merge_output({}, sub_result)
        assert delta["result"] == "回答本文"
        assert delta["cited_articles"] == ["第18条"]
        assert delta["risk_level"] == "MEDIUM"
        assert delta["enforcement_deadline"] == "2026-07-06"

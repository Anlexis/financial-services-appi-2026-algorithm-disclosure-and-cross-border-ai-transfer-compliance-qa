# FIN-C2-145 — Unit Tests: Inner Domain Nodes (RetrieveNode, RankNode,
# GenerateNode) and PostProcessNode (output gate)

import importlib
import json

import pytest


def _load(mod_path):
    try:
        return importlib.import_module(mod_path)
    except ImportError as exc:  # pragma: no cover - environment guard
        pytest.skip(f"framework/shared not installed (SDK wheel required): {exc}")


def _status():
    from framework.schemas.agent_status import AgentStatus

    return AgentStatus


# ---------------------------------------------------------------------------
# RetrieveNode
# ---------------------------------------------------------------------------


class TestRetrieveNode:
    """RetrieveNode: envelope parsing + deterministic KB retrieval."""

    def test_valid_query_returns_success(self):
        mod = _load("src.nodes.retrieve_node")
        result = mod.RetrieveNode().execute({"validated_input": "クロスボーダー転送の義務"})
        assert result.get("status") == _status().SUCCESS.value
        assert result.get("retrieved_chunks")

    def test_cross_border_keyword_retrieves_article_24(self):
        mod = _load("src.nodes.retrieve_node")
        result = mod.RetrieveNode().execute({"validated_input": "cross-border transfer rules"})
        top_ids = [c["article_id"] for c in result["retrieved_chunks"][:2]]
        assert "第24条" in top_ids

    def test_algorithm_disclosure_keyword_retrieves_relevant(self):
        mod = _load("src.nodes.retrieve_node")
        result = mod.RetrieveNode().execute({"validated_input": "algorithm disclosure requirements"})
        top = result["retrieved_chunks"][0]
        assert top["relevance_score"] > 0

    def test_empty_query_returns_error(self):
        mod = _load("src.nodes.retrieve_node")
        result = mod.RetrieveNode().execute({"validated_input": ""})
        assert result.get("status") == _status().ERROR.value

    def test_json_envelope_parsed(self):
        """The outer graph's JSON envelope is deserialized: query drives
        retrieval, top_k is forwarded, and the clean query is republished."""
        mod = _load("src.nodes.retrieve_node")
        envelope = json.dumps(
            {"query": "クロスボーダー転送について", "top_k": 2, "topic_filter": "cross_border_transfer"},
            ensure_ascii=False,
        )
        result = mod.RetrieveNode().execute({"user_input": envelope})
        assert result.get("status") == _status().SUCCESS.value
        assert result.get("top_k") == 2
        assert result.get("validated_input") == "クロスボーダー転送について"
        topics = {c["topic"] for c in result["retrieved_chunks"]}
        assert topics == {"appi_2026_cross_border_transfer"}

    def test_topic_filter_restricts_corpus(self):
        mod = _load("src.nodes.retrieve_node")
        envelope = json.dumps({"query": "契約 監査", "topic_filter": "vendor_contracts"}, ensure_ascii=False)
        result = mod.RetrieveNode().execute({"user_input": envelope})
        topics = {c["topic"] for c in result["retrieved_chunks"]}
        assert topics <= {"appi_2026_vendor_contracts"}

    def test_plain_string_still_works(self):
        """A non-JSON input stays a plain query (direct inner invocation)."""
        mod = _load("src.nodes.retrieve_node")
        result = mod.RetrieveNode().execute({"user_input": "ベンダー契約の監督義務"})
        assert result.get("status") == _status().SUCCESS.value
        assert result.get("validated_input") == "ベンダー契約の監督義務"

    def test_envelope_with_empty_query_errors(self):
        mod = _load("src.nodes.retrieve_node")
        result = mod.RetrieveNode().execute({"user_input": json.dumps({"query": ""})})
        assert result.get("status") == _status().ERROR.value

    def test_output_chunks_json_serializable(self):
        mod = _load("src.nodes.retrieve_node")
        result = mod.RetrieveNode().execute({"validated_input": "アルゴリズム開示"})
        json.dumps(result["retrieved_chunks"], ensure_ascii=False)

    def test_audit_emitted_on_success(self, monkeypatch):
        mod = _load("src.nodes.retrieve_node")
        calls = []
        monkeypatch.setattr(mod, "emit_trace_event", lambda ev, pl, st: calls.append(ev))
        mod.RetrieveNode().execute({"validated_input": "クロスボーダー転送"})
        assert "retrieve_complete" in calls

    def test_required_trust_level_anonymous(self):
        mod = _load("src.nodes.retrieve_node")
        from framework.schemas.trust_level import TrustLevel

        assert mod.RetrieveNode.required_trust_level == TrustLevel.ANONYMOUS


# ---------------------------------------------------------------------------
# RankNode
# ---------------------------------------------------------------------------


def _chunks(scores):
    return [
        {"article_id": f"a{i}", "topic": "appi_2026_vendor_contracts", "text": "t", "relevance_score": s}
        for i, s in enumerate(scores)
    ]


class TestRankNode:
    def test_non_empty_chunks_returns_success(self):
        mod = _load("src.nodes.rank_node")
        result = mod.RankNode().execute({"retrieved_chunks": _chunks([1.0, 2.0])})
        assert result.get("status") == _status().SUCCESS.value
        assert len(result["ranked_chunks"]) == 2

    def test_ranking_is_descending_by_score(self):
        mod = _load("src.nodes.rank_node")
        result = mod.RankNode().execute({"retrieved_chunks": _chunks([0.5, 3.0, 1.5])})
        scores = [c["relevance_score"] for c in result["ranked_chunks"]]
        assert scores == sorted(scores, reverse=True)

    def test_default_truncation_without_config(self):
        mod = _load("src.nodes.rank_node")
        result = mod.RankNode().execute({"retrieved_chunks": _chunks([float(i + 1) for i in range(10)])})
        assert len(result["ranked_chunks"]) == 5

    def test_zero_relevance_chunks_excluded(self):
        """Zero-score chunks share nothing with the question — they must not
        pad the answer nor drive the risk classification."""
        mod = _load("src.nodes.rank_node")
        result = mod.RankNode().execute({"retrieved_chunks": _chunks([0.0, 2.0, 0.0])})
        assert [c["relevance_score"] for c in result["ranked_chunks"]] == [2.0]

    def test_caller_top_k_wins_over_declared_default(self):
        mod = _load("src.nodes.rank_node")
        state = {
            "retrieved_chunks": _chunks([5.0, 4.0, 3.0, 2.0, 1.0]),
            "top_k": 2,
            "default_top_k": 4,
        }
        result = mod.RankNode().execute(state)
        assert len(result["ranked_chunks"]) == 2

    def test_declared_default_wins_over_literal(self):
        mod = _load("src.nodes.rank_node")
        state = {
            "retrieved_chunks": _chunks([5.0, 4.0, 3.0, 2.0, 1.0]),
            "default_top_k": 3,
        }
        result = mod.RankNode().execute(state)
        assert len(result["ranked_chunks"]) == 3

    def test_invalid_state_top_k_falls_back(self):
        """A malformed count in state falls back to the declared/literal
        default instead of failing open."""
        mod = _load("src.nodes.rank_node")
        state = {
            "retrieved_chunks": _chunks([5.0, 4.0, 3.0, 2.0, 1.0, 0.5]),
            "top_k": float("nan"),
        }
        result = mod.RankNode().execute(state)
        assert len(result["ranked_chunks"]) == 5

    def test_empty_retrieved_chunks_success_with_empty_ranked(self):
        mod = _load("src.nodes.rank_node")
        result = mod.RankNode().execute({"retrieved_chunks": []})
        assert result.get("status") == _status().SUCCESS.value
        assert result["ranked_chunks"] == []

    def test_audit_emitted(self, monkeypatch):
        mod = _load("src.nodes.rank_node")
        calls = []
        monkeypatch.setattr(mod, "emit_trace_event", lambda ev, pl, st: calls.append(ev))
        mod.RankNode().execute({"retrieved_chunks": _chunks([1.0])})
        assert "rank_complete" in calls

    def test_required_trust_level_anonymous(self):
        mod = _load("src.nodes.rank_node")
        from framework.schemas.trust_level import TrustLevel

        assert mod.RankNode.required_trust_level == TrustLevel.ANONYMOUS


# ---------------------------------------------------------------------------
# GenerateNode
# ---------------------------------------------------------------------------


def _kb_chunk(article_id, topic, score=1.0):
    return {"article_id": article_id, "topic": topic, "text": f"{article_id} の条文テキスト", "relevance_score": score}


class TestGenerateNode:
    def test_ranked_chunks_produce_answer(self):
        mod = _load("src.nodes.generate_node")
        state = {
            "ranked_chunks": [_kb_chunk("第24条", "appi_2026_cross_border_transfer")],
            "validated_input": "越境転送の義務",
        }
        result = mod.GenerateNode().execute(state)
        assert result.get("status") == _status().SUCCESS.value
        assert "第24条" in result["compliance_answer"]

    @pytest.mark.parametrize(
        ("topic", "expected_risk"),
        [
            ("appi_2026_cross_border_transfer", "HIGH"),
            ("appi_2026_automated_decisions", "HIGH"),
            ("appi_2026_algorithm_disclosure", "MEDIUM"),
            ("appi_2026_vendor_contracts", "MEDIUM"),
        ],
    )
    def test_topic_risk_classification(self, topic, expected_risk):
        mod = _load("src.nodes.generate_node")
        result = mod.GenerateNode().execute({"ranked_chunks": [_kb_chunk("a", topic)], "validated_input": "q"})
        assert result["risk_level"] == expected_risk
        assert result["enforcement_deadline_warning"] is (expected_risk == "HIGH")

    def test_no_topics_risk_low(self):
        mod = _load("src.nodes.generate_node")
        result = mod.GenerateNode().execute({"ranked_chunks": [], "validated_input": "q"})
        assert result["risk_level"] == "LOW"
        assert result["enforcement_deadline_warning"] is False

    def test_empty_chunks_returns_fallback_answer(self):
        mod = _load("src.nodes.generate_node")
        result = mod.GenerateNode().execute({"ranked_chunks": [], "validated_input": "q"})
        assert "見つかりませんでした" in result["compliance_answer"]

    def test_cited_articles_extracted_deduplicated(self):
        mod = _load("src.nodes.generate_node")
        state = {
            "ranked_chunks": [
                _kb_chunk("第24条", "appi_2026_cross_border_transfer"),
                _kb_chunk("第24条", "appi_2026_cross_border_transfer"),
                _kb_chunk("第18条", "appi_2026_vendor_contracts"),
            ],
            "validated_input": "q",
        }
        result = mod.GenerateNode().execute(state)
        assert result["cited_articles"] == ["第24条", "第18条"]

    def test_zero_score_chunk_not_cited(self):
        mod = _load("src.nodes.generate_node")
        state = {
            "ranked_chunks": [_kb_chunk("第18条", "appi_2026_vendor_contracts", score=0.0)],
            "validated_input": "q",
        }
        result = mod.GenerateNode().execute(state)
        assert result["cited_articles"] == []

    def test_declared_deadline_rendered_in_checklist(self):
        """The declared enforcement_deadline (seeded from config/config.yaml)
        reaches the rendered answer."""
        mod = _load("src.nodes.generate_node")
        state = {
            "ranked_chunks": [_kb_chunk("第24条", "appi_2026_cross_border_transfer")],
            "validated_input": "q",
            "enforcement_deadline": "2027-01-15",
        }
        result = mod.GenerateNode().execute(state)
        assert "2027年1月15日" in result["compliance_answer"]

    def test_absent_deadline_falls_back_to_literal(self):
        mod = _load("src.nodes.generate_node")
        state = {
            "ranked_chunks": [_kb_chunk("第24条", "appi_2026_cross_border_transfer")],
            "validated_input": "q",
        }
        result = mod.GenerateNode().execute(state)
        assert "2026年7月6日" in result["compliance_answer"]

    def test_malformed_deadline_falls_back_to_literal(self):
        mod = _load("src.nodes.generate_node")
        assert mod.format_deadline_ja("not-a-date") == "2026年7月6日"
        assert mod.format_deadline_ja(None) == "2026年7月6日"
        assert mod.format_deadline_ja("2027-01-15") == "2027年1月15日"

    def test_audit_emitted(self, monkeypatch):
        mod = _load("src.nodes.generate_node")
        calls = []
        monkeypatch.setattr(mod, "emit_trace_event", lambda ev, pl, st: calls.append(ev))
        mod.GenerateNode().execute({"ranked_chunks": [], "validated_input": "q"})
        assert "generate_complete" in calls

    def test_required_trust_level_anonymous(self):
        mod = _load("src.nodes.generate_node")
        from framework.schemas.trust_level import TrustLevel

        assert mod.GenerateNode.required_trust_level == TrustLevel.ANONYMOUS


# ---------------------------------------------------------------------------
# PostProcessNode (output gate)
# ---------------------------------------------------------------------------


class TestPostProcessNode:
    def _valid_state(self, risk_level="MEDIUM", deadline_warning=False, **extra):
        state = {
            "result": "APPI 2026の第24条に基づき、以下の義務があります: ...",
            "risk_level": risk_level,
            "enforcement_deadline_warning": deadline_warning,
        }
        state.update(extra)
        return state

    def test_valid_result_returns_success_with_disclaimer(self):
        mod = _load("src.nodes.post_process_node")
        result = mod.PostProcessNode().execute(self._valid_state())
        assert result.get("status") == _status().SUCCESS.value
        formatted = result.get("formatted_output", "")
        assert "免責事項" in formatted
        assert "legal counsel" in formatted

    def test_high_risk_injects_deadline_warning(self):
        mod = _load("src.nodes.post_process_node")
        result = mod.PostProcessNode().execute(self._valid_state(risk_level="HIGH", deadline_warning=True))
        formatted = result.get("formatted_output", "")
        assert "APPI 2026施行日" in formatted
        assert "2026年7月6日" in formatted

    def test_high_risk_warning_uses_declared_deadline(self):
        mod = _load("src.nodes.post_process_node")
        result = mod.PostProcessNode().execute(
            self._valid_state(risk_level="HIGH", deadline_warning=True, enforcement_deadline="2027-01-15")
        )
        assert "2027年1月15日" in result.get("formatted_output", "")

    def test_medium_risk_no_deadline_warning(self):
        mod = _load("src.nodes.post_process_node")
        result = mod.PostProcessNode().execute(self._valid_state(risk_level="MEDIUM"))
        assert "APPI 2026施行日" not in result.get("formatted_output", "")

    @pytest.mark.parametrize(
        "credential",
        [
            "設定には sk-1234567890abcdefghij を使用します",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.abcdefghijklmnopqrstuvwxyz12345",
            "password=SuperSecret123 の設定について",
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
        ],
    )
    def test_credential_in_result_blocked_and_cleared(self, credential):
        """A violating release returns an error AND clears every
        output-bearing field — the response envelope falls back to
        state["result"] even on error status, so clearing is what actually
        contains the blocked text."""
        mod = _load("src.nodes.post_process_node")
        result = mod.PostProcessNode().execute({"result": credential, "risk_level": "MEDIUM"})
        assert result.get("status") == _status().ERROR.value
        assert result.get("result") == ""
        assert result.get("formatted_output") == ""
        assert result.get("cited_articles") == []
        # Nothing of the blocked text survives anywhere in the delta.
        assert credential not in str(result)

    def test_blocked_release_audited(self, monkeypatch):
        mod = _load("src.nodes.post_process_node")
        calls = []
        monkeypatch.setattr(mod, "emit_trace_event", lambda ev, pl, st: calls.append((ev, pl)))
        mod.PostProcessNode().execute({"result": "password=SuperSecret123", "risk_level": "MEDIUM"})
        assert calls and calls[0][1].get("outcome") == "blocked"

    def test_empty_result_returns_error(self):
        mod = _load("src.nodes.post_process_node")
        result = mod.PostProcessNode().execute({"result": "", "risk_level": "MEDIUM"})
        assert result.get("status") == _status().ERROR.value
        # An empty result is reported, not swallowed: the envelope surfaces formatted_output
        # as `output`, so a refusal that leaves it blank reaches the caller as a hang.
        assert (result.get("formatted_output") or "").startswith("Request could not be completed.")

    def test_audit_emitted_on_success(self, monkeypatch):
        mod = _load("src.nodes.post_process_node")
        calls = []
        monkeypatch.setattr(mod, "emit_trace_event", lambda ev, pl, st: calls.append(ev))
        mod.PostProcessNode().execute(self._valid_state())
        assert "post_process_complete" in calls

    def test_gate_function_raises_on_missing_disclaimer(self):
        """The module-level gate re-verifies the invariant on the final text."""
        mod = _load("src.nodes.post_process_node")
        with pytest.raises(ValueError):
            mod._security_gate_output("本文のみ、免責事項なし", False, "2026年7月6日")

    def test_gate_function_passes_composed_text(self):
        mod = _load("src.nodes.post_process_node")
        composed = "本文" + mod._DISCLAIMER
        mod._security_gate_output(composed, False, "2026年7月6日")

    def test_required_trust_level_anonymous(self):
        mod = _load("src.nodes.post_process_node")
        from framework.schemas.trust_level import TrustLevel

        assert mod.PostProcessNode.required_trust_level == TrustLevel.ANONYMOUS

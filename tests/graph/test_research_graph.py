"""Structural tests for the 2-node research graph (04-04-PLAN.md).

Asserts graph compilation, node membership, and the shared
``AgentGraphState`` schema — no end-to-end invocation here (that is Plan
05's endpoint integration test).
"""

from __future__ import annotations

from app.graph.research_graph import build_research_graph
from app.graph.state import AgentGraphState


def test_build_research_graph_compiles() -> None:
    """build_research_graph() returns a compiled graph without raising."""
    graph = build_research_graph()
    assert hasattr(graph, "ainvoke")


def test_graph_has_both_nodes() -> None:
    """The compiled graph's node registry includes both agent nodes."""
    graph = build_research_graph()
    node_names = set(graph.get_graph().nodes.keys())
    assert "fundamental_analysis" in node_names
    assert "synthesis" in node_names


def test_state_has_required_fields() -> None:
    """AgentGraphState carries every field both nodes read/write."""
    required_fields = {
        "plan_id",
        "ticker",
        "user_id",
        "session",
        "fundamentals_output",
        "fundamentals_status",
        "synthesis_output",
        "synthesis_status",
        "memo_status",
    }
    assert required_fields.issubset(AgentGraphState.__annotations__.keys())

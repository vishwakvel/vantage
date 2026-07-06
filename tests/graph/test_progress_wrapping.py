"""Unit tests for app.graph.research_graph._with_progress — RED phase (D-07).

Covers the single node-wrapping helper that emits a RUNNING event before a
node runs and its terminal status after, based on the node's own return
value — without touching any file under app/agents/.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.anyio
async def test_wrapped_node_emits_running_then_terminal_status() -> None:
    from app.graph.research_graph import _with_progress

    fake_node = AsyncMock(return_value={"fundamentals_status": "PARTIAL"})

    with patch(
        "app.graph.research_graph.publish_agent_status", new=AsyncMock()
    ) as mock_publish:
        wrapped = _with_progress(fake_node, "FundamentalAnalysis", "fundamentals_status")
        result = await wrapped({"memo_id": "m1"})

    assert result == {"fundamentals_status": "PARTIAL"}
    assert mock_publish.await_count == 2
    first_call, second_call = mock_publish.await_args_list
    assert first_call.kwargs == {
        "memo_id": "m1",
        "agent_type": "FundamentalAnalysis",
        "status": "RUNNING",
    } or first_call.args == ("m1", "FundamentalAnalysis", "RUNNING")
    assert second_call.kwargs == {
        "memo_id": "m1",
        "agent_type": "FundamentalAnalysis",
        "status": "PARTIAL",
    } or second_call.args == ("m1", "FundamentalAnalysis", "PARTIAL")


@pytest.mark.anyio
async def test_wrapped_node_without_memo_id_no_ops() -> None:
    """No memo_id in state => zero publish calls, node result returned verbatim."""
    from app.graph.research_graph import _with_progress

    fake_node = AsyncMock(return_value={"fundamentals_status": "SUCCESS"})

    with patch(
        "app.graph.research_graph.publish_agent_status", new=AsyncMock()
    ) as mock_publish:
        wrapped = _with_progress(fake_node, "FundamentalAnalysis", "fundamentals_status")
        result = await wrapped({})

    mock_publish.assert_not_awaited()
    assert result == {"fundamentals_status": "SUCCESS"}


def test_build_research_graph_still_compiles_with_all_six_nodes() -> None:
    from app.graph.research_graph import build_research_graph

    graph = build_research_graph()
    node_names = set(graph.get_graph().nodes.keys())
    assert {
        "fundamental_analysis",
        "sentiment_nlp",
        "risk_assessment",
        "macro_sector",
        "comparable_companies",
        "synthesis",
    }.issubset(node_names)

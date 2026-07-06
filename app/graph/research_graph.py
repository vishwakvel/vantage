"""5-way fan-out + fan-in research graph: 5 specialists -> Synthesis.

Assembles the compiled ``StateGraph`` the ``/run`` endpoint invokes. All 5
specialist agents (FundamentalAnalysis, SentimentNLP, RiskAssessment,
MacroSector, ComparableCompanies) are wired with their own edge out of
``START`` — multiple edges out of ``START`` make LangGraph dispatch every
specialist concurrently in the same super-step (AGENT-05). Each specialist
also has its own edge into Synthesis, so LangGraph's fan-in waits for ALL 5
to finish before invoking Synthesis (AGENT-06) — this is the structural
precondition for a PARTIAL memo whenever any subset of agents fails while
the rest succeed.

Boundary constraint (CI-enforced by ``tests/test_boundaries.py::
test_no_groq_import_in_graph``): this module imports only
``app.agents.*`` node functions, never ``groq``/``AsyncGroq`` directly.
(``app.services.progress_publisher`` is a plain service import, not a Groq
client — it does not violate this boundary.)

Routing is plain, declarative ``add_edge`` wiring only — every run
dispatches the same fixed 5-way fan-out with no runtime branching or
LLM-driven decision of which agents to run (PROJECT.md: declarative
routing, not LLM-driven; D-08: all 5 specialists dispatch every run).

Live progress emit (EXEC-01, D-07): each node is registered through the
single ``_with_progress`` wrapping helper below, which publishes a RUNNING
event before the node runs and its terminal SUCCESS/PARTIAL/FAILED event
after, over the per-memo Redis channel owned by
``app.services.progress_publisher``. This is the one wiring point for
progress emission — no file under ``app/agents/`` is modified to support
it, and the wrapper no-ops (no publish calls) when ``state["memo_id"]`` is
absent, so existing callers that build state without ``memo_id`` (e.g.
graph integration tests) are unaffected.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from langgraph.graph import END, START, StateGraph

from app.agents.comparable_companies import comparable_companies_node
from app.agents.fundamental_analysis import fundamental_analysis_node
from app.agents.macro_sector import macro_sector_node
from app.agents.risk_assessment import risk_assessment_node
from app.agents.sentiment_nlp import sentiment_nlp_node
from app.agents.synthesis import synthesis_node
from app.graph.state import AgentGraphState
from app.services.progress_publisher import publish_agent_status

NodeFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def _with_progress(
    node_fn: NodeFn, agent_type: str, status_field: str
) -> NodeFn:
    """Wrap ``node_fn`` to emit RUNNING + terminal progress events (D-07).

    Reads ``state["memo_id"]``; when truthy, publishes a RUNNING event for
    ``agent_type`` before calling ``node_fn``, then — after ``node_fn``
    returns — publishes the node's own terminal status (read from
    ``result[status_field]``) for the same ``agent_type``. When ``memo_id``
    is missing/empty, calls ``node_fn`` and returns its result with no
    publish calls at all. Never alters the wrapped node's return value.
    """

    async def _wrapped(state: dict[str, Any]) -> dict[str, Any]:
        memo_id = state.get("memo_id")
        if memo_id:
            await publish_agent_status(
                memo_id=memo_id, agent_type=agent_type, status="RUNNING"
            )
        result = await node_fn(state)
        if memo_id:
            terminal = result.get(status_field)
            if terminal:
                await publish_agent_status(
                    memo_id=memo_id, agent_type=agent_type, status=terminal
                )
        return result

    return _wrapped


def build_research_graph():
    """Build and compile the 5-way fan-out + fan-in research graph.

    No checkpointer is passed to ``compile()`` — state (including any
    request-scoped DB session Fundamentals reads off ``state["session"]``)
    lives in-memory only for the duration of a single ``ainvoke`` call.

    All six nodes are registered through ``_with_progress`` (D-07) — the
    single point where per-agent live-progress events are emitted.
    """
    workflow = StateGraph(AgentGraphState)
    workflow.add_node(
        "fundamental_analysis",
        _with_progress(
            fundamental_analysis_node, "FundamentalAnalysis", "fundamentals_status"
        ),
    )
    workflow.add_node(
        "sentiment_nlp",
        _with_progress(sentiment_nlp_node, "SentimentNLP", "sentiment_status"),
    )
    workflow.add_node(
        "risk_assessment",
        _with_progress(risk_assessment_node, "RiskAssessment", "risk_status"),
    )
    workflow.add_node(
        "macro_sector",
        _with_progress(macro_sector_node, "MacroSector", "macro_status"),
    )
    workflow.add_node(
        "comparable_companies",
        _with_progress(
            comparable_companies_node, "ComparableCompanies", "comparables_status"
        ),
    )
    workflow.add_node(
        "synthesis",
        _with_progress(synthesis_node, "Synthesis", "synthesis_status"),
    )

    # 5 edges out of START = concurrent dispatch of every specialist
    # (AGENT-05) — LangGraph runs nodes with no unmet dependency in the same
    # super-step.
    workflow.add_edge(START, "fundamental_analysis")
    workflow.add_edge(START, "sentiment_nlp")
    workflow.add_edge(START, "risk_assessment")
    workflow.add_edge(START, "macro_sector")
    workflow.add_edge(START, "comparable_companies")

    # 5 edges into synthesis = fan-in wait on every specialist (AGENT-06) —
    # Synthesis only runs once all 5 have produced a state update.
    workflow.add_edge("fundamental_analysis", "synthesis")
    workflow.add_edge("sentiment_nlp", "synthesis")
    workflow.add_edge("risk_assessment", "synthesis")
    workflow.add_edge("macro_sector", "synthesis")
    workflow.add_edge("comparable_companies", "synthesis")

    workflow.add_edge("synthesis", END)
    return workflow.compile()

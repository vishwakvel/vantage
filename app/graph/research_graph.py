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

Routing is plain, declarative ``add_edge`` wiring only — every run
dispatches the same fixed 5-way fan-out with no runtime branching or
LLM-driven decision of which agents to run (PROJECT.md: declarative
routing, not LLM-driven; D-08: all 5 specialists dispatch every run).
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.agents.comparable_companies import comparable_companies_node
from app.agents.fundamental_analysis import fundamental_analysis_node
from app.agents.macro_sector import macro_sector_node
from app.agents.risk_assessment import risk_assessment_node
from app.agents.sentiment_nlp import sentiment_nlp_node
from app.agents.synthesis import synthesis_node
from app.graph.state import AgentGraphState


def build_research_graph():
    """Build and compile the 5-way fan-out + fan-in research graph.

    No checkpointer is passed to ``compile()`` — state (including any
    request-scoped DB session Fundamentals reads off ``state["session"]``)
    lives in-memory only for the duration of a single ``ainvoke`` call.
    """
    workflow = StateGraph(AgentGraphState)
    workflow.add_node("fundamental_analysis", fundamental_analysis_node)
    workflow.add_node("sentiment_nlp", sentiment_nlp_node)
    workflow.add_node("risk_assessment", risk_assessment_node)
    workflow.add_node("macro_sector", macro_sector_node)
    workflow.add_node("comparable_companies", comparable_companies_node)
    workflow.add_node("synthesis", synthesis_node)

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

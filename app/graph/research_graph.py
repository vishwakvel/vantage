"""2-node linear research graph: FundamentalAnalysis -> Synthesis.

Assembles the compiled ``StateGraph`` the ``/run`` endpoint invokes. The
sequential ``add_edge`` wiring guarantees Synthesis always runs after
FundamentalAnalysis — even when Fundamentals FAILED — which is the
structural precondition for EXEC-03's PARTIAL memo.

Boundary constraint (CI-enforced by ``tests/test_boundaries.py::
test_no_groq_import_in_graph``): this module imports only
``app.agents.*`` node functions, never ``groq``/``AsyncGroq`` directly.

Routing is plain ``add_edge`` only — no ``add_conditional_edges``, no
LLM-driven router (PROJECT.md: declarative routing, not LLM-driven).
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.agents.fundamental_analysis import fundamental_analysis_node
from app.agents.synthesis import synthesis_node
from app.graph.state import AgentGraphState


def build_research_graph():
    """Build and compile the linear FundamentalAnalysis -> Synthesis graph.

    No checkpointer is passed to ``compile()`` — state (including the
    request-scoped DB session) lives in-memory only for the duration of a
    single ``ainvoke`` call.
    """
    workflow = StateGraph(AgentGraphState)
    workflow.add_node("fundamental_analysis", fundamental_analysis_node)
    workflow.add_node("synthesis", synthesis_node)
    workflow.add_edge(START, "fundamental_analysis")
    workflow.add_edge("fundamental_analysis", "synthesis")
    workflow.add_edge("synthesis", END)
    return workflow.compile()

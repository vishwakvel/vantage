"""Unit tests for ``fundamental_analysis_node`` (04-02-PLAN.md, D-01).

Coverage (MEMO-02, MEMO-03, EXEC-02):
  - test_citations_have_canonical_id: citations carry the source chunk's
    ``metadata["canonical_id"]``.
  - test_citations_have_quote: citations carry a non-empty ``quote`` equal to
    the source chunk's ``text``.
  - test_status_success_all_sections: chunks covering all four target
    sections (mda, financials, notes, risk_factors) => AgentTask.status ==
    SUCCESS and AgentOutput.completeness == FULL.
  - test_status_partial_missing_section: chunks covering only some target
    sections => AgentTask.status == PARTIAL, AgentOutput.completeness ==
    PARTIAL, missing_fields lists the absent section names.
  - test_status_failed_zero_chunks: hybrid_retrieve returns [] =>
    AgentTask.status == FAILED and fundamentals_output is None; no exception.
  - test_node_never_raises_on_llm_error: call_groq raises => node returns a
    state update with fundamentals_status "FAILED" and does NOT propagate.
  - test_one_agenttask_and_one_agentoutput_persisted: after a run, exactly
    one AgentTask (agent_type "FundamentalAnalysis") and one AgentOutput
    exist for the plan.
  - test_anomalies_flow_through_to_output: detect_anomalies-produced
    anomalies land in fundamentals_output["anomalies"] and in the persisted
    AgentOutput.output, each carrying metric_name/period/value/severity/
    description (METRIC-02).
  - test_skipped_metrics_sets_insufficient_history_note: skipped_metrics
    sets metrics_note to _REASONS["insufficient_history"] (D-05).
  - test_skipped_metrics_coexist_with_anomalies: the note is not
    all-or-nothing — anomalies and the note can both be present.
  - test_metrics_persistence_failure_leaves_status_and_completeness_unchanged:
    persist_quarterly_metrics raising never changes AgentTask.status or
    AgentOutputCompleteness, sets metrics_note to
    _REASONS["metrics_unavailable"], never raises (R-E, D-07).
  - test_anomaly_detection_failure_leaves_status_and_completeness_unchanged:
    detect_anomalies raising behaves identically.
  - test_empty_metrics_series_sets_metrics_unavailable_note: an empty
    series mapping sets metrics_note to _REASONS["metrics_unavailable"].
  - test_zero_chunk_failed_path_has_anomalies_and_metrics_note_keys: the
    zero-chunk FAILED path still writes an anomalies:[] / metrics_note:None
    shape (_fallback_output consistency).
  - test_detect_anomalies_invoked_via_asyncio_to_thread: detect_anomalies
    is awaited through asyncio.to_thread, never called inline.
  - test_success_persists_token_counts: on the success path, the persisted
    AgentOutput carries prompt_tokens/completion_tokens equal to the mocked
    GroqResult's values (D-06).
  - test_zero_chunks_persists_null_token_counts: the zero-chunks early-return
    path (never calls Groq) persists NULL prompt_tokens/completion_tokens.
  - test_llm_error_persists_null_token_counts: a raising call_groq persists
    NULL prompt_tokens/completion_tokens on the fallback AgentOutput.

Mocks only at the SERVICE boundary — ``app.agents.fundamental_analysis.call_groq``
and ``app.agents.fundamental_analysis.hybrid_retrieve`` — never the groq SDK or
ChromaDB directly (mirrors ``tests/services/test_ticker_resolver.py``'s
boundary-mock convention). An autouse fixture additionally patches
``persist_quarterly_metrics`` for every test in this module (see below) so the
node's new metrics call can never reach a real external API from a unit test.
``call_groq`` is mocked to return a ``GroqResult`` (post-12-05 contract), built
via the ``_make_groq_result`` helper below rather than a bare string.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AgentOutput,
    AgentOutputCompleteness,
    AgentTask,
    AgentTaskStatus,
    ResearchPlan,
    ResearchRequest,
    User,
)
from app.ingestion.section_constants import (
    SECTION_FINANCIALS,
    SECTION_MDA,
    SECTION_NOTES,
    SECTION_RISK_FACTORS,
)
from app.services.anomaly_detection import AnomalyReport
from app.services.groq_client import GroqResult

pytestmark = pytest.mark.anyio

_ALL_TARGET_SECTIONS = (SECTION_MDA, SECTION_FINANCIALS, SECTION_NOTES, SECTION_RISK_FACTORS)


def _make_groq_result(
    text: str = "A narrative about AAPL's fundamentals.",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
) -> GroqResult:
    """Build a ``GroqResult`` for mocking ``call_groq`` (post-12-05 contract)."""
    return GroqResult(
        text=text,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        usage_metadata={
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    )


@pytest.fixture(autouse=True)
def _patch_persist_quarterly_metrics():
    """Patch ``persist_quarterly_metrics`` for every test in this module.

    The node now calls ``persist_quarterly_metrics`` (and therefore
    yfinance) on every reachable success-path branch. A unit test must
    never reach a real external API, so this default patch keeps the
    existing suite hermetic and offline by returning an empty series
    mapping. Individual tests that need specific series/failure behaviour
    override this patch locally inside their own ``with`` block.
    """
    with patch(
        "app.agents.fundamental_analysis.persist_quarterly_metrics",
        AsyncMock(return_value={}),
    ):
        yield


# ---------------------------------------------------------------------------
# Seed helpers — mirrors tests/api/test_research_api.py's User/ResearchPlan chain
# ---------------------------------------------------------------------------


async def _seed_user(db_session: AsyncSession) -> User:
    user = User(
        email=f"{uuid.uuid4()}@example.com",
        password_hash="not-a-real-hash",
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)
    return user


async def _seed_plan(db_session: AsyncSession, owner: User) -> ResearchPlan:
    request = ResearchRequest(
        user_id=owner.id, raw_query="Tell me about Apple", status="RESOLVED"
    )
    db_session.add(request)
    await db_session.flush()

    plan = ResearchPlan(
        request_id=request.id,
        user_id=owner.id,
        resolved_tickers=["AAPL"],
    )
    db_session.add(plan)
    await db_session.flush()
    await db_session.refresh(plan)
    return plan


# ---------------------------------------------------------------------------
# Fake chunk builder — matches hybrid_retrieve's documented return shape
# (app/ingestion/retriever.py:131-162)
# ---------------------------------------------------------------------------


def _make_chunk(section: str, canonical_id: str | None = None, text: str | None = None) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "text": text or f"Sample {section} narrative text discussing AAPL.",
        "metadata": {
            "canonical_id": canonical_id or f"canon-{section}-{uuid.uuid4()}",
            "section": section,
            "ticker": "AAPL",
            "form_type": "10-K",
            "period_of_report": "2025-12-31",
        },
        "score": 0.9,
    }


def _all_section_chunks() -> list[dict]:
    return [_make_chunk(section) for section in _ALL_TARGET_SECTIONS]


async def _build_state(db_session: AsyncSession, plan: ResearchPlan, user: User) -> dict:
    return {
        "session": db_session,
        "ticker": "AAPL",
        "user_id": str(user.id),
        "plan_id": str(plan.id),
    }


# ---------------------------------------------------------------------------
# Citation shape (MEMO-02, MEMO-03)
# ---------------------------------------------------------------------------


async def test_citations_have_canonical_id(db_session: AsyncSession) -> None:
    """Each citation carries the source chunk's canonical_id."""
    from app.agents.fundamental_analysis import fundamental_analysis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    chunks = _all_section_chunks()
    state = await _build_state(db_session, plan, user)

    with (
        patch(
            "app.agents.fundamental_analysis.hybrid_retrieve", return_value=chunks
        ),
        patch(
            "app.agents.fundamental_analysis.call_groq",
            AsyncMock(return_value=_make_groq_result()),
        ),
    ):
        result = await fundamental_analysis_node(state)

    citations = result["fundamentals_output"]["citations"]
    assert len(citations) == len(chunks)
    expected_ids = {c["metadata"]["canonical_id"] for c in chunks}
    actual_ids = {c["canonical_id"] for c in citations}
    assert actual_ids == expected_ids
    for citation in citations:
        assert citation["canonical_id"]


# ---------------------------------------------------------------------------


async def test_citations_have_quote(db_session: AsyncSession) -> None:
    """Each citation carries a non-empty quote equal to the source chunk's text."""
    from app.agents.fundamental_analysis import fundamental_analysis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    chunks = _all_section_chunks()
    state = await _build_state(db_session, plan, user)

    with (
        patch(
            "app.agents.fundamental_analysis.hybrid_retrieve", return_value=chunks
        ),
        patch(
            "app.agents.fundamental_analysis.call_groq",
            AsyncMock(return_value=_make_groq_result()),
        ),
    ):
        result = await fundamental_analysis_node(state)

    citations = result["fundamentals_output"]["citations"]
    text_by_id = {c["id"]: c["text"] for c in chunks}
    for citation in citations:
        assert citation["quote"]
        assert citation["quote"] == text_by_id[citation["chunk_id"]]


# ---------------------------------------------------------------------------
# Status / coverage rule
# ---------------------------------------------------------------------------


async def test_status_success_all_sections(db_session: AsyncSession) -> None:
    """Chunks covering all four target sections => SUCCESS + FULL."""
    from app.agents.fundamental_analysis import fundamental_analysis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    chunks = _all_section_chunks()
    state = await _build_state(db_session, plan, user)

    with (
        patch(
            "app.agents.fundamental_analysis.hybrid_retrieve", return_value=chunks
        ),
        patch(
            "app.agents.fundamental_analysis.call_groq",
            AsyncMock(return_value=_make_groq_result()),
        ),
    ):
        result = await fundamental_analysis_node(state)

    assert result["fundamentals_status"] == AgentTaskStatus.SUCCESS.value

    task_row = (
        await db_session.execute(
            select(AgentTask).where(AgentTask.plan_id == plan.id)
        )
    ).scalar_one()
    assert task_row.status == AgentTaskStatus.SUCCESS

    output_row = (
        await db_session.execute(
            select(AgentOutput).where(AgentOutput.task_id == task_row.id)
        )
    ).scalar_one()
    assert output_row.completeness == AgentOutputCompleteness.FULL
    assert output_row.missing_fields is None
    assert output_row.prompt_tokens == 100
    assert output_row.completion_tokens == 50


# ---------------------------------------------------------------------------


async def test_status_partial_missing_section(db_session: AsyncSession) -> None:
    """Chunks covering only some target sections => PARTIAL + PARTIAL, missing_fields populated."""
    from app.agents.fundamental_analysis import fundamental_analysis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    # Only MDA and Financials present — Notes and Risk Factors missing.
    chunks = [_make_chunk(SECTION_MDA), _make_chunk(SECTION_FINANCIALS)]
    state = await _build_state(db_session, plan, user)

    with (
        patch(
            "app.agents.fundamental_analysis.hybrid_retrieve", return_value=chunks
        ),
        patch(
            "app.agents.fundamental_analysis.call_groq",
            AsyncMock(return_value=_make_groq_result()),
        ),
    ):
        result = await fundamental_analysis_node(state)

    assert result["fundamentals_status"] == AgentTaskStatus.PARTIAL.value

    task_row = (
        await db_session.execute(
            select(AgentTask).where(AgentTask.plan_id == plan.id)
        )
    ).scalar_one()
    assert task_row.status == AgentTaskStatus.PARTIAL

    output_row = (
        await db_session.execute(
            select(AgentOutput).where(AgentOutput.task_id == task_row.id)
        )
    ).scalar_one()
    assert output_row.completeness == AgentOutputCompleteness.PARTIAL
    assert set(output_row.missing_fields) == {SECTION_NOTES, SECTION_RISK_FACTORS}


# ---------------------------------------------------------------------------


async def test_status_failed_zero_chunks(db_session: AsyncSession) -> None:
    """Zero retrieved chunks => AgentTask.status FAILED, fundamentals_output None, no exception."""
    from app.agents.fundamental_analysis import _REASONS, fundamental_analysis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = await _build_state(db_session, plan, user)

    with (
        patch("app.agents.fundamental_analysis.hybrid_retrieve", return_value=[]),
        patch(
            "app.agents.fundamental_analysis.call_groq",
            AsyncMock(return_value=_make_groq_result(text="unused")),
        ) as mock_call_groq,
    ):
        result = await fundamental_analysis_node(state)

    assert result["fundamentals_status"] == AgentTaskStatus.FAILED.value
    assert result["fundamentals_output"] is None
    mock_call_groq.assert_not_awaited()

    task_row = (
        await db_session.execute(
            select(AgentTask).where(AgentTask.plan_id == plan.id)
        )
    ).scalar_one()
    assert task_row.status == AgentTaskStatus.FAILED

    output_row = (
        await db_session.execute(
            select(AgentOutput).where(AgentOutput.task_id == task_row.id)
        )
    ).scalar_one()
    # D-07: a human-readable reason sentence, never the raw section-name list
    # (that list is reserved for the PARTIAL missing-some-sections case).
    assert output_row.missing_fields == _REASONS["zero_chunks"]
    assert output_row.prompt_tokens is None
    assert output_row.completion_tokens is None


# ---------------------------------------------------------------------------


async def test_node_never_raises_on_llm_error(db_session: AsyncSession) -> None:
    """call_groq raising an exception never propagates; node degrades to FAILED."""
    from app.agents.fundamental_analysis import _REASONS, fundamental_analysis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    chunks = _all_section_chunks()
    state = await _build_state(db_session, plan, user)

    with (
        patch(
            "app.agents.fundamental_analysis.hybrid_retrieve", return_value=chunks
        ),
        patch(
            "app.agents.fundamental_analysis.call_groq",
            AsyncMock(side_effect=RuntimeError("groq boom")),
        ),
    ):
        result = await fundamental_analysis_node(state)

    assert result["fundamentals_status"] == "FAILED"
    assert result["fundamentals_output"] is None

    task_row = (
        await db_session.execute(
            select(AgentTask).where(AgentTask.plan_id == plan.id)
        )
    ).scalar_one()
    assert task_row.status == AgentTaskStatus.FAILED

    output_row = (
        await db_session.execute(
            select(AgentOutput).where(AgentOutput.task_id == task_row.id)
        )
    ).scalar_one()
    # D-07: a distinct reason from the zero-chunks case — the previous bug
    # wrote the identical section-name list for every failure path,
    # regardless of cause.
    assert output_row.missing_fields == _REASONS["llm_error"]
    assert output_row.missing_fields != _REASONS["zero_chunks"]
    assert output_row.prompt_tokens is None
    assert output_row.completion_tokens is None


# ---------------------------------------------------------------------------
# Persistence cardinality
# ---------------------------------------------------------------------------


async def test_one_agenttask_and_one_agentoutput_persisted(db_session: AsyncSession) -> None:
    """Exactly one AgentTask (FundamentalAnalysis) and one AgentOutput exist per run."""
    from app.agents.fundamental_analysis import fundamental_analysis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    chunks = _all_section_chunks()
    state = await _build_state(db_session, plan, user)

    with (
        patch(
            "app.agents.fundamental_analysis.hybrid_retrieve", return_value=chunks
        ),
        patch(
            "app.agents.fundamental_analysis.call_groq",
            AsyncMock(return_value=_make_groq_result()),
        ),
    ):
        await fundamental_analysis_node(state)

    task_rows = (
        await db_session.execute(
            select(AgentTask).where(
                AgentTask.plan_id == plan.id,
                AgentTask.agent_type == "FundamentalAnalysis",
            )
        )
    ).scalars().all()
    assert len(task_rows) == 1

    output_rows = (
        await db_session.execute(
            select(AgentOutput).where(AgentOutput.task_id == task_rows[0].id)
        )
    ).scalars().all()
    assert len(output_rows) == 1


# ---------------------------------------------------------------------------
# Metric persistence + anomaly detection (METRIC-01/02/03, 09-06-PLAN.md)
# ---------------------------------------------------------------------------


_SAMPLE_SERIES = {
    "revenue": [
        ("2025Q1", 1_000_000.0),
        ("2025Q2", 1_100_000.0),
        ("2025Q3", 1_050_000.0),
        ("2025Q4", 100_000_000.0),
    ]
}

_SAMPLE_ANOMALY = {
    "metric_name": "revenue",
    "period": "2025Q4",
    "value": 100_000_000.0,
    "severity": "High",
    "description": (
        "Revenue climbed to $100.00M in 2025Q4, the highest of the 4 "
        "quarters on record (median $1.05M)."
    ),
}


async def test_anomalies_flow_through_to_output(db_session: AsyncSession) -> None:
    """Anomalies from detect_anomalies land in fundamentals_output['anomalies']
    and in the persisted AgentOutput.output, each carrying the expected keys."""
    from app.agents.fundamental_analysis import fundamental_analysis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    chunks = _all_section_chunks()
    state = await _build_state(db_session, plan, user)
    report = AnomalyReport(anomalies=[_SAMPLE_ANOMALY], skipped_metrics=[])

    with (
        patch(
            "app.agents.fundamental_analysis.hybrid_retrieve", return_value=chunks
        ),
        patch(
            "app.agents.fundamental_analysis.call_groq",
            AsyncMock(return_value=_make_groq_result()),
        ),
        patch(
            "app.agents.fundamental_analysis.persist_quarterly_metrics",
            AsyncMock(return_value=_SAMPLE_SERIES),
        ),
        patch(
            "app.agents.fundamental_analysis.detect_anomalies",
            return_value=report,
        ),
    ):
        result = await fundamental_analysis_node(state)

    assert result["fundamentals_output"]["anomalies"] == [_SAMPLE_ANOMALY]
    assert result["fundamentals_output"]["metrics_note"] is None

    task_row = (
        await db_session.execute(
            select(AgentTask).where(AgentTask.plan_id == plan.id)
        )
    ).scalar_one()
    output_row = (
        await db_session.execute(
            select(AgentOutput).where(AgentOutput.task_id == task_row.id)
        )
    ).scalar_one()
    assert output_row.output["anomalies"] == [_SAMPLE_ANOMALY]
    for key in ("metric_name", "period", "value", "severity", "description"):
        assert key in output_row.output["anomalies"][0]


# ---------------------------------------------------------------------------


async def test_skipped_metrics_sets_insufficient_history_note(
    db_session: AsyncSession,
) -> None:
    """skipped_metrics sets metrics_note to _REASONS["insufficient_history"] (D-05)."""
    from app.agents.fundamental_analysis import (
        _REASONS,
        fundamental_analysis_node,
    )

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    chunks = _all_section_chunks()
    state = await _build_state(db_session, plan, user)
    report = AnomalyReport(anomalies=[], skipped_metrics=["debt_to_equity"])

    with (
        patch(
            "app.agents.fundamental_analysis.hybrid_retrieve", return_value=chunks
        ),
        patch(
            "app.agents.fundamental_analysis.call_groq",
            AsyncMock(return_value=_make_groq_result(text="narrative")),
        ),
        patch(
            "app.agents.fundamental_analysis.persist_quarterly_metrics",
            AsyncMock(return_value=_SAMPLE_SERIES),
        ),
        patch(
            "app.agents.fundamental_analysis.detect_anomalies",
            return_value=report,
        ),
    ):
        result = await fundamental_analysis_node(state)

    assert result["fundamentals_output"]["metrics_note"] == _REASONS[
        "insufficient_history"
    ]
    assert result["fundamentals_output"]["anomalies"] == []


# ---------------------------------------------------------------------------


async def test_skipped_metrics_coexist_with_anomalies(
    db_session: AsyncSession,
) -> None:
    """The note is not all-or-nothing: skipped metrics and real anomalies
    can both be returned from the same run."""
    from app.agents.fundamental_analysis import (
        _REASONS,
        fundamental_analysis_node,
    )

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    chunks = _all_section_chunks()
    state = await _build_state(db_session, plan, user)
    report = AnomalyReport(
        anomalies=[_SAMPLE_ANOMALY], skipped_metrics=["debt_to_equity"]
    )

    with (
        patch(
            "app.agents.fundamental_analysis.hybrid_retrieve", return_value=chunks
        ),
        patch(
            "app.agents.fundamental_analysis.call_groq",
            AsyncMock(return_value=_make_groq_result(text="narrative")),
        ),
        patch(
            "app.agents.fundamental_analysis.persist_quarterly_metrics",
            AsyncMock(return_value=_SAMPLE_SERIES),
        ),
        patch(
            "app.agents.fundamental_analysis.detect_anomalies",
            return_value=report,
        ),
    ):
        result = await fundamental_analysis_node(state)

    assert result["fundamentals_output"]["metrics_note"] == _REASONS[
        "insufficient_history"
    ]
    assert result["fundamentals_output"]["anomalies"] == [_SAMPLE_ANOMALY]


# ---------------------------------------------------------------------------


async def test_metrics_persistence_failure_leaves_status_and_completeness_unchanged(
    db_session: AsyncSession,
) -> None:
    """R-E/D-07: persist_quarterly_metrics raising must not change
    AgentTask.status or AgentOutputCompleteness relative to the same
    scenario without the failure, and the node must not raise."""
    from app.agents.fundamental_analysis import (
        _REASONS,
        fundamental_analysis_node,
    )

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    chunks = _all_section_chunks()  # SUCCESS/FULL scenario
    state = await _build_state(db_session, plan, user)

    with (
        patch(
            "app.agents.fundamental_analysis.hybrid_retrieve", return_value=chunks
        ),
        patch(
            "app.agents.fundamental_analysis.call_groq",
            AsyncMock(return_value=_make_groq_result(text="narrative")),
        ),
        patch(
            "app.agents.fundamental_analysis.persist_quarterly_metrics",
            AsyncMock(side_effect=RuntimeError("yfinance down")),
        ),
    ):
        result = await fundamental_analysis_node(state)

    assert result["fundamentals_status"] == AgentTaskStatus.SUCCESS.value
    assert result["fundamentals_output"]["anomalies"] == []
    assert result["fundamentals_output"]["metrics_note"] == _REASONS[
        "metrics_unavailable"
    ]

    task_row = (
        await db_session.execute(
            select(AgentTask).where(AgentTask.plan_id == plan.id)
        )
    ).scalar_one()
    assert task_row.status == AgentTaskStatus.SUCCESS

    output_row = (
        await db_session.execute(
            select(AgentOutput).where(AgentOutput.task_id == task_row.id)
        )
    ).scalar_one()
    assert output_row.completeness == AgentOutputCompleteness.FULL


# ---------------------------------------------------------------------------


async def test_anomaly_detection_failure_leaves_status_and_completeness_unchanged(
    db_session: AsyncSession,
) -> None:
    """detect_anomalies raising behaves identically to a persistence failure."""
    from app.agents.fundamental_analysis import (
        _REASONS,
        fundamental_analysis_node,
    )

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    chunks = _all_section_chunks()  # SUCCESS/FULL scenario
    state = await _build_state(db_session, plan, user)

    with (
        patch(
            "app.agents.fundamental_analysis.hybrid_retrieve", return_value=chunks
        ),
        patch(
            "app.agents.fundamental_analysis.call_groq",
            AsyncMock(return_value=_make_groq_result(text="narrative")),
        ),
        patch(
            "app.agents.fundamental_analysis.persist_quarterly_metrics",
            AsyncMock(return_value=_SAMPLE_SERIES),
        ),
        patch(
            "app.agents.fundamental_analysis.detect_anomalies",
            side_effect=RuntimeError("sklearn boom"),
        ),
    ):
        result = await fundamental_analysis_node(state)

    assert result["fundamentals_status"] == AgentTaskStatus.SUCCESS.value
    assert result["fundamentals_output"]["anomalies"] == []
    assert result["fundamentals_output"]["metrics_note"] == _REASONS[
        "metrics_unavailable"
    ]

    task_row = (
        await db_session.execute(
            select(AgentTask).where(AgentTask.plan_id == plan.id)
        )
    ).scalar_one()
    assert task_row.status == AgentTaskStatus.SUCCESS

    output_row = (
        await db_session.execute(
            select(AgentOutput).where(AgentOutput.task_id == task_row.id)
        )
    ).scalar_one()
    assert output_row.completeness == AgentOutputCompleteness.FULL


# ---------------------------------------------------------------------------


async def test_empty_metrics_series_sets_metrics_unavailable_note(
    db_session: AsyncSession,
) -> None:
    """An empty series mapping sets metrics_note to
    _REASONS["metrics_unavailable"] and anomalies to []."""
    from app.agents.fundamental_analysis import (
        _REASONS,
        fundamental_analysis_node,
    )

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    chunks = _all_section_chunks()
    state = await _build_state(db_session, plan, user)

    with (
        patch(
            "app.agents.fundamental_analysis.hybrid_retrieve", return_value=chunks
        ),
        patch(
            "app.agents.fundamental_analysis.call_groq",
            AsyncMock(return_value=_make_groq_result(text="narrative")),
        ),
        patch(
            "app.agents.fundamental_analysis.persist_quarterly_metrics",
            AsyncMock(return_value={}),
        ),
        patch(
            "app.agents.fundamental_analysis.detect_anomalies"
        ) as mock_detect,
    ):
        result = await fundamental_analysis_node(state)

    assert result["fundamentals_output"]["anomalies"] == []
    assert result["fundamentals_output"]["metrics_note"] == _REASONS[
        "metrics_unavailable"
    ]
    mock_detect.assert_not_called()


# ---------------------------------------------------------------------------


async def test_zero_chunk_failed_path_has_anomalies_and_metrics_note_keys(
    db_session: AsyncSession,
) -> None:
    """The zero-chunk FAILED path still writes an AgentOutput.output
    containing anomalies:[] and metrics_note:None (_fallback_output shape
    consistency)."""
    from app.agents.fundamental_analysis import fundamental_analysis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    state = await _build_state(db_session, plan, user)

    with patch("app.agents.fundamental_analysis.hybrid_retrieve", return_value=[]):
        result = await fundamental_analysis_node(state)

    assert result["fundamentals_output"] is None

    task_row = (
        await db_session.execute(
            select(AgentTask).where(AgentTask.plan_id == plan.id)
        )
    ).scalar_one()
    output_row = (
        await db_session.execute(
            select(AgentOutput).where(AgentOutput.task_id == task_row.id)
        )
    ).scalar_one()
    assert output_row.output["anomalies"] == []
    assert output_row.output["metrics_note"] is None


# ---------------------------------------------------------------------------


async def test_detect_anomalies_invoked_via_asyncio_to_thread(
    db_session: AsyncSession,
) -> None:
    """detect_anomalies is awaited through asyncio.to_thread, never called
    inline in the coroutine (T-09-DOS-LOOP)."""
    from app.agents.fundamental_analysis import fundamental_analysis_node

    user = await _seed_user(db_session)
    plan = await _seed_plan(db_session, user)
    chunks = _all_section_chunks()
    state = await _build_state(db_session, plan, user)
    report = AnomalyReport(anomalies=[], skipped_metrics=[])

    with (
        patch(
            "app.agents.fundamental_analysis.hybrid_retrieve", return_value=chunks
        ),
        patch(
            "app.agents.fundamental_analysis.call_groq",
            AsyncMock(return_value=_make_groq_result(text="narrative")),
        ),
        patch(
            "app.agents.fundamental_analysis.persist_quarterly_metrics",
            AsyncMock(return_value=_SAMPLE_SERIES),
        ),
        patch(
            "app.agents.fundamental_analysis.detect_anomalies"
        ) as mock_detect,
        patch(
            "app.agents.fundamental_analysis.asyncio.to_thread",
            AsyncMock(return_value=report),
        ) as mock_to_thread,
    ):
        await fundamental_analysis_node(state)

    mock_to_thread.assert_awaited_once_with(mock_detect, _SAMPLE_SERIES)
    mock_detect.assert_not_called()

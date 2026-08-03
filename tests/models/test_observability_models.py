"""ORM-level model tests for Phase 12's persistence layer (12-02-PLAN.md,
OBS-02, OBS-03).

Coverage (shape assertions against `Model.__table__.columns` /
`Model.__table__.foreign_keys` — no live DB, mirrors
`tests/models/test_alert_event_model.py`'s Phase-11 analog):
  - `AgentOutput.prompt_tokens` / `.completion_tokens` are nullable Integer
    columns (D-06) — every agent failure path writes an `AgentOutput` row
    without ever reaching a Groq call, and all pre-migration rows have no
    token data at all.
  - `RagasEvalResult` is a standalone table (`ragas_eval_results`) with zero
    ForeignKey constraints (D-10 — no natural per-memo or per-user
    relationship for a golden-set eval score).
  - `RagasEvalResult` exposes the full column set the offline eval run
    writes.
  - `RagasEvalResult.context_precision` is nullable — a per-case scoring
    failure persists a NULL score rather than aborting the run.
"""

from __future__ import annotations

from sqlalchemy import Float, Integer

from app.db.models import AgentOutput, RagasEvalResult


def test_agent_output_prompt_tokens_is_nullable_integer() -> None:
    column = AgentOutput.__table__.columns["prompt_tokens"]
    assert column.nullable is True
    assert isinstance(column.type, Integer)


def test_agent_output_completion_tokens_is_nullable_integer() -> None:
    column = AgentOutput.__table__.columns["completion_tokens"]
    assert column.nullable is True
    assert isinstance(column.type, Integer)


def test_ragas_eval_result_table_has_no_foreign_keys() -> None:
    assert RagasEvalResult.__tablename__ == "ragas_eval_results"
    assert len(RagasEvalResult.__table__.foreign_keys) == 0


def test_ragas_eval_result_exposes_expected_columns() -> None:
    columns = RagasEvalResult.__table__.columns
    for name in (
        "id",
        "run_at",
        "case_id",
        "query",
        "ticker",
        "context_precision",
        "context_recall",
        "retrieved_count",
    ):
        assert name in columns


def test_ragas_eval_result_context_precision_is_nullable() -> None:
    column = RagasEvalResult.__table__.columns["context_precision"]
    assert column.nullable is True
    assert isinstance(column.type, Float)

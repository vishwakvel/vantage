"""Live smoke test proving a real Groq call emits a real LangSmith trace
carrying real, strictly-positive token counts (OBS-01, RESEARCH.md
Assumption A2).

Gated exactly like ``tests/live/test_live_service_clients.py``: the whole
module is skipped unless ``RUN_LIVE_TESTS=1`` is set, so an ordinary
``pytest`` run (CI, local dev) stays fully hermetic and makes zero outbound
calls. The one test here additionally skips if ``LANGSMITH_API_KEY`` is
absent, mirroring that module's per-credential skip for NewsAPI/FRED —
``RUN_LIVE_TESTS=1`` alone should not require a LangSmith account to be
useful for other live modules.

Data exposure: enabling tracing for this test's duration sends the FULL
prompt text and completion text of the one Groq call below to LangSmith's
hosted backend (api.smith.langchain.com). That is bounded deliberately: the
prompt is a fixed, trivial, single-sentence constant containing no filing
text, no ticker, and no user data (T-12-14-TRACELEAK) — this module's
contract is exactly what it sends, not an incidental detail. The small
``_MAX_TOKENS`` budget also keeps this check off the shared 6,000-tokens/min
Groq quota STATE.md records having been exhausted once by ordinary use
(T-12-14-QUOTA).

What this test does NOT verify: whether the traced run actually renders its
token counts in the LangSmith UI. RESEARCH.md's Environment Availability
table records that no LangSmith credentials existed during research, so the
trace-listing client API was never verified against a real account —
asserting against a guessed API would trade a known-honest human check for a
guess. That verification (RESEARCH.md Assumption A2) is deliberately left to
this plan's Task 4 human checkpoint: this test only proves the *token
counts themselves* are real and positive, then prints the numbers a human
matches against the LangSmith UI by hand. If Assumption A2 resolves false —
the trace renders but token counts do not — plan 12-05's recorded fallback is
a plain-dict return shape from ``call_groq``, or attaching usage to the
current run tree inside the function; either is a cosmetic gap only, since
``GroqResult.prompt_tokens``/``.completion_tokens`` (asserted here) remain
the authoritative source for OBS-02 and MEMO-06 regardless of what the
LangSmith UI renders.

Pitfall 6 caveat: ``llama-3.3-70b-versatile`` was decommissioned by Groq on
2026-08-16; Phase 13-06 swapped the ``call_groq`` default to
``openai/gpt-oss-20b`` (see ``app/agents/synthesis.py``). A failure here
naming the model rather than a shape or count problem points at a further
Groq model change, not a regression in this test's subject.

Invocation::

    RUN_LIVE_TESTS=1 LANGSMITH_API_KEY=... \\
        python -m pytest tests/live/test_live_langsmith_tracing.py -v -s
"""

import asyncio
import os

import pytest

from app.services.groq_client import GroqResult, call_groq, reset_groq_client

pytestmark = [
    pytest.mark.live,
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_TESTS") != "1",
        reason="live API tests: set RUN_LIVE_TESTS=1 to run",
    ),
]

#: Fixed, trivial, single-sentence prompt — no filing text, no ticker, no
#: user data. Tracing ships this verbatim to LangSmith's hosted backend
#: (T-12-14-TRACELEAK), so what this sends is part of this module's
#: contract, not an incidental detail.
_TRACE_TEST_PROMPT = "Reply with the single word: pong."

#: Small completion budget — keeps this one-off check off the shared
#: 6,000-tokens/min Groq quota (T-12-14-QUOTA).
_MAX_TOKENS = 16

#: Remaining safety margin AFTER the LangSmith client's own ``flush()`` call
#: below returns. ``flush()`` drains this process's local send queue, but
#: LangSmith's backend may take a brief moment to index an ingested run
#: before it is visible in the UI — this settle time is the human
#: checkpoint's grace period, not a functional wait for this test's own
#: assertions (which never query the LangSmith API). Mirrors
#: ``scripts/smoke_alert_evaluation.py``'s ``_PUBSUB_SETTLE_SECONDS``
#: register: generous relative to real ingestion latency, cheap relative to
#: a human switching tabs to check.
_TRACE_SETTLE_SECONDS = 2.0


async def test_live_call_groq_emits_langsmith_trace_with_token_counts(monkeypatch) -> None:
    """One real Groq call, traced, asserting real strictly-positive usage."""
    if not os.environ.get("LANGSMITH_API_KEY"):
        pytest.skip("LANGSMITH_API_KEY not set — cannot run live LangSmith smoke test")

    # Enable tracing for this test only — current LangSmith-prefixed name,
    # not the deprecated LANGCHAIN_* alias (RESEARCH.md Pattern 2 naming
    # note). LANGSMITH_API_KEY itself is read from the operator's real
    # environment above, never set here.
    monkeypatch.setenv("LANGSMITH_TRACING", "true")

    # Phase 6 lesson (groq_client.reset_groq_client's own docstring): rebind
    # the lazy AsyncGroq singleton to this test's own event loop before
    # calling it.
    reset_groq_client()

    result = await call_groq(_TRACE_TEST_PROMPT, max_tokens=_MAX_TOKENS)

    assert isinstance(result, GroqResult)
    assert isinstance(result.text, str)
    assert len(result.text) > 0
    assert result.prompt_tokens > 0
    assert result.completion_tokens > 0

    # The LangSmith-shaped usage_metadata dict is derived from the same two
    # real numbers, not a second mock — pin it to them directly.
    assert result.usage_metadata["input_tokens"] == result.prompt_tokens
    assert result.usage_metadata["output_tokens"] == result.completion_tokens
    assert result.usage_metadata["total_tokens"] == result.prompt_tokens + result.completion_tokens

    # Traced runs are shipped in the background. Read the installed
    # langsmith distribution's own documented drain/flush path rather than
    # guessing an API from memory: langsmith.client.Client.flush(timeout=...)
    # blocks until this process's pending trace-send queue is empty, and
    # langsmith.run_trees.get_cached_client() returns the same lazily-built,
    # process-wide Client instance @traceable itself uses when no explicit
    # client is passed to call_groq (confirmed by reading
    # langsmith/run_trees.py — get_cached_client is the module's own
    # "called directly by langchain, do not remove" shared accessor).
    from langsmith.run_trees import get_cached_client

    get_cached_client().flush(timeout=10.0)
    await asyncio.sleep(_TRACE_SETTLE_SECONDS)

    # Resolved LangSmith project name — falls back to the SDK's own default
    # ("default") when LANGSMITH_PROJECT/LANGCHAIN_PROJECT is unset.
    from langsmith.utils import get_tracer_project

    project_name = get_tracer_project()
    total_tokens = result.prompt_tokens + result.completion_tokens

    # "Eyeball this" register (scripts/smoke_alert_evaluation.py's
    # convention) — Task 4's checkpoint reads these numbers off the
    # terminal and matches them against the LangSmith UI.
    print(f"\n  [eyeball this] LangSmith project: {project_name!r}")
    print(f"  [eyeball this] prompt_tokens: {result.prompt_tokens}")
    print(f"  [eyeball this] completion_tokens: {result.completion_tokens}")
    print(f"  [eyeball this] total_tokens: {total_tokens}")
    print(
        "  Open this project in the LangSmith UI and find the "
        "'groq_chat_completion' run — confirm it shows the prompt, the "
        "completion, and these same token counts (Assumption A2)."
    )

"""CI enforcement of the ``reset_*()`` async-singleton convention.

Invariant guarded here: every module-level singleton in ``app/services/`` that
wraps a persistent asyncio-loop-bound client (an ``httpx.AsyncClient``, held
directly or one level deep via ``._client``, or an ``AsyncGroq``) exposes a
matching ``reset_*()`` callable in the *same* module, AND
``app/workers/tasks.py`` both imports and calls every such reset before its
``asyncio.run(...)``. ``app/db/session.py::reset_session_factory`` is guarded
by name as well — the engine holds loop-bound asyncpg connections rather than
an ``httpx.AsyncClient``, so introspection cannot see it.

Two enforcement techniques, one per half:

- **Runtime introspection** for the singleton half. ``conftest`` already
  imports ``app.main``, so every ``app.services`` submodule import cost is
  already paid; inspecting live module attributes for an ``httpx.AsyncClient``
  / ``AsyncGroq`` is exact. An ``ast`` scan cannot tell a module-level
  assignment that wraps an async client from a plain object without brittle
  naming heuristics (decision D-03), so it is the wrong tool here.
- **``ast`` source scanning** for the ``app/workers/tasks.py`` wiring half.
  The actual 999.1 failure mode (decision D-04) is a ``reset_*()`` that exists
  but is never invoked before ``asyncio.run(...)`` — existence alone is
  insufficient, so the wiring is checked structurally against the source.

The required-reset set is derived at runtime from
``_required_reset_names()`` and consumed by BOTH tests, so a future client
(e.g. Phase 16's email client) is guarded automatically with no edit to this
file.

This module never imports the ``groq`` package (it detects ``AsyncGroq`` by
class name) so it does not perturb the ``sys.modules`` state that
``tests/test_boundaries.py::test_no_groq_import_in_agents`` observes.

Reference: DEBT-01; decisions D-01, D-02, D-03, D-04, D-05;
``.planning/research/PITFALLS.md`` Pitfall 1 (the ``reset_*()`` singleton gap).
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import httpx

# DRY: reuse the package walker from the sibling boundary-test module. If this
# ever proves fragile under this project's pytest import mode, copy the helper
# verbatim instead (it is ~30 lines) and note it here.
from tests.test_boundaries import _import_all_submodules

# ---------------------------------------------------------------------------
# Exempt list (D-02) — module short name -> one-line reason.
#
# D-02 explicitly REJECTED adding no-op reset_*() functions to these modules:
# that would be dead code implying loop-bound state that does not exist. Each
# entry self-invalidates via test_reset_exempt_list_is_not_stale below.
# ---------------------------------------------------------------------------
_RESET_EXEMPT: dict[str, str] = {
    "live_price_source": (
        "yfinance is synchronous, every call goes through `asyncio.to_thread`, "
        "no loop-bound client object exists (see the module docstring)"
    ),
    "financial_metrics_source": (
        "yfinance is synchronous, every call goes through `asyncio.to_thread`, "
        "no loop-bound client object exists (see the module docstring)"
    ),
    "comparables_source": (
        "yfinance is synchronous, every call goes through `asyncio.to_thread`, "
        "no loop-bound client object exists (see the module docstring)"
    ),
    "vector_store": (
        "`chroma_client` is a `chromadb.HttpClient` — `requests`-based, "
        "synchronous, not asyncio-loop-bound"
    ),
}

# ---------------------------------------------------------------------------
# Extra guarded resets (D-01) — dotted module path -> required reset name.
#
# The DB engine holds loop-bound asyncpg connections rather than an
# httpx.AsyncClient, so _module_async_client_attrs cannot find it by value or
# annotation. It is the origin of the whole convention, so guard it by name.
# ---------------------------------------------------------------------------
_EXTRA_GUARDED_RESETS: dict[str, str] = {
    "app.db.session": "reset_session_factory",
}

_RESET_PREFIX = "reset_"

#: The Celery entrypoint that owns the reset block, and the file it lives in.
_TASKS_MODULE_PATH = Path("app/workers/tasks.py")
_TASK_ENTRYPOINT_NAME = "run_research_task"


def _is_async_client(value: object) -> bool:
    """True if *value* is a loop-bound async client this convention guards.

    Matches an ``httpx.AsyncClient`` instance, or anything whose runtime class
    is named ``AsyncGroq`` (matched by name so this module never imports the
    ``groq`` package — see the module docstring).
    """
    if isinstance(value, httpx.AsyncClient):
        return True
    return type(value).__name__ == "AsyncGroq"


def _defined_in_module(value: object, module_name: str) -> bool:
    """True if *value*'s class is defined in *module_name*.

    Distinguishes a singleton created in this module from one merely
    re-imported into it (``from app.services.edgar_client import edgar_client``
    in an unrelated service module must not be counted against that module).
    """
    return getattr(type(value), "__module__", None) == module_name


def _module_async_client_attrs(module: object) -> list[str]:
    """Return the module-level attribute names that hold a loop-bound client.

    Two detection branches:

    (a) **value inspection** — the attribute value's one-level-deep
        ``._client`` is a loop-bound async client (or the value itself is),
        AND the value's class is defined in this module. Catches every eager
        singleton (edgar / arxiv / news / fred); the module-ownership guard
        rejects re-imported client singletons.
    (b) **annotation inspection** — the module ``__annotations__`` entry for
        the name, coerced with ``str(...)``, mentions ``AsyncClient`` or
        ``AsyncGroq``. The annotations dict is inherently module-local, so no
        ownership guard is needed. This is what catches the lazy variant:
        ``groq_client._client`` is literally ``None`` at import time and is
        invisible to value inspection.
    """
    module_name = getattr(module, "__name__", "")
    annotations = getattr(module, "__annotations__", {})
    found: list[str] = []
    for name, value in list(vars(module).items()):
        if name.startswith("__") and name.endswith("__"):
            continue
        if _defined_in_module(value, module_name) and (
            _is_async_client(value) or _is_async_client(getattr(value, "_client", None))
        ):
            found.append(name)
            continue
        annotation = annotations.get(name)
        if annotation is not None and (
            "AsyncClient" in str(annotation) or "AsyncGroq" in str(annotation)
        ):
            found.append(name)
    return found


def _required_reset_names() -> dict[str, str]:
    """Map each guarded module (dotted path) to its required reset-fn name.

    Single source of truth for BOTH tests in this module: the introspection
    test asserts the reset exists in the module; the wiring test asserts
    ``app/workers/tasks.py`` imports and calls it. A newly added client is
    therefore required in ``app/workers/tasks.py`` with no test edit (same
    self-maintaining property as the D-20 ``pkgutil.walk_packages`` precedent
    in ``tests/test_boundaries.py``).
    """
    _import_all_submodules("app.services")

    prefix = "app.services."
    required: dict[str, str] = {}
    for mod_name, module in list(sys.modules.items()):
        if module is None or not mod_name.startswith(prefix):
            continue
        remainder = mod_name[len(prefix) :]
        if "." in remainder:
            continue  # direct submodules only
        short_name = remainder
        if short_name in _RESET_EXEMPT:
            continue
        if _module_async_client_attrs(module):
            required[mod_name] = f"reset_{short_name}"

    required.update(_EXTRA_GUARDED_RESETS)
    return required


def test_every_async_client_singleton_has_a_reset_function() -> None:
    """Every guarded module defines a callable matching reset function.

    RED on today's code (decision D-05): ``app/services/fred_client.py`` holds
    an ``httpx.AsyncClient`` but defines no matching reset — plan 13-02 adds
    it and flips this GREEN.
    """
    required = _required_reset_names()

    violations: list[str] = []
    for mod_name, reset_name in sorted(required.items()):
        module = importlib.import_module(mod_name)
        client_attrs = _module_async_client_attrs(module)
        acceptable = {reset_name} | {f"reset_{attr}" for attr in client_attrs}
        if not any(callable(getattr(module, cand, None)) for cand in acceptable):
            module_file = mod_name.replace(".", "/") + ".py"
            violations.append(
                f"  - {mod_name}: client-bearing attribute(s) "
                f"{client_attrs or ['<engine singleton, guarded by name>']} have no "
                f"matching reset callable. Add `def {reset_name}() -> None:` to "
                f"{module_file}, mirroring "
                f"app/services/arxiv_client.py::reset_arxiv_client."
            )

    assert not violations, (
        "RESET CONVENTION VIOLATION (DEBT-01, D-01): every module-level singleton "
        "wrapping a loop-bound async client must expose a matching reset_*() "
        "callable in the same module. Offenders:\n" + "\n".join(violations)
    )


def test_reset_exempt_list_is_not_stale() -> None:
    """Each _RESET_EXEMPT module imports and still has no reset_<name>().

    Passes on today's code. Fails the moment an exempt module grows a reset —
    that is the signal to delete its _RESET_EXEMPT entry so the convention
    test starts guarding it.
    """
    stale: list[str] = []
    for short_name in sorted(_RESET_EXEMPT):
        module = importlib.import_module(f"app.services.{short_name}")
        if callable(getattr(module, f"reset_{short_name}", None)):
            stale.append(f"  - app.services.{short_name} now defines reset_{short_name}()")

    assert not stale, (
        "STALE EXEMPT LIST: an exempt module grew a reset — delete its "
        "`_RESET_EXEMPT` entry so the convention test starts guarding it.\n" + "\n".join(stale)
    )


def _parse_tasks_module() -> ast.Module:
    """Read and ``ast.parse`` ``app/workers/tasks.py``.

    On ``SyntaxError`` / ``UnicodeDecodeError`` raise ``AssertionError`` — an
    unparseable file proves nothing about wiring compliance, so it must fail
    the scan rather than return a permissive empty tree (mirrors the
    ``unparseable`` handling in
    ``tests/test_boundaries.py::_find_yfinance_importers``).
    """
    posix_path = _TASKS_MODULE_PATH.as_posix()
    try:
        return ast.parse(_TASKS_MODULE_PATH.read_text(), filename=posix_path)
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise AssertionError(
            f"WIRING SCAN INCOMPLETE: {posix_path} could not be parsed ({exc!r}); "
            "an unparseable file proves nothing about reset-wiring compliance."
        ) from exc


def test_worker_task_calls_every_reset_before_asyncio_run() -> None:
    """app/workers/tasks.py imports AND calls every reset before asyncio.run().

    RED on today's code (decision D-05): the FRED client's reset is neither
    imported nor called in ``run_research_task``. Plan 13-02 wires it and
    flips this GREEN.
    """
    required = _required_reset_names()
    tree = _parse_tasks_module()

    # `imported` / `called` are collected by prefix (not by the required set)
    # on purpose: an unexpected extra reset then shows up in the failure
    # message instead of being silently ignored.
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(
                alias.name for alias in node.names if alias.name.startswith(_RESET_PREFIX)
            )

    entrypoint = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == _TASK_ENTRYPOINT_NAME
        ),
        None,
    )
    assert entrypoint is not None, (
        f"{_TASKS_MODULE_PATH.as_posix()} has no `def {_TASK_ENTRYPOINT_NAME}(...)` — "
        "that is the Celery entrypoint that must call every reset before "
        "asyncio.run(...)."
    )

    called: dict[str, int] = {}
    run_lineno: int | None = None
    for node in ast.walk(entrypoint):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id.startswith(_RESET_PREFIX):
            called.setdefault(func.id, node.lineno)
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "run"
            and isinstance(func.value, ast.Name)
            and func.value.id == "asyncio"
        ):
            run_lineno = node.lineno

    assert run_lineno is not None, (
        f"{_TASKS_MODULE_PATH.as_posix()}::{_TASK_ENTRYPOINT_NAME} has no "
        "`asyncio.run(...)` call — cannot verify resets run before it."
    )

    required_names = sorted(required.values())
    not_imported = [name for name in required_names if name not in imported]
    not_called = [name for name in required_names if name not in called]
    too_late = [name for name in required_names if name in called and called[name] >= run_lineno]

    problems: list[str] = []
    if not_imported:
        problems.append(
            f"  NOT IMPORTED into {_TASKS_MODULE_PATH.as_posix()}: {not_imported}\n"
            "    Fix: add `from app.services.<module> import <reset>` alongside the "
            "existing reset imports."
        )
    if not_called:
        problems.append(
            f"  NEVER CALLED in {_TASK_ENTRYPOINT_NAME}(): {not_called}\n"
            "    Fix: add `<reset>()` to the reset block immediately above "
            "`asyncio.run(...)`. A reset that exists but is never invoked is the "
            "exact 999.1 'Event loop is closed' failure mode (D-04)."
        )
    if too_late:
        problems.append(
            f"  CALLED AFTER asyncio.run(...) (line {run_lineno}): {too_late}\n"
            "    Fix: move the call above `asyncio.run(...)`."
        )

    assert not problems, (
        "RESET WIRING VIOLATION (D-04): app/workers/tasks.py must import AND call "
        "every required reset before its `asyncio.run(...)`.\n"
        f"  required (derived from _required_reset_names()): {required_names}\n"
        f"  imported reset_* names: {sorted(imported)}\n"
        f"  called reset_* names:   {sorted(called)}\n" + "\n".join(problems)
    )

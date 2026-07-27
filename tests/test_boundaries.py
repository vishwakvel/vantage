"""Import guard — CI enforcement of vendor-SDK boundary rules.

Guards two vendor SDKs by two different techniques:

- groq: no module in app/agents/ or app/graph/ may import the 'groq' package
  directly.  All Groq API calls must go through the shared async token-bucket
  rate limiter in app.services.groq_client.  Direct imports bypass the rate
  limiter and risk hitting the 6,000-token/min quota without back-pressure.
  These guards observe sys.modules after walking a package via
  pkgutil.walk_packages — that works because conftest imports app.main before
  the tests run, so re-importing an agent never re-executes an already-cached
  module's own imports.

- yfinance: imports are confined to app/services/ (Phase 9, T-09-BOUNDARY).
  This guard parses source files with ``ast`` instead of relying on
  sys.modules, so its result does not depend on import order or module
  caching — it is deterministic and has zero import side effects.

Design decision D-20: the groq tests walk ALL submodules via
pkgutil.walk_packages so newly added agent files are automatically covered —
there is no need to update the test when a new agent module is added.

Reference: SPEC Requirement 6 AC ("test suite fails if any agent module imports
groq directly"), STRIDE threat T-01-08-01, T-09-BOUNDARY.
"""

import ast
import importlib
import pkgutil
import sys
from pathlib import Path


def _import_all_submodules(package_name: str) -> None:
    """Import every submodule reachable from *package_name*.

    Args:
        package_name: Dotted package path to walk (e.g. ``"app.agents"``).

    If the package does not exist yet (e.g. app/agents/ contains only
    ``__init__.py`` with no submodules), the function returns silently.
    Import errors within individual submodules are swallowed so that an
    unrelated import failure does not hide a genuine boundary violation.
    """
    try:
        package = importlib.import_module(package_name)
    except ModuleNotFoundError:
        return  # Package absent — nothing to guard yet

    package_path = getattr(package, "__path__", None)
    if package_path is None:
        return

    for _importer, module_name, _is_pkg in pkgutil.walk_packages(
        path=package_path,
        prefix=package_name + ".",
        onerror=lambda name: None,
    ):
        try:
            importlib.import_module(module_name)
        except Exception:
            pass  # Don't let unrelated import errors mask boundary violations


def test_no_groq_import_in_agents() -> None:
    """app/agents/ modules must NOT import 'groq' directly.

    Walks all submodules of app.agents and asserts that the 'groq' package was
    not added to sys.modules as a side effect.  Fails CI the moment any agent
    module adds ``import groq`` or ``from groq import ...``.
    """
    sys.modules.pop("groq", None)  # Clean slate — remove any prior import

    _import_all_submodules("app.agents")

    assert "groq" not in sys.modules, (
        "BOUNDARY VIOLATION: a module in app/agents/ imported 'groq' directly.\n"
        "All Groq API calls must go through app.services.groq_client.groq_rate_limiter\n"
        "so that the shared async token-bucket rate limiter is always applied."
    )


def test_no_groq_import_in_graph() -> None:
    """app/graph/ modules must NOT import 'groq' directly.

    Same guard as test_no_groq_import_in_agents but applied to the LangGraph
    graph construction layer (app/graph/).
    """
    sys.modules.pop("groq", None)  # Clean slate

    _import_all_submodules("app.graph")

    assert "groq" not in sys.modules, (
        "BOUNDARY VIOLATION: a module in app/graph/ imported 'groq' directly.\n"
        "All Groq API calls must go through app.services.groq_client.groq_rate_limiter\n"
        "so that the shared async token-bucket rate limiter is always applied."
    )


_EXPECTED_YFINANCE_IMPORTERS: frozenset[str] = frozenset(
    {
        "app/services/comparables_source.py",
        "app/services/financial_metrics_source.py",
    }
)


def _find_yfinance_importers(root: str = "app") -> set[str]:
    """Return the POSIX relative paths of files under *root* importing yfinance.

    Parses each ``*.py`` file with ``ast.parse`` and collects files containing
    either an ``ast.Import`` whose alias name is ``yfinance`` (or starts with
    ``yfinance.``) or an ``ast.ImportFrom`` whose module is ``yfinance`` (or
    starts with ``yfinance.``). A file that fails to parse is recorded as an
    unparseable path rather than silently treated as compliant — an
    unparseable file means the scan proved nothing about it.

    Raises:
        AssertionError: if any file under *root* fails to parse, naming the
            unparseable file(s) so the failure is self-diagnosing.
    """
    importers: set[str] = set()
    unparseable: list[str] = []

    for path in sorted(Path(root).rglob("*.py")):
        posix_path = path.as_posix()
        try:
            source = path.read_text()
            tree = ast.parse(source, filename=posix_path)
        except (SyntaxError, UnicodeDecodeError):
            unparseable.append(posix_path)
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(
                    alias.name == "yfinance" or alias.name.startswith("yfinance.")
                    for alias in node.names
                ):
                    importers.add(posix_path)
            elif isinstance(node, ast.ImportFrom):
                if node.module is not None and (
                    node.module == "yfinance" or node.module.startswith("yfinance.")
                ):
                    importers.add(posix_path)

    assert not unparseable, (
        "BOUNDARY SCAN INCOMPLETE: the following files failed to parse and\n"
        "could not be verified for yfinance imports (an unparseable file\n"
        "proves nothing about compliance):\n" + "\n".join(unparseable)
    )

    return importers


def test_yfinance_imports_confined_to_services() -> None:
    """yfinance imports are confined to the exact expected set of files.

    Parses every *.py file under app/ with ast (no import side effects, no
    dependency on module caching or import order) and asserts the set of
    yfinance-importing files equals exactly
    {app/services/comparables_source.py, app/services/financial_metrics_source.py}.
    Adding a third importer, or moving either module, fails this test.
    """
    actual = _find_yfinance_importers()

    unexpected = actual - _EXPECTED_YFINANCE_IMPORTERS
    missing = _EXPECTED_YFINANCE_IMPORTERS - actual

    assert actual == _EXPECTED_YFINANCE_IMPORTERS, (
        "BOUNDARY VIOLATION: yfinance imports must be confined to exactly\n"
        f"{sorted(_EXPECTED_YFINANCE_IMPORTERS)}.\n"
        "Agents and graph modules must import the comparables_source /\n"
        "financial_metrics_source singletons and never import yfinance\n"
        "directly.\n"
        f"Unexpected importers: {sorted(unexpected)}\n"
        f"Missing expected importers: {sorted(missing)}"
    )

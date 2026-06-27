"""Import guard — CI enforcement of the Groq rate-limiter boundary.

Verifies that no module in app/agents/ or app/graph/ imports the 'groq' package
directly.  All Groq API calls must go through the shared async token-bucket rate
limiter in app.services.groq_client.  Direct imports bypass the rate limiter and
risk hitting the 6,000-token/min quota without back-pressure.

Design decision D-20: this test walks ALL submodules via pkgutil.walk_packages so
newly added agent files are automatically covered — there is no need to update the
test when a new agent module is added.

Reference: SPEC Requirement 6 AC ("test suite fails if any agent module imports
groq directly"), STRIDE threat T-01-08-01.
"""

import importlib
import pkgutil
import sys


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

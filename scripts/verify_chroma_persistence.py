"""DEPLOY-04 acceptance harness — proves ChromaDB survives a machine restart.

This is the live link no unit or integration test can cover: that bytes
written to the vector store through the application's own store-and-embed
path survive the destruction and recreation of the ChromaDB container
process. That depends entirely on the container's data directory being
mounted at the exact path the pinned image persists to — and it fails
*silently* rather than loudly when it is wrong: writes succeed, the
heartbeat stays green, retrieval works right up until the restart, and then
the collection is simply gone with no error anywhere. A green config review
is not evidence. This script is the evidence.

Prerequisites:
    - The target stack must already be running and reachable — either the
      local docker-compose ``chromadb`` service, or the hosted
      ``vantage-chroma`` Fly app on its private network.
    - The environment must carry ``DATABASE_URL``, ``JWT_SECRET_KEY``, and
      ``GROQ_API_KEY`` — constructing a ``Settings`` instance validates all
      three even though this script calls none of them.
    - ``CHROMADB_HOST`` / ``CHROMADB_PORT`` (and optionally
      ``CHROMADB_COLLECTION``) must point at the stack under test.

Invocation:
    # Local docker-compose stack, scripted restart via --sleep-seconds:
    docker compose up -d chromadb
    .venv/bin/python scripts/verify_chroma_persistence.py --sleep-seconds 5

    # Hosted stack — operator drives the restart in another terminal:
    CHROMADB_HOST=vantage-chroma.flycast CHROMADB_PORT=8000 \\
        .venv/bin/python scripts/verify_chroma_persistence.py

This script is deliberately NOT a pytest test, is NOT collected by pytest, is
NOT imported by any application module, and is NEVER referenced from any CI
configuration.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time
import uuid

# Standalone-script bootstrap: make ``app`` importable when this file is run
# directly (``python scripts/verify_chroma_persistence.py``), since Python
# puts the script's own directory on ``sys.path``, not the repo root.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402 - import must follow the bootstrap
from app.services import vector_store  # noqa: E402

#: Public scope value the module documents for chunks with no owning user
#: (see ``app/services/vector_store.py::dense_query``) — the module rejects
#: a ``None`` metadata value outright, so this is the only legal "no user"
#: marker.
_PUBLIC_SCOPE = ""


class PersistenceCheckFailure(AssertionError):
    """Raised when a live assertion fails — caught once at the top level."""


def _check(condition: bool, description: str) -> None:
    """Assert-and-print helper — raises PersistenceCheckFailure on failure."""
    if not condition:
        raise PersistenceCheckFailure(f"FAILED: {description}")
    print(f"  [OK] {description}")


def _collection_count() -> int:
    """Return the current item count of the module's lazy collection."""
    collection = (
        vector_store._get_chroma_collection()
    )  # noqa: SLF001 - same lazy accessor the app uses
    return collection.count()  # type: ignore[no-any-return]


def _reset_singletons() -> None:
    """Drop the module-level lazy client/collection so the next call reconnects.

    After the operator restarts the ChromaDB machine, the old HTTP client may
    still hold a socket to a process that no longer exists. Resetting these
    singletons forces a fresh connection on the next query rather than
    letting a stale-socket error masquerade as a data-loss failure (the
    distinction step five's checks depend on).
    """
    vector_store.chroma_client = None
    vector_store.vantage_collection = None


def run_verification(marker: str, sleep_seconds: int | None) -> None:
    settings = get_settings()

    # --- Step 1: connect and report ----------------------------------------
    print("\n=== Step 1: connect and report ===")
    print(f"  host: {settings.CHROMADB_HOST}")
    print(f"  port: {settings.CHROMADB_PORT}")
    print(f"  collection: {settings.CHROMADB_COLLECTION}")
    count_before = _collection_count()
    _check(count_before >= 0, "collection is reachable")
    print(f"  item count before ingest: {count_before}")

    # --- Step 2: ingest a marker --------------------------------------------
    print(f"\n=== Step 2: ingest a marker (id={marker}) ===")
    vector_store.embed_and_store(
        ids=[marker],
        texts=[f"DEPLOY-04 persistence verification marker {marker}"],
        metadatas=[{"marker": marker, "user_id": _PUBLIC_SCOPE}],
    )
    count_after_ingest = _collection_count()
    _check(
        count_after_ingest >= count_before + 1,
        f"item count rose after ingest (before={count_before}, " f"after={count_after_ingest})",
    )
    print(f"  item count after ingest: {count_after_ingest}")

    # --- Step 3: prove it is retrievable now --------------------------------
    print("\n=== Step 3: prove it is retrievable now (control) ===")
    results_now = vector_store.dense_query(marker, user_id=_PUBLIC_SCOPE, n_results=20)
    ids_now = results_now.get("ids", [[]])[0]
    _check(
        marker in ids_now,
        f"marker {marker!r} is present in the query result BEFORE restart "
        "(if this fails, the problem is ingestion, not persistence — stop here)",
    )
    print(f"  item count: {_collection_count()}")

    # --- Step 4: restart, under operator control ----------------------------
    print("\n=== Step 4: restart the ChromaDB machine ===")
    if sleep_seconds is not None:
        print(
            "  --sleep-seconds set: skipping the operator prompt. Make sure "
            "the target stack's own restart happens during this sleep "
            "(e.g. `docker compose restart chromadb` in another terminal)."
        )
        print(f"  sleeping {sleep_seconds}s ...")
        time.sleep(sleep_seconds)
    else:
        print(
            "  ------------------------------------------------------------\n"
            "  In another terminal, against the stack under test:\n"
            "\n"
            "    fly machine list --app vantage-chroma\n"
            "    fly machine restart <machine-id> --app vantage-chroma\n"
            "\n"
            "  Wait for `fly machine list` to show the restarted machine's\n"
            "  health check green again before continuing.\n"
            "  ------------------------------------------------------------"
        )
        input("  Press Enter once the restart is complete and healthy: ")

    # --- Step 5: prove it survived -------------------------------------------
    print("\n=== Step 5: prove it survived the restart ===")
    _reset_singletons()
    results_after = vector_store.dense_query(marker, user_id=_PUBLIC_SCOPE, n_results=20)
    ids_after = results_after.get("ids", [[]])[0]
    _check(
        marker in ids_after,
        f"marker {marker!r} is present in the query result AFTER restart",
    )
    count_after_restart = _collection_count()
    _check(
        count_after_restart >= count_after_ingest,
        f"item count did not drop after restart (before={count_after_ingest}, "
        f"after={count_after_restart}) — a collapse to zero is the signature "
        "silent-persistence-loss failure: check that the ChromaDB container's "
        "mount path matches the volume's mount destination and that the "
        "persistence flag for this image version is set",
    )
    print(f"  item count after restart: {count_after_restart}")

    print("\n=== VERDICT: PASS — the document survived the restart ===")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DEPLOY-04 acceptance harness — verify ChromaDB persistence "
        "across a machine restart."
    )
    parser.add_argument(
        "--sleep-seconds",
        type=int,
        default=None,
        help="Skip the operator prompt and sleep this many seconds instead "
        "(for a scripted local docker-compose restart). Default: prompt and wait.",
    )
    args = parser.parse_args()

    marker = f"deploy-04-{uuid.uuid4()}"

    try:
        run_verification(marker, args.sleep_seconds)
    except PersistenceCheckFailure as exc:
        print(f"\n=== FAIL ===\n{exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - top-level script boundary
        print(f"\n=== FAIL (unexpected error) ===\n{exc!r}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()

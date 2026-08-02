"""Offline evaluation assets for RAGAS retrieval-quality scoring (OBS-03).

This package holds the hand-curated golden set consumed by the scheduled
RAGAS evaluation Celery beat task (plan 12-12) and the validating loader that
guards its shape.

The golden-set JSON file (``ragas_golden_set.json``) lives inside this
application package rather than under ``tests/`` even though it was
hand-authored the way a test fixture would be: it is loaded at *runtime* by
an unattended background worker task, not by pytest, so it is application
data the running system depends on, not test-only data. This is the
documented resolution of D-07's "planner's call on exact location/format".
"""

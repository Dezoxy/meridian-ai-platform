"""Seeded generator for the synthetic data and golden set of Meridian Insurance.

Meridian Insurance is fictional. The generator writes policies, claim history,
four policy wordings and 40 first-notice-of-loss claims with labelled expected
outcomes. Everything derives from one seeded ``random.Random`` and one product
catalogue, so a rerun with the same seed is byte-identical.

Run it from the repository root:
``PYTHONPATH=data/synthetic uv run python -m generator``.
"""

GENERATOR_VERSION = "1"

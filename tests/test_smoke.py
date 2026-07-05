"""Placeholder keeping CI green until Phase 0 lands real code and tests.

Without at least one test, `pytest .` exits with code 5 (no tests
collected) and the workflow fails.
"""
import hrusha


def test_package_imports():
    assert hrusha is not None

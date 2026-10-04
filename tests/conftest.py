"""Shared pytest fixtures.

The suite's ownership assertions ("mine" vs "waiting") depend on the user
identity, which normally comes from config.yaml — a gitignored, per-machine
file. Pin a deterministic identity for the whole test session so the suite is
hermetic on any machine and in CI (where config.yaml is seeded from the
example, whose user name is empty).

notes_index caches a compiled "is this me?" matcher at import time, so we
rebuild it after overriding the identity (and restore both on teardown).
"""

import pytest

from src.config import CONFIG
from src import notes_index as ni


@pytest.fixture(autouse=True, scope="session")
def _pin_user_identity():
    original_user = CONFIG.get("user")
    original_matcher = ni._ME_MATCHER
    CONFIG["user"] = {"name": "Steven", "aliases": "Steve, Steven Heller, me, myself"}
    ni._ME_MATCHER = ni._build_me_matcher()
    yield
    if original_user is None:
        CONFIG.pop("user", None)
    else:
        CONFIG["user"] = original_user
    ni._ME_MATCHER = original_matcher

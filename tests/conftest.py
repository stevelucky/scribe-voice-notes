"""Shared pytest fixtures.

The suite's ownership assertions ("mine" vs "waiting") depend on the user
identity, which normally comes from config.yaml — a gitignored, per-machine
file. Pin a deterministic identity for the whole test session so the suite is
hermetic on any machine and in CI (where config.yaml is seeded from the
example, whose user name is empty).

notes_index rebuilds its "is this me?" matcher whenever the identity changes,
so overriding CONFIG["user"] here is enough — no cache poking needed.
"""

import pytest

from src.config import CONFIG


@pytest.fixture(autouse=True, scope="session")
def _pin_user_identity():
    original_user = CONFIG.get("user")
    CONFIG["user"] = {"name": "Steven", "aliases": "Steve, Steven Heller, me, myself"}
    yield
    if original_user is None:
        CONFIG.pop("user", None)
    else:
        CONFIG["user"] = original_user

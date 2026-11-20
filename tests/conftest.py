import pytest


@pytest.fixture(autouse=True)
def _grant_mechanism_tests_start_with_the_everyone_switch_off(monkeypatch):
    """The older tests check the per-device grant (no grant = no solver). That is exactly the world with
    the admin's 'solve for everyone' switch off, so they run in it. Tests of the switch turn it on
    themselves with db.set_everyone(True)."""
    from papaya import db
    monkeypatch.setattr(db, "SOLVE_EVERYONE_DEFAULT", "0")

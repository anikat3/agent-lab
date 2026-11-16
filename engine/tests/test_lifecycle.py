from datetime import UTC, datetime, timedelta

import pytest

from app import lifecycle as lc

ALL = [lc.QUEUED, lc.RUNNING, lc.COMPLETED, lc.FAILED, lc.CANCELLED]

ALLOWED = {
    (lc.QUEUED, lc.RUNNING),
    (lc.QUEUED, lc.FAILED),
    (lc.QUEUED, lc.CANCELLED),
    (lc.RUNNING, lc.COMPLETED),
    (lc.RUNNING, lc.FAILED),
    (lc.RUNNING, lc.CANCELLED),
}


@pytest.mark.parametrize("current", ALL)
@pytest.mark.parametrize("new", ALL)
def test_transition_matrix(current, new):
    assert lc.can_transition(current, new) == ((current, new) in ALLOWED)


def test_terminal_states_have_no_exits():
    assert {s for s in ALL if lc.is_terminal(s)} == {lc.COMPLETED, lc.FAILED, lc.CANCELLED}
    assert not lc.can_transition(lc.COMPLETED, lc.COMPLETED)  # not even to themselves


def test_unknown_status_is_never_valid():
    assert not lc.can_transition("pending", lc.RUNNING)  # app-only status
    assert not lc.is_terminal("bogus")


def test_sources_for():
    assert lc.sources_for(lc.RUNNING) == {lc.QUEUED}
    assert lc.sources_for(lc.COMPLETED) == {lc.RUNNING}
    assert lc.sources_for(lc.FAILED) == {lc.QUEUED, lc.RUNNING}
    assert lc.sources_for(lc.QUEUED) == set()


def test_remaining_seconds_is_measured_from_started_at():
    started = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    assert lc.remaining_seconds(started, 300, started) == 300
    assert lc.remaining_seconds(started, 300, started + timedelta(seconds=120)) == 180
    assert lc.remaining_seconds(started, 300, started + timedelta(seconds=301)) == -1

"""Tests for the Session data model and state machine."""

import pytest

from substrat.session import Session, SessionState, SessionStateError


def test_session_defaults() -> None:
    s = Session()
    assert s.state == SessionState.CREATED
    assert s.provider_name == ""
    assert s.created_at != ""
    assert s.provider_state == b""


def test_activate_from_created() -> None:
    s = Session()
    s.activate()
    assert s.state == SessionState.ACTIVE


def test_suspend_from_active() -> None:
    s = Session()
    s.activate()
    s.suspend(b"blob")
    assert s.state == SessionState.SUSPENDED
    assert s.provider_state == b"blob"
    assert s.suspended_at is not None


def test_resume_from_suspended() -> None:
    s = Session()
    s.activate()
    s.suspend(b"blob")
    s.activate()
    assert s.state == SessionState.ACTIVE
    assert s.suspended_at is None


def test_terminate_from_active() -> None:
    s = Session()
    s.activate()
    s.terminate()
    assert s.state == SessionState.TERMINATED


def test_terminate_from_suspended() -> None:
    s = Session()
    s.activate()
    s.suspend(b"")
    s.terminate()
    assert s.state == SessionState.TERMINATED


def test_cannot_activate_terminated() -> None:
    s = Session()
    s.activate()
    s.terminate()
    with pytest.raises(SessionStateError):
        s.activate()


def test_cannot_suspend_created() -> None:
    s = Session()
    with pytest.raises(SessionStateError):
        s.suspend(b"")


def test_cannot_suspend_terminated() -> None:
    s = Session()
    s.activate()
    s.terminate()
    with pytest.raises(SessionStateError):
        s.suspend(b"")


def test_cannot_double_activate() -> None:
    s = Session()
    s.activate()
    with pytest.raises(SessionStateError):
        s.activate()


def test_terminated_is_terminal() -> None:
    s = Session()
    s.activate()
    s.terminate()
    with pytest.raises(SessionStateError):
        s.terminate()


def _check_state(s: Session, expected: SessionState) -> None:
    assert s.state == expected


def test_full_lifecycle() -> None:
    s = Session(provider_name="test", model="gpt-4")
    _check_state(s, SessionState.CREATED)
    s.activate()
    _check_state(s, SessionState.ACTIVE)
    s.suspend(b"state-1")
    _check_state(s, SessionState.SUSPENDED)
    assert s.provider_state == b"state-1"
    s.activate()
    _check_state(s, SessionState.ACTIVE)
    s.suspend(b"state-2")
    assert s.provider_state == b"state-2"
    s.terminate()
    _check_state(s, SessionState.TERMINATED)

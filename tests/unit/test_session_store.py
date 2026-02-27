# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the persistent session store."""

from pathlib import Path
from uuid import uuid4

import pytest

from substrat.session import Session, SessionState, SessionStore


@pytest.fixture()
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path)


def _make_session(**kwargs: object) -> Session:
    defaults: dict[str, object] = {
        "provider_name": "cursor-agent",
        "model": "sonnet-4.6",
    }
    defaults.update(kwargs)
    return Session(**defaults)  # type: ignore[arg-type]


def test_save_load_roundtrip(store: SessionStore) -> None:
    s = _make_session(provider_state=b"\x00\xff binary blob")
    store.save(s)
    loaded = store.load(s.id)
    assert loaded.id == s.id
    assert loaded.state == s.state
    assert loaded.provider_name == s.provider_name
    assert loaded.model == s.model
    assert loaded.created_at == s.created_at
    assert loaded.suspended_at == s.suspended_at
    assert loaded.provider_state == s.provider_state


def test_save_overwrites_on_state_change(store: SessionStore) -> None:
    s = _make_session()
    store.save(s)
    s.activate()
    store.save(s)
    loaded = store.load(s.id)
    assert loaded.state == SessionState.ACTIVE


def test_scan_multiple(store: SessionStore) -> None:
    a, b = _make_session(), _make_session()
    store.save(a)
    store.save(b)
    found = store.scan()
    ids = {s.id for s in found}
    assert ids == {a.id, b.id}


def test_scan_empty_dir(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    assert store.scan() == []


def test_scan_missing_dir(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "nonexistent")
    assert store.scan() == []


def test_load_missing_raises(store: SessionStore) -> None:
    with pytest.raises(FileNotFoundError):
        store.load(uuid4())


def test_recover_flips_active_to_suspended(store: SessionStore) -> None:
    active = _make_session()
    active.activate()
    store.save(active)
    created = _make_session()
    store.save(created)
    suspended = _make_session()
    suspended.activate()
    suspended.suspend(b"blob")
    store.save(suspended)
    terminated = _make_session()
    terminated.activate()
    terminated.terminate()
    store.save(terminated)
    recovered = store.recover()
    by_id = {s.id: s for s in recovered}
    assert by_id[active.id].state == SessionState.SUSPENDED
    assert by_id[created.id].state == SessionState.CREATED
    assert by_id[suspended.id].state == SessionState.SUSPENDED
    assert by_id[terminated.id].state == SessionState.TERMINATED


def test_recover_persists_flipped_state(store: SessionStore) -> None:
    s = _make_session()
    s.activate()
    store.save(s)
    store.recover()
    loaded = store.load(s.id)
    assert loaded.state == SessionState.SUSPENDED


def test_agent_dir(store: SessionStore) -> None:
    sid = uuid4()
    assert store.agent_dir(sid) == store._root / sid.hex


def test_tmp_files_ignored_by_scan(store: SessionStore) -> None:
    """Stale .tmp files from interrupted writes don't break scan."""
    s = _make_session()
    store.save(s)
    # Drop a .tmp file that isn't a directory.
    (store._root / "not-a-session.tmp").write_bytes(b"junk")
    found = store.scan()
    assert len(found) == 1
    assert found[0].id == s.id

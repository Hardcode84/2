# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the persistent workspace store."""

from pathlib import Path
from uuid import uuid4

import pytest

from substrat.workspace import LinkSpec, Workspace, WorkspaceStore, validate_name


@pytest.fixture()
def store(tmp_path: Path) -> WorkspaceStore:
    return WorkspaceStore(tmp_path)


def _make_workspace(**kwargs: object) -> Workspace:
    defaults: dict[str, object] = {
        "name": "test-ws",
        "scope": uuid4(),
        "root_path": Path("/tmp/backing"),
    }
    defaults.update(kwargs)
    return Workspace(**defaults)  # type: ignore[arg-type]


# --- roundtrip ---


def test_save_load_roundtrip(store: WorkspaceStore) -> None:
    ws = _make_workspace()
    store.save(ws)
    loaded = store.load(ws.scope, ws.name)
    assert loaded.name == ws.name
    assert loaded.scope == ws.scope
    assert loaded.root_path == ws.root_path
    assert loaded.network_access == ws.network_access
    assert loaded.links == ws.links
    assert loaded.created_at == ws.created_at


def test_save_with_links(store: WorkspaceStore) -> None:
    ws = _make_workspace(
        links=[
            LinkSpec(
                host_path=Path("/home/user/src"), mount_path=Path("src"), mode="ro"
            ),
            LinkSpec(host_path=Path("/data"), mount_path=Path("data"), mode="rw"),
        ]
    )
    store.save(ws)
    loaded = store.load(ws.scope, ws.name)
    assert len(loaded.links) == 2
    assert loaded.links[0].host_path == Path("/home/user/src")
    assert loaded.links[0].mount_path == Path("src")
    assert loaded.links[0].mode == "ro"
    assert loaded.links[1].mode == "rw"


# --- backing dir ---


def test_save_creates_backing_dir(store: WorkspaceStore) -> None:
    ws = _make_workspace()
    store.save(ws)
    backing = store.workspace_dir(ws.scope, ws.name) / "root"
    assert backing.is_dir()


# --- scan ---


def test_scan_multiple(store: WorkspaceStore) -> None:
    scope_a, scope_b = uuid4(), uuid4()
    a = _make_workspace(name="ws-a", scope=scope_a)
    b = _make_workspace(name="ws-b", scope=scope_b)
    store.save(a)
    store.save(b)
    found = store.scan()
    names = {ws.name for ws in found}
    assert names == {"ws-a", "ws-b"}


def test_scan_empty(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    assert store.scan() == []


def test_scan_missing_root(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path / "nonexistent")
    assert store.scan() == []


# --- load errors ---


def test_load_missing_raises(store: WorkspaceStore) -> None:
    with pytest.raises(FileNotFoundError):
        store.load(uuid4(), "nope")


# --- delete ---


def test_delete_removes_dir(store: WorkspaceStore) -> None:
    ws = _make_workspace()
    store.save(ws)
    assert store.exists(ws.scope, ws.name)
    store.delete(ws.scope, ws.name)
    assert not store.exists(ws.scope, ws.name)
    assert not store.workspace_dir(ws.scope, ws.name).exists()


def test_delete_missing_raises(store: WorkspaceStore) -> None:
    with pytest.raises(FileNotFoundError):
        store.delete(uuid4(), "ghost")


# --- exists ---


def test_exists(store: WorkspaceStore) -> None:
    ws = _make_workspace()
    assert not store.exists(ws.scope, ws.name)
    store.save(ws)
    assert store.exists(ws.scope, ws.name)


# --- scope isolation ---


def test_scope_isolation(store: WorkspaceStore) -> None:
    scope_x, scope_y = uuid4(), uuid4()
    a = _make_workspace(name="shared", scope=scope_x)
    b = _make_workspace(name="shared", scope=scope_y)
    store.save(a)
    store.save(b)
    loaded_a = store.load(scope_x, "shared")
    loaded_b = store.load(scope_y, "shared")
    assert loaded_a.scope == scope_x
    assert loaded_b.scope == scope_y


# --- junk files ---


def test_tmp_files_ignored_by_scan(store: WorkspaceStore) -> None:
    """Stale .tmp files from interrupted writes don't break scan."""
    ws = _make_workspace()
    store.save(ws)
    # Drop junk at the root level and inside a scope dir.
    (store._root / "leftover.tmp").write_bytes(b"junk")
    scope_dir = store._root / ws.scope.hex
    (scope_dir / "not-a-workspace.tmp").write_bytes(b"junk")
    found = store.scan()
    assert len(found) == 1
    assert found[0].name == ws.name


# --- name validation ---


@pytest.mark.parametrize(
    "bad_name",
    [
        "",
        ".",
        "..",
        "../etc",
        "foo/bar",
        "foo\\bar",
        "-starts-with-dash",
        "_starts-with-underscore",
        "has spaces",
        "has\x00null",
    ],
)
def test_validate_name_rejects_bad_names(bad_name: str) -> None:
    with pytest.raises(ValueError, match="invalid workspace name"):
        validate_name(bad_name)


@pytest.mark.parametrize(
    "good_name",
    ["env", "worker-env", "ws_01", "A", "my-workspace-2"],
)
def test_validate_name_accepts_good_names(good_name: str) -> None:
    validate_name(good_name)  # Should not raise.


def test_save_rejects_path_traversal(store: WorkspaceStore) -> None:
    ws = _make_workspace(name="../../etc")
    with pytest.raises(ValueError, match="invalid workspace name"):
        store.save(ws)


def test_delete_rejects_path_traversal(store: WorkspaceStore) -> None:
    with pytest.raises(ValueError, match="invalid workspace name"):
        store.delete(uuid4(), "../../../tmp")

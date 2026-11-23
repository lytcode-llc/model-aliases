"""Tests for model_aliases.state — cache persistence and merge/drift logic."""

from __future__ import annotations

import json
import os

from pathlib import Path

from model_aliases.state import (
    Resolution,
    cache_path,
    load,
    merge,
    save,
)


def _res(resolved: dict[str, str], updated_at: float = 1.0, history: list[dict] | None = None) -> Resolution:
    return Resolution(resolved=resolved, updated_at=updated_at, history=history or [])


# --- roundtrip / load failures -------------------------------------------


def test_save_load_roundtrip(tmp_path: Path) -> None:
    history = [{"alias": "a", "from": None, "to": "m1", "at": 5.0}]
    original = _res({"a": "m1", "b": "m2"}, updated_at=123.5, history=history)
    save(tmp_path, "prof", original)
    loaded = load(tmp_path, "prof")
    assert loaded is not None
    assert loaded.resolved == original.resolved
    assert loaded.updated_at == 123.5
    assert loaded.history == history


def test_load_missing_file(tmp_path: Path) -> None:
    assert load(tmp_path, "nope") is None


def test_load_corrupt_json(tmp_path: Path) -> None:
    path = cache_path(tmp_path, "prof")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert load(tmp_path, "prof") is None


def test_load_wrong_version(tmp_path: Path) -> None:
    path = cache_path(tmp_path, "prof")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 99, "updated_at": 1.0, "resolved": {}, "history": []}),
        encoding="utf-8",
    )
    assert load(tmp_path, "prof") is None


def test_load_wrong_shape(tmp_path: Path) -> None:
    path = cache_path(tmp_path, "prof")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "updated_at": 1.0, "resolved": ["not", "a", "dict"], "history": []}),
        encoding="utf-8",
    )
    assert load(tmp_path, "prof") is None


# --- save mechanics -------------------------------------------------------


def test_save_creates_nested_dirs_and_is_atomic(tmp_path: Path) -> None:
    cache_dir = tmp_path / "deep" / "nested"
    save(cache_dir, "prof", _res({"a": "m1"}, updated_at=9.0))
    path = cache_path(cache_dir, "prof")
    assert path.exists()
    # valid JSON on disk
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["version"] == 1
    assert on_disk["resolved"] == {"a": "m1"}
    # no temp file left behind
    assert not path.with_name(path.name + ".tmp").exists()


# --- merge ----------------------------------------------------------------


def test_merge_changed_winner_drifts() -> None:
    prev = _res({"a": "old"}, updated_at=1.0)
    merged, events = merge(prev, {"a": "new"}, now=42.0)
    assert len(events) == 1
    assert events[0] == {"alias": "a", "from": "old", "to": "new", "at": 42.0}
    assert merged.resolved == {"a": "new"}
    assert merged.updated_at == 42.0
    assert merged.history == events


def test_merge_unchanged_winner_no_event() -> None:
    prev = _res({"a": "same"}, updated_at=1.0, history=[{"alias": "x", "from": None, "to": "y", "at": 0.5}])
    merged, events = merge(prev, {"a": "same"}, now=2.0)
    assert events == []
    assert merged.resolved == {"a": "same"}
    assert len(merged.history) == 1  # prev history kept, nothing added


def test_merge_none_keeps_last_known_good() -> None:
    prev = _res({"a": "keeper"}, updated_at=1.0)
    merged, events = merge(prev, {"a": None}, now=2.0)
    assert events == []
    assert merged.resolved == {"a": "keeper"}


def test_merge_none_without_prev_absent() -> None:
    merged, events = merge(None, {"a": None}, now=2.0)
    assert events == []
    assert "a" not in merged.resolved


def test_merge_prev_only_keys_retained() -> None:
    prev = _res({"keep": "m1", "stale": "m2"}, updated_at=1.0)
    merged, events = merge(prev, {"keep": "m1"}, now=2.0)
    assert events == []
    assert merged.resolved == {"keep": "m1", "stale": "m2"}


def test_merge_history_capped_at_20() -> None:
    seed = [{"alias": f"a{i}", "from": None, "to": f"m{i}", "at": float(i)} for i in range(19)]
    prev = _res({"x": "m0", "y": "m0"}, updated_at=0.0, history=seed)
    merged, events = merge(prev, {"x": "mX", "y": "mY"}, now=100.0)
    assert len(events) == 2
    assert len(merged.history) == 20
    # oldest dropped: seed entry for a0 gone, seed a1 first, newest events last
    assert merged.history[0]["alias"] == "a1"
    assert merged.history[-1] == {"alias": "y", "from": "m0", "to": "mY", "at": 100.0}


# --- cache_path -----------------------------------------------------------


def test_cache_path_sanitizes_profile_name(tmp_path: Path) -> None:
    path = cache_path(tmp_path, "alias:OC/1")
    assert path.parent == tmp_path
    assert os.sep not in path.name
    assert "/" not in path.name
    # [^a-z0-9._-] replaces ':', 'O', 'C', '/' alike
    assert path.name == "alias____1.json"

"""Tests for model_aliases.catalog — endpoint parsing, live fetch, candidate merging."""

from __future__ import annotations

from model_aliases import catalog
from model_aliases.catalog import (
    ProviderDescriptor,
    fetch_live_ids,
    fetch_models_dev,
    live_candidates,
    merge_candidates,
    models_dev_candidates,
    parse_models_endpoint,
)
from model_aliases.resolver import Candidate


# --- parse_models_endpoint -------------------------------------------------


def test_parse_data_list() -> None:
    payload = {"data": [{"id": "a"}, {"id": "b", "context_length": 128}]}
    assert parse_models_endpoint(payload) == [("a", None), ("b", 128)]


def test_parse_models_key_with_name() -> None:
    assert parse_models_endpoint({"models": [{"name": "m1"}]}) == [("m1", None)]


def test_parse_plain_list_mixed_items() -> None:
    assert parse_models_endpoint(["x", {"model": "y"}]) == [("x", None), ("y", None)]


def test_parse_garbage() -> None:
    assert parse_models_endpoint(None) == []
    assert parse_models_endpoint("nope") == []
    assert parse_models_endpoint(42) == []
    assert parse_models_endpoint({"other": []}) == []
    assert parse_models_endpoint([None, 3, {}]) == []


def test_parse_dedupe_first_wins() -> None:
    payload = {"data": [{"id": "a", "context_length": 10}, {"id": "a", "context_length": 99}]}
    assert parse_models_endpoint(payload) == [("a", 10)]


def test_parse_context_priority_and_payload_default() -> None:
    payload = {
        "context_length": 77,
        "models": [
            {"id": "p", "context_length": 1, "context_window": 2, "max_input_tokens": 3},
            {"id": "w", "context_window": 2, "max_input_tokens": 3},
            {"id": "t", "max_input_tokens": 3},
            {"id": "d"},
        ],
    }
    assert parse_models_endpoint(payload) == [("p", 1), ("w", 2), ("t", 3), ("d", 77)]


# --- fetch_live_ids --------------------------------------------------------


def test_fetch_live_no_endpoint() -> None:
    assert fetch_live_ids(ProviderDescriptor(name="x")) is None
    assert fetch_live_ids(ProviderDescriptor(name="x", base_url="", models_url="")) is None


def test_fetch_live_rows(monkeypatch) -> None:
    seen: dict = {}

    def fake(url: str, *, headers=None, timeout: float = 3.0) -> object:
        seen["url"] = url
        seen["headers"] = headers
        return {"data": [{"id": "a"}]}

    monkeypatch.setattr(catalog, "_fetch_json", fake)
    rows = fetch_live_ids(ProviderDescriptor(name="x", base_url="https://api.x.com/v1/"))
    assert rows == [("a", None)]
    assert seen["url"] == "https://api.x.com/v1/models"


def test_fetch_live_failure_returns_none(monkeypatch) -> None:
    def boom(url: str, *, headers=None, timeout: float = 3.0) -> object:
        raise OSError("network down")

    monkeypatch.setattr(catalog, "_fetch_json", boom)
    assert fetch_live_ids(ProviderDescriptor(name="x", base_url="https://x")) is None


def test_fetch_live_bearer_header_when_env_present(monkeypatch) -> None:
    captured: dict = {}

    def fake(url: str, *, headers=None, timeout: float = 3.0) -> object:
        captured["headers"] = headers
        return {"data": []}

    monkeypatch.setattr(catalog, "_fetch_json", fake)
    monkeypatch.setenv("XCO_KEY", "sekrit")
    rows = fetch_live_ids(
        ProviderDescriptor(name="x", base_url="https://x", api_key_env="XCO_KEY")
    )
    assert rows == []
    assert captured["headers"] == {"Authorization": "Bearer sekrit"}


def test_fetch_live_no_auth_header_when_env_missing(monkeypatch) -> None:
    captured: dict = {}

    def fake(url: str, *, headers=None, timeout: float = 3.0) -> object:
        captured["headers"] = headers
        return {"data": []}

    monkeypatch.setattr(catalog, "_fetch_json", fake)
    monkeypatch.delenv("XCO_ABSENT", raising=False)
    fetch_live_ids(ProviderDescriptor(name="x", base_url="https://x", api_key_env="XCO_ABSENT"))
    assert "Authorization" not in (captured["headers"] or {})


# --- models_dev_candidates -------------------------------------------------


def test_models_dev_candidates_targetco(models_dev: dict) -> None:
    cands = models_dev_candidates(models_dev, ProviderDescriptor(name="targetco"))
    assert len(cands) == 7
    assert {c.id for c in cands} == {
        "tcl-2.0",
        "tcl-3.5",
        "tcl-4.0",
        "tcl-4.1",
        "tcl-4.1-preview",
        "tcl-4",
        "tcl-04",
    }
    tcl = next(c for c in cands if c.id == "tcl-4.1")
    assert tcl.context == 400000
    assert tcl.tool_call is True
    assert tcl.input_modalities == ("text", "image")
    assert tcl.release_date == "2026-02-10"


def test_models_dev_unknown_provider(models_dev: dict) -> None:
    assert models_dev_candidates(models_dev, ProviderDescriptor(name="nope")) == []
    assert models_dev_candidates({}, ProviderDescriptor(name="x")) == []
    assert models_dev_candidates({"x": "not-a-dict"}, ProviderDescriptor(name="x")) == []
    assert models_dev_candidates({"x": {"models": "not-a-dict"}}, ProviderDescriptor(name="x")) == []


def test_models_dev_id_precedence_over_name(models_dev: dict) -> None:
    # id wins even though name points elsewhere
    cands = models_dev_candidates(
        models_dev, ProviderDescriptor(name="aggco", models_dev_id="targetco")
    )
    assert len(cands) == 7
    # id misses -> fall back to name
    cands2 = models_dev_candidates(
        models_dev, ProviderDescriptor(name="targetco", models_dev_id="missing")
    )
    assert len(cands2) == 7


# --- live_candidates -------------------------------------------------------


def test_live_candidates_metadata_unknown() -> None:
    cands = live_candidates([("a", 100), ("b", None)])
    assert cands == [Candidate(id="a", context=100), Candidate(id="b", context=None)]
    for c in cands:
        assert c.release_date is None
        assert c.tool_call is None
        assert c.input_modalities is None
        assert c.output_modalities is None


# --- merge_candidates ------------------------------------------------------


def test_merge_candidates_union(models_dev: dict) -> None:
    dev = models_dev_candidates(models_dev, ProviderDescriptor(name="targetco"))
    live = [("tcl-4.1", 999), ("fresh-model", 50)]
    merged = merge_candidates(dev, live)
    ids = [c.id for c in merged]
    # no dupes, models.dev order first, live-only appended in live order
    assert len(ids) == len(set(ids))
    assert ids == [c.id for c in dev] + ["fresh-model"]
    # shared id keeps models.dev metadata
    tcl = next(c for c in merged if c.id == "tcl-4.1")
    assert tcl.context == 400000
    assert tcl.release_date == "2026-02-10"
    # live-only id carries only context
    fresh = next(c for c in merged if c.id == "fresh-model")
    assert fresh.context == 50
    assert fresh.release_date is None
    assert fresh.tool_call is None


# --- fetch_models_dev ------------------------------------------------------


def test_fetch_models_dev_ok(models_dev: dict, monkeypatch) -> None:
    monkeypatch.setattr(
        catalog, "_fetch_json", lambda url, *, headers=None, timeout=3.0: models_dev
    )
    assert fetch_models_dev() is models_dev


def test_fetch_models_dev_failure(monkeypatch) -> None:
    def boom(url: str, *, headers=None, timeout: float = 3.0) -> object:
        raise OSError("nope")

    monkeypatch.setattr(catalog, "_fetch_json", boom)
    assert fetch_models_dev() is None

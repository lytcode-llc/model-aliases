"""profile.build_profile contract tests (stubbed ``providers`` package)."""

from __future__ import annotations

import sys
import types

import pytest

from model_aliases.catalog import ProviderDescriptor
from model_aliases.config import AliasSpec, TargetSpec
from model_aliases.profile import build_profile


class _FakeProviderProfile:
    """Minimal stand-in for providers.base.ProviderProfile (kwargs -> attrs)."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture(autouse=True)
def stub_providers(monkeypatch):
    base_mod = types.ModuleType("providers.base")
    base_mod.ProviderProfile = _FakeProviderProfile
    pkg = types.ModuleType("providers")
    monkeypatch.setitem(sys.modules, "providers", pkg)
    monkeypatch.setitem(sys.modules, "providers.base", base_mod)
    return base_mod


def _spec() -> TargetSpec:
    return TargetSpec(
        key="oc",
        provider="opencode-go",
        aliases={"fast": AliasSpec(match=("glm-*",)), "stable": AliasSpec(match=("glm-4.7*",))},
    )


def _desc() -> ProviderDescriptor:
    return ProviderDescriptor(
        name="opencode-go",
        base_url="https://api.opencode.ai/v1",
        api_key_env="OPENCODE_API_KEY",
    )


def _extras(**kw):
    base = {"api_mode": "chat_completions", "supports_vision": False, "inline_api_key": None, "warn": None}
    base.update(kw)
    return base


def _build(resolved=None, caps=None, rid=None, refresh=None, extras=None):
    return build_profile(
        profile_name="alias-oc",
        spec=_spec(),
        desc=_desc(),
        extras=extras or _extras(),
        resolved=resolved if resolved is not None else {"fast": "glm-4.7", "stable": "glm-4.7"},
        capability_meta=caps if caps is not None else {"fast": {"limit": {"context": 128000}}},
        rid_meta=rid if rid is not None else {"glm-4.7": {"limit": {"context": 128000}}},
        refresh=refresh,
    )


# --- normalize_model_id (must stay pure dict lookups) ----------------------

def test_normalize_resolves_alias():
    assert _build().normalize_model_id("fast") == "glm-4.7"


def test_normalize_case_insensitive():
    assert _build().normalize_model_id("FAST") == "glm-4.7"
    assert _build().normalize_model_id("  stable  ") == "glm-4.7"


def test_normalize_concrete_id_passes_through_as_none():
    assert _build().normalize_model_id("glm-4.7") is None
    assert _build().normalize_model_id("unknown") is None
    assert _build().normalize_model_id(123) is None  # type: ignore[arg-type]


# --- wire field copying -----------------------------------------------------

def test_wire_fields_copied_from_target():
    p = _build()
    assert p.base_url == "https://api.opencode.ai/v1"
    assert p.env_vars == ("OPENCODE_API_KEY",)
    assert p.fallback_models == ("fast", "stable")
    assert p.static_models == ("fast", "stable")
    assert "opencode-go" in p.display_name


def test_model_capabilities_keyed_by_alias():
    p = _build()
    assert p.model_capabilities["fast"]["limit"]["context"] == 128000


# --- live_context_length / resolve_api_key_secret --------------------------

def test_live_context_length_from_rid_meta():
    assert _build().live_context_length("fast") == 128000
    assert _build().live_context_length("nope") is None


def test_inline_api_key_secret():
    p = _build(extras=_extras(inline_api_key="sk-inline"))
    assert p.resolve_api_key_secret() == ("sk-inline", "config:inline-api-key")
    assert _build().resolve_api_key_secret() is None


# --- fetch_models / refresh state swap -------------------------------------

def test_fetch_models_returns_alias_ids_without_refresh():
    p = _build(refresh=None)
    assert p.fetch_models() == ["fast", "stable"]


def test_fetch_models_refresh_receives_timeout():
    seen: list[float | None] = []

    def refresh(timeout=None):
        seen.append(timeout)
        return None

    p = _build(refresh=refresh)
    p.fetch_models(timeout=5.0)
    assert seen == [5.0]


def test_fetch_models_survives_refresh_exception():
    def refresh(timeout=None):
        raise RuntimeError("boom")

    p = _build(refresh=refresh)
    assert p.fetch_models() == ["fast", "stable"]  # ids unaffected
    assert p.normalize_model_id("fast") == "glm-4.7"  # resolution untouched


def test_fetch_models_empty_refresh_does_not_wipe_snapshot():
    def refresh(timeout=None):
        return ({}, {}, {})

    p = _build(refresh=refresh)
    p.fetch_models()
    assert p.normalize_model_id("fast") == "glm-4.7"  # S2: empty result keeps LKG


def test_fetch_models_successful_refresh_swaps_state():
    def refresh(timeout=None):
        return (
            {"fast": "glm-5.0"},
            {"fast": {"limit": {"context": 256000}}},
            {"glm-5.0": {"limit": {"context": 256000}}},
        )

    p = _build(refresh=refresh)
    assert p.fetch_models() == ["fast", "stable"]
    assert p.normalize_model_id("fast") == "glm-5.0"
    assert p.model_capabilities["fast"]["limit"]["context"] == 256000
    assert p.live_context_length("fast") == 256000

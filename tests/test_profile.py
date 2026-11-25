"""profile.wrap_profile contract tests (stubbed ``providers`` package)."""

from __future__ import annotations

import sys
import types

import pytest

from model_aliases.config import AliasSpec
from model_aliases.profile import build_custom_profile, wrap_profile


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


def _target(**kw):
    defaults = {
        "name": "original",
        "aliases": ("real-1", "real-2"),
        "base_url": "https://api.example.com/v1",
        "env_vars": ("EXAMPLE_API_KEY",),
        "api_mode": "chat_completions",
    }
    defaults.update(kw)
    return _FakeProviderProfile(**defaults)


def _wrap(target=None, *, resolved=None, caps=None, rid_meta=None, real_ids=None, refresh=None, extra_aliases=()):
    return wrap_profile(
        target or _target(),
        provider_name="test-provider",
        aliases={"fast": AliasSpec(match=("tcl-*",)), "stable": AliasSpec(match=("glm-4.7*",))},
        resolved=resolved if resolved is not None else {"fast": "tcl-4.1", "stable": "glm-4.7"},
        caps=caps if caps is not None else {"fast": {"limit": {"context": 128000}}},
        rid_meta=rid_meta if rid_meta is not None else {"tcl-4.1": {"limit": {"context": 128000}}, "glm-4.7": {"limit": {"context": 64000}}},
        real_ids=real_ids if real_ids is not None else ["tcl-4.1", "glm-4.7"],
        refresh=refresh,
        extra_aliases=extra_aliases,
    )


# --- normalize_model_id ----------------------------------------------------


def test_normalize_resolves_alias():
    p = _wrap()
    assert p.normalize_model_id("fast") == "tcl-4.1"
    assert p.normalize_model_id("stable") == "glm-4.7"


def test_normalize_case_insensitive_and_strips():
    p = _wrap()
    assert p.normalize_model_id("FAST") == "tcl-4.1"
    assert p.normalize_model_id("  stable  ") == "glm-4.7"


def test_normalize_unknown_slug_returns_none():
    p = _wrap()
    assert p.normalize_model_id("glm-4.7") is None
    assert p.normalize_model_id("unknown") is None
    assert p.normalize_model_id(123) is None  # type: ignore[arg-type]


def test_normalize_known_alias_unmatched_logs_once(caplog):
    p = _wrap(resolved={})
    with caplog.at_level("WARNING", logger="model-aliases"):
        assert p.normalize_model_id("fast") is None
        assert p.normalize_model_id("FAST") is None
        assert p.normalize_model_id("fast") is None
    # one warning per (provider, lowercased alias)
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "fast" in warnings[0].message and "test-provider" in warnings[0].message


# --- wire field copying ----------------------------------------------------


def test_wire_fields_copied_from_target():
    p = _wrap()
    assert p.base_url == "https://api.example.com/v1"
    assert p.env_vars == ("EXAMPLE_API_KEY",)
    assert p.name == "test-provider"


def test_model_capabilities_keyed_by_alias():
    p = _wrap()
    assert p.model_capabilities["fast"]["limit"]["context"] == 128000


# --- live_context_length ---------------------------------------------------


def test_live_context_length_from_rid_meta():
    p = _wrap()
    assert p.live_context_length("fast") == 128000
    assert p.live_context_length("stable") == 64000


def test_live_context_length_delegates_for_real_ids():
    calls: list[tuple] = []

    class _TargetWithLive:
        def __init__(self):
            self.name = "orig"
            self.aliases = ()

        def live_context_length(self, model, *, api_key="", base_url=""):
            calls.append((model, api_key, base_url))
            return 999

    p = _wrap(_TargetWithLive())
    assert p.live_context_length("real-model") == 999
    assert calls == [("real-model", "", "")]
    assert p.live_context_length("fast") == 128000


# --- fetch_models / refresh state swap -------------------------------------


def test_fetch_models_returns_real_and_alias_ids():
    p = _wrap(refresh=None)
    assert p.fetch_models() == ["tcl-4.1", "glm-4.7", "fast", "stable"]


def test_fetch_models_refresh_receives_timeout():
    seen: list[float | None] = []

    def refresh(timeout=None):
        seen.append(timeout)
        return None

    p = _wrap(refresh=refresh)
    p.fetch_models(timeout=5.0)
    assert seen == [5.0]


def test_fetch_models_survives_refresh_exception():
    def refresh(timeout=None):
        raise RuntimeError("boom")

    p = _wrap(refresh=refresh)
    assert p.fetch_models() == ["tcl-4.1", "glm-4.7", "fast", "stable"]
    assert p.normalize_model_id("fast") == "tcl-4.1"


def test_fetch_models_empty_refresh_does_not_wipe_snapshot():
    def refresh(timeout=None):
        return ({}, {}, {}, [])

    p = _wrap(refresh=refresh)
    p.fetch_models()
    assert p.normalize_model_id("fast") == "tcl-4.1"


def test_fetch_models_successful_refresh_swaps_state():
    def refresh(timeout=None):
        return (
            {"fast": "tcl-5.0"},
            {"fast": {"limit": {"context": 256000}}},
            {"tcl-5.0": {"limit": {"context": 256000}}},
            ["tcl-5.0"],
        )

    p = _wrap(refresh=refresh)
    # real_ids are replaced by the refresh tuple; aliases are appended after
    assert p.fetch_models() == ["tcl-5.0", "fast", "stable"]
    assert p.normalize_model_id("fast") == "tcl-5.0"
    assert p.model_capabilities["fast"]["limit"]["context"] == 256000
    assert p.live_context_length("fast") == 256000
    assert p._real_ids == ["tcl-5.0"]


# --- build_custom_profile --------------------------------------------------


def test_build_custom_profile_field_mapping():
    prof = build_custom_profile(
        "work",
        {
            "api": "https://gpu.example/v1",
            "base_url": "https://ignored.example/v1",
            "key_env": "GPU_KEY",
            "transport": "anthropic_messages",
            "catalog_provider": "targetco",
        },
    )
    assert prof.name == "work"
    assert prof.base_url == "https://gpu.example/v1"
    assert prof.env_vars == ("GPU_KEY",)
    assert prof.api_mode == "anthropic_messages"
    assert prof.models_dev_id == "targetco"
    assert prof.auth_type == "api_key"
    assert prof.supports_health_check is False
    assert prof.supports_model_listing is True


def test_build_custom_profile_falls_back_aliases():
    prof = build_custom_profile(
        "work",
        {
            "url": "https://gpu.example/v1",
            "api_key_env": "GPU_KEY",
            "api_mode": "chat_completions",
        },
    )
    assert prof.base_url == "https://gpu.example/v1"
    assert prof.env_vars == ("GPU_KEY",)
    assert prof.api_mode == "chat_completions"


def test_build_custom_profile_default_api_mode():
    prof = build_custom_profile("work", {"api": "https://gpu.example/v1"})
    assert prof.api_mode == "chat_completions"
    assert prof.env_vars == ()


# --- extra_aliases ---------------------------------------------------------


def test_extra_aliases_appended():
    p = _wrap(_target(aliases=("existing",)), extra_aliases=("custom:test-provider",))
    assert p.aliases == ("existing", "custom:test-provider")


def test_wrap_profile_sets_picker_curated_floor():
    """The /model picker's curated floor reads static_models || fallback_models
    per plugin profile; without it a live-catalog target (empty static list)
    leaves the floor empty and the picker shows only the active model."""
    p = _wrap()
    assert p.static_models == ("fast", "stable")
    assert p.fallback_models == ("fast", "stable")
    # shadow wins over a non-empty bundled static list
    t = _target(static_models=("bundled-1",), fallback_models=("bundled-1",))
    p2 = _wrap(t)
    assert p2.static_models == ("fast", "stable")
    assert p2.fallback_models == ("fast", "stable")

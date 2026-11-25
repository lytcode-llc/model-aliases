"""plugin v2: referenced_providers, describe_provider, resolve_provider, register_all."""

from __future__ import annotations

import logging
import sys
import time
import types

import pytest

from model_aliases import plugin as plugin_mod
from model_aliases import state
from model_aliases.catalog import ProviderDescriptor
from model_aliases.config import parse
from model_aliases.plugin import (
    TargetError,
    describe_provider,
    referenced_providers,
    register_all,
    resolve_provider,
)

CACHE_KEY = "targetco"


# --- helpers ---------------------------------------------------------------


def _cfg_raw(**over):
    raw = {
        "fast": {"match": ["tcl-*"]},
    }
    raw.update(over)
    return raw


def _desc(name="targetco", **kw):
    kw.setdefault("name", name)
    return ProviderDescriptor(**kw)


@pytest.fixture(autouse=True)
def stub_providers(monkeypatch):
    """Stub providers package + providers.base fake."""
    registered: list = []

    def register_provider(profile):
        registered.append(profile)

    class _FakeProviderProfile:
        aliases = ()

        def __init__(self, **kw):
            self.__dict__.update(kw)

    pkg = types.ModuleType("providers")
    pkg.register_provider = register_provider
    pkg.get_provider_profile = lambda name: None
    pkg.list_providers = lambda: []
    base_mod = types.ModuleType("providers.base")
    base_mod.ProviderProfile = _FakeProviderProfile
    monkeypatch.setitem(sys.modules, "providers", pkg)
    monkeypatch.setitem(sys.modules, "providers.base", base_mod)
    return registered


# --- referenced_providers --------------------------------------------------


def test_referenced_providers_model_block():
    full = {"model": {"provider": "openai"}}
    cfg = parse({"a": {"match": "x*"}})
    assert list(referenced_providers(full, cfg).keys()) == ["openai"]


def test_referenced_providers_auxiliary_fallback_delegation_smart_routing():
    full = {
        "model": {"provider": "openai"},
        "auxiliary": {"coder": {"provider": "anthropic"}, "vision": {"provider": "auto"}},
        "fallback_providers": [{"provider": "google"}, {"provider": ""}],
        "delegation": {"provider": "groq"},
        "smart_model_routing": {"cheap_model": {"provider": "deepseek"}},
    }
    cfg = parse({"a": {"match": "x*"}})
    names = list(referenced_providers(full, cfg).keys())
    assert names == ["openai", "anthropic", "google", "groq", "deepseek"]


def test_referenced_providers_custom_list_and_dict():
    full = {
        "custom_providers": [
            {"name": "local", "api": "http://127.0.0.1:9/v1"},
        ],
        "providers": {
            "legacy": {"api": "http://legacy.example/v1"},
        },
    }
    cfg = parse({"a": {"match": "x*"}})
    refs = referenced_providers(full, cfg)
    assert list(refs.keys()) == ["local", "legacy"]
    assert refs["local"]["api"] == "http://127.0.0.1:9/v1"
    assert refs["legacy"]["api"] == "http://legacy.example/v1"


def test_referenced_providers_wrap_all(monkeypatch):
    class P1:
        name = "alpha"

    class P2:
        name = "beta"

    pkg = sys.modules.get("providers")
    if pkg is None:
        pkg = types.ModuleType("providers")
        monkeypatch.setitem(sys.modules, "providers", pkg)
    pkg.list_providers = lambda: [P1(), P2()]
    cfg = parse({"wrap_all": True, "a": {"match": "x*"}})
    refs = referenced_providers({}, cfg)
    assert list(refs.keys()) == ["alpha", "beta"]


# --- describe_provider -----------------------------------------------------


def test_describe_bundled_profile(monkeypatch):
    fake = types.SimpleNamespace(
        name="opencode-go",
        api_mode="codex_responses",
        auth_type="api_key",
        env_vars=("OPENCODE_BASE_URL", "OPENCODE_API_KEY"),
        base_url="https://api.opencode.ai/v1",
        models_url="",
        models_dev_id="ocgo",
    )
    pkg = types.ModuleType("providers")
    pkg.get_provider_profile = lambda name: fake
    monkeypatch.setitem(sys.modules, "providers", pkg)
    prof, desc = describe_provider("opencode-go", None, {})
    assert desc.base_url == "https://api.opencode.ai/v1"
    assert desc.api_key_env == "OPENCODE_API_KEY"  # *_URL var excluded
    assert desc.models_dev_id == "ocgo"
    assert prof is fake


def test_describe_bundled_unknown_raises(monkeypatch):
    pkg = types.ModuleType("providers")
    pkg.get_provider_profile = lambda name: None
    monkeypatch.setitem(sys.modules, "providers", pkg)
    with pytest.raises(TargetError, match="unknown provider"):
        describe_provider("nope", None, {})


def test_describe_custom_entry(monkeypatch):
    entry = {
        "api": "https://gpu.example/v1",
        "key_env": "GPU_KEY",
        "transport": "anthropic_messages",
        "catalog_provider": "targetco",
    }
    prof, desc = describe_provider("work", entry, {})
    assert prof.base_url == "https://gpu.example/v1"
    assert desc.base_url == "https://gpu.example/v1"
    assert desc.api_key_env == "GPU_KEY"
    assert desc.models_dev_id == "targetco"
    assert prof.api_mode == "anthropic_messages"


def test_describe_custom_missing_api():
    with pytest.raises(TargetError, match="api/base_url"):
        describe_provider("work", {"key_env": "K"}, {})


# --- resolve_provider ------------------------------------------------------


def test_resolve_warm_cache_zero_network(tmp_path, monkeypatch, models_dev):
    """Fresh + complete cache: no models.dev fetch, no live fetch."""
    seed = state.Resolution(
        resolved={"fast": "tcl-4.1"},
        updated_at=time.time(),
        capabilities={"fast": dict(models_dev["targetco"]["models"]["tcl-4.1"])},
        real_ids=["tcl-4.1"],
    )
    state.save(tmp_path, CACHE_KEY, seed)

    def explode(*a, **k):
        raise AssertionError("network must not be touched on warm cache")

    monkeypatch.setattr(plugin_mod, "_memo_models_dev", explode)
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", explode)

    cfg = parse(_cfg_raw())
    resolved, caps, rid_meta, real_ids = resolve_provider(
        cfg, "targetco", _desc(), tmp_path
    )
    assert resolved == {"fast": "tcl-4.1"}
    assert caps["fast"]["limit"]["context"] == 400000
    assert rid_meta["tcl-4.1"]["limit"]["context"] == 400000
    assert real_ids


def test_resolve_cold_saves_state_and_capabilities(tmp_path, monkeypatch, models_dev):
    monkeypatch.setattr(plugin_mod, "_memo_models_dev", lambda: models_dev)
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", lambda desc, timeout=3.0: None)
    cfg = parse(_cfg_raw())
    resolved, caps, rid_meta, real_ids = resolve_provider(cfg, "targetco", _desc(), tmp_path)
    assert resolved["fast"] == "tcl-4.1"
    saved = state.load(tmp_path, CACHE_KEY)
    assert saved is not None and saved.resolved["fast"] == "tcl-4.1"
    assert saved.capabilities["fast"]["release_date"] == "2026-02-10"
    assert rid_meta["tcl-4.1"]["tool_call"] is True
    assert "tcl-4.1" in real_ids


def test_resolve_drift_logged_and_recorded(tmp_path, monkeypatch, models_dev, caplog):
    seed = state.Resolution(resolved={"fast": "tcl-3.5"}, updated_at=1.0)  # ancient -> stale
    state.save(tmp_path, CACHE_KEY, seed)
    monkeypatch.setattr(plugin_mod, "_memo_models_dev", lambda: models_dev)
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", lambda desc, timeout=3.0: None)
    cfg = parse(_cfg_raw())
    with caplog.at_level(logging.WARNING, logger="model-aliases"):
        resolved, _c, _r, _ri = resolve_provider(cfg, "targetco", _desc(), tmp_path)
    assert resolved["fast"] == "tcl-4.1"
    assert any("tcl-3.5 -> tcl-4.1" in r.message for r in caplog.records), caplog.records
    saved = state.load(tmp_path, CACHE_KEY)
    assert saved.history and saved.history[-1]["from"] == "tcl-3.5"


def test_resolve_strict_raises_on_unresolved(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_mod, "_memo_models_dev", lambda: {})
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", lambda desc, timeout=3.0: None)
    cfg = parse({**_cfg_raw(), "strict": True})
    with pytest.raises(TargetError, match="fast"):
        resolve_provider(cfg, "targetco", _desc(), tmp_path)


def test_resolve_tolerant_logs_error_on_unresolved(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(plugin_mod, "_memo_models_dev", lambda: {})
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", lambda desc, timeout=3.0: None)
    cfg = parse(_cfg_raw())
    with caplog.at_level(logging.ERROR, logger="model-aliases"):
        resolved, _c, _r, _ri = resolve_provider(cfg, "targetco", _desc(), tmp_path)
    assert resolved == {}
    assert any("fast" in r.message and "no models" in r.message for r in caplog.records)


def test_resolve_tolerant_keeps_last_known_good_when_fetches_fail(tmp_path, monkeypatch):
    seed = state.Resolution(resolved={"fast": "tcl-4.1"}, updated_at=1.0)  # stale
    state.save(tmp_path, CACHE_KEY, seed)

    monkeypatch.setattr(plugin_mod, "_memo_models_dev", lambda: None)
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", lambda desc, timeout=3.0: None)
    cfg = parse(_cfg_raw())
    resolved, _c, _r, _ri = resolve_provider(cfg, "targetco", _desc(), tmp_path)
    assert resolved == {"fast": "tcl-4.1"}  # network dead -> last known good survives


# --- register_all ----------------------------------------------------------


def _run_register_all(monkeypatch, tmp_path, raw_section, full_config=None):
    monkeypatch.setattr(plugin_mod, "_load_raw_section", lambda: (raw_section, full_config or {}))
    monkeypatch.setattr(plugin_mod, "_hermes_home", lambda: tmp_path)
    register_all()


def _custom_full(catalog_provider: str = "targetco") -> dict:
    return {
        "custom_providers": [
            {"name": "local", "api": "http://127.0.0.1:9/v1", "catalog_provider": catalog_provider}
        ]
    }


def test_register_all_registers_profile(tmp_path, monkeypatch, stub_providers, models_dev):
    monkeypatch.setattr(plugin_mod, "_memo_models_dev", lambda: models_dev)
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", lambda desc, timeout=3.0: None)
    _run_register_all(monkeypatch, tmp_path, _cfg_raw(), _custom_full())
    assert [p.name for p in stub_providers] == ["local"]
    p = stub_providers[0]
    assert p.normalize_model_id("fast") == "tcl-4.1"
    assert "fast" in p.fetch_models()


def test_register_all_custom_entry_extra_alias(tmp_path, monkeypatch, stub_providers, models_dev):
    monkeypatch.setattr(plugin_mod, "_memo_models_dev", lambda: models_dev)
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", lambda desc, timeout=3.0: None)
    _run_register_all(monkeypatch, tmp_path, _cfg_raw(), _custom_full())
    p = stub_providers[0]
    assert "custom:local" in p.aliases


def test_register_all_strict_registers_nothing(tmp_path, monkeypatch, stub_providers, models_dev):
    """Strict failure must not partially register earlier providers."""
    monkeypatch.setattr(plugin_mod, "_memo_models_dev", lambda: models_dev)
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", lambda desc, timeout=3.0: None)
    raw = {
        "strict": True,
        "a": {"match": ["tcl-*"]},
    }
    full = {
        "model": {"provider": "local"},
        "auxiliary": {"ghost": {"provider": "ghost"}},
        "custom_providers": [
            {"name": "local", "api": "http://127.0.0.1:9/v1", "catalog_provider": "targetco"},
        ],
    }
    with pytest.raises(RuntimeError, match="ghost"):
        _run_register_all(monkeypatch, tmp_path, raw, full)
    assert stub_providers == []  # nothing registered despite 'local' resolving first


def test_register_all_config_error_registers_nothing(tmp_path, monkeypatch, stub_providers, caplog):
    with caplog.at_level(logging.ERROR, logger="model-aliases"):
        _run_register_all(monkeypatch, tmp_path, {"a": {"match": 123}})
    assert stub_providers == []
    assert any("invalid config" in r.message for r in caplog.records)


def test_register_all_empty_config_is_noop(tmp_path, monkeypatch, stub_providers):
    _run_register_all(monkeypatch, tmp_path, {})
    assert stub_providers == []


def test_wrapped_profile_answers_to_alias_provider_name(tmp_path, monkeypatch, stub_providers, models_dev):
    """Early-return families (opencode/aggregators/dot-hyphen) need the
    neutral alias-<provider> name on the request path."""
    monkeypatch.setattr(plugin_mod, "_memo_models_dev", lambda: models_dev)
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", lambda desc, timeout=3.0: None)
    _run_register_all(monkeypatch, tmp_path, _cfg_raw(), _custom_full())
    p = stub_providers[0]
    assert "alias-local" in p.aliases, p.aliases
    # bare name + custom: form for custom entries
    assert "local" in p.aliases
    assert "custom:local" in p.aliases


def test_alias_provider_reference_resolves_back_for_wrapping(tmp_path, monkeypatch, stub_providers, models_dev):
    """model.provider: alias-<x> must wrap <x> (the neutral name is an alias
    OF the real wrap — describing it directly would miss)."""
    monkeypatch.setattr(plugin_mod, "_memo_models_dev", lambda: models_dev)
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", lambda desc, timeout=3.0: None)
    pkg = sys.modules["providers"]
    fake = types.SimpleNamespace(
        name="opencode-go", api_mode="chat_completions", supports_vision=False,
        auth_type="api_key", env_vars=("OPENCODE_GO_API_KEY",),
        base_url="https://api.opencode.ai/v1", models_url="", models_dev_id="targetco",
    )
    pkg.get_provider_profile = lambda name: fake if name == "opencode-go" else None
    raw = {"glm-latest": {"match": ["glm-*"]}}
    full = {"model": {"provider": "alias-opencode-go"}}
    _run_register_all(monkeypatch, tmp_path, raw, full)
    assert [p.name for p in stub_providers] == ["opencode-go"], stub_providers
    p = stub_providers[0]
    assert "alias-opencode-go" in p.aliases
    # glm-* matches nothing in the targetco fixture — unresolved alias stays
    # literal (the registration + neutral name are what this test proves)
    assert p.normalize_model_id("glm-latest") is None

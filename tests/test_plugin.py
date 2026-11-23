"""plugin.describe_target / resolve_target / register_all with stubbed seams."""

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
from model_aliases.plugin import TargetError, describe_target, register_all, resolve_target

CACHE_KEY = "alias-oc"


# --- helpers ---------------------------------------------------------------

def _cfg_raw(**over):
    raw = {
        "targets": {"oc": {"provider": "custom:local", "aliases": {"fast": {"match": ["tcl-*"]}}}}
    }
    raw.update(over)
    return raw


def _desc(name="targetco", **kw):
    kw.setdefault("name", name)
    return ProviderDescriptor(**kw)


@pytest.fixture
def stub_providers(monkeypatch):
    """providers package (register_provider capture) + providers.base fake."""
    registered: list = []

    def register_provider(profile):
        registered.append(profile)

    class _FakeProviderProfile:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    pkg = types.ModuleType("providers")
    pkg.register_provider = register_provider
    pkg.get_provider_profile = lambda name: None
    base_mod = types.ModuleType("providers.base")
    base_mod.ProviderProfile = _FakeProviderProfile
    monkeypatch.setitem(sys.modules, "providers", pkg)
    monkeypatch.setitem(sys.modules, "providers.base", base_mod)
    return registered


# --- describe_target --------------------------------------------------------

def test_describe_named_custom_block():
    full = {"providers": {"work": {"api": "https://gpu.example/v1", "key_env": "GPU_KEY",
                                   "transport": "anthropic_messages", "api_key": "  sk-1  "}}}
    desc, extras = describe_target("custom:work", full)
    assert desc.base_url == "https://gpu.example/v1"
    assert desc.api_key_env == "GPU_KEY"
    assert extras["inline_api_key"] == "sk-1"
    assert extras["api_mode"] == "anthropic_messages"


def test_describe_named_custom_bare_name_and_disabled():
    full = {"providers": {"work": {"api": "https://x/v1", "enabled": False}}}
    with pytest.raises(TargetError, match="disabled"):
        describe_target("work", full)
    full2 = {"providers": {"work": {"api": "https://x/v1"}}}
    assert describe_target("work", full2)[0].base_url == "https://x/v1"


def test_describe_named_custom_missing_api():
    with pytest.raises(TargetError, match="api/base_url"):
        describe_target("custom:work", {"providers": {"work": {"key_env": "K"}}})


def test_describe_key_cmd_warns():
    full = {"providers": {"w": {"api": "https://x/v1", "key_cmd": "op token"}}}
    _desc_out, extras = describe_target("custom:w", full)
    assert "key_cmd" in extras["warn"]


def test_describe_bundled_profile(monkeypatch):
    fake = types.SimpleNamespace(
        name="opencode-go", api_mode="codex_responses", supports_vision=True,
        auth_type="api_key", env_vars=("OPENCODE_BASE_URL", "OPENCODE_API_KEY"),
        base_url="https://api.opencode.ai/v1", models_url="", models_dev_id="ocgo",
    )
    pkg = types.ModuleType("providers")
    pkg.get_provider_profile = lambda name: fake
    monkeypatch.setitem(sys.modules, "providers", pkg)
    desc, extras = describe_target("opencode-go", {})
    assert desc.base_url == "https://api.opencode.ai/v1"
    assert desc.api_key_env == "OPENCODE_API_KEY"  # *_URL var excluded
    assert desc.models_dev_id == "ocgo"
    assert extras["api_mode"] == "codex_responses"
    assert extras["supports_vision"] is True
    assert extras["warn"] is None


def test_describe_bundled_oauth_target_warns(monkeypatch):
    fake = types.SimpleNamespace(
        name="qwen-oauth", api_mode="chat_completions", supports_vision=False,
        auth_type="oauth_external", env_vars=(), base_url="", models_url="", models_dev_id="",
    )
    pkg = types.ModuleType("providers")
    pkg.get_provider_profile = lambda name: fake
    monkeypatch.setitem(sys.modules, "providers", pkg)
    _d, extras = describe_target("qwen-oauth", {})
    assert extras["warn"] and "oauth_external" in extras["warn"]


def test_describe_unknown_and_self_reference(monkeypatch):
    pkg = types.ModuleType("providers")
    monkeypatch.setitem(sys.modules, "providers", pkg)
    pkg.get_provider_profile = lambda name: None
    with pytest.raises(TargetError, match="unknown target"):
        describe_target("nope", {})
    pkg.get_provider_profile = lambda name: types.SimpleNamespace(name="alias-oc")
    with pytest.raises(TargetError, match="self-reference"):
        describe_target("alias-oc", {})


# --- resolve_target ---------------------------------------------------------

def test_resolve_warm_cache_zero_network(tmp_path, monkeypatch, models_dev):
    """Fresh + complete cache: no models.dev fetch, no live fetch (C1)."""
    seed = state.Resolution(
        resolved={"fast": "tcl-4.1"},
        updated_at=time.time(),
        capabilities={"fast": dict(models_dev["targetco"]["models"]["tcl-4.1"])},
    )
    state.save(tmp_path, CACHE_KEY, seed)

    def explode(*a, **k):
        raise AssertionError("network must not be touched on warm cache")

    monkeypatch.setattr(plugin_mod, "_memo_models_dev", explode)
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", explode)

    cfg = parse(_cfg_raw())
    resolved, caps, rid = resolve_target(
        cfg, cfg.targets["oc"], _desc(), tmp_path, CACHE_KEY,
    )
    assert resolved == {"fast": "tcl-4.1"}
    assert caps["fast"]["limit"]["context"] == 400000
    assert rid["tcl-4.1"]["limit"]["context"] == 400000


def test_resolve_cold_saves_state_and_capabilities(tmp_path, monkeypatch, models_dev):
    monkeypatch.setattr(plugin_mod, "_memo_models_dev", lambda: models_dev)
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", lambda desc, timeout=3.0: None)
    cfg = parse(_cfg_raw())
    resolved, caps, rid = resolve_target(
        cfg, cfg.targets["oc"], _desc(), tmp_path, CACHE_KEY,
    )
    assert resolved["fast"] == "tcl-4.1"
    saved = state.load(tmp_path, CACHE_KEY)
    assert saved is not None and saved.resolved["fast"] == "tcl-4.1"
    assert saved.capabilities["fast"]["release_date"] == "2026-02-10"
    assert rid["tcl-4.1"]["tool_call"] is True


def test_resolve_drift_logged_and_recorded(tmp_path, monkeypatch, models_dev, caplog):
    seed = state.Resolution(resolved={"fast": "tcl-3.5"}, updated_at=1.0)  # ancient -> stale
    state.save(tmp_path, CACHE_KEY, seed)
    monkeypatch.setattr(plugin_mod, "_memo_models_dev", lambda: models_dev)
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", lambda desc, timeout=3.0: None)
    cfg = parse(_cfg_raw())
    with caplog.at_level(logging.WARNING, logger="model-aliases"):
        resolved, _c, _r = resolve_target(cfg, cfg.targets["oc"], _desc(), tmp_path, CACHE_KEY)
    assert resolved["fast"] == "tcl-4.1"
    assert any("tcl-3.5 -> tcl-4.1" in r.message for r in caplog.records), caplog.records
    saved = state.load(tmp_path, CACHE_KEY)
    assert saved.history and saved.history[-1]["from"] == "tcl-3.5"


def test_resolve_strict_raises_on_unresolved(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_mod, "_memo_models_dev", lambda: {})
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", lambda desc, timeout=3.0: None)
    cfg = parse({**_cfg_raw(), "strict": True})
    with pytest.raises(TargetError, match="fast"):
        resolve_target(cfg, cfg.targets["oc"], _desc(), tmp_path, CACHE_KEY)


def test_resolve_tolerant_logs_error_on_unresolved(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(plugin_mod, "_memo_models_dev", lambda: {})
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", lambda desc, timeout=3.0: None)
    cfg = parse(_cfg_raw())
    with caplog.at_level(logging.ERROR, logger="model-aliases"):
        resolved, _c, _r = resolve_target(cfg, cfg.targets["oc"], _desc(), tmp_path, CACHE_KEY)
    assert resolved == {}
    assert any("resolved to no model" in r.message for r in caplog.records)


def test_resolve_tolerant_keeps_last_known_good_when_fetches_fail(tmp_path, monkeypatch):
    seed = state.Resolution(resolved={"fast": "tcl-4.1"}, updated_at=1.0)  # stale
    state.save(tmp_path, CACHE_KEY, seed)

    monkeypatch.setattr(plugin_mod, "_memo_models_dev", lambda: None)
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", lambda desc, timeout=3.0: None)
    cfg = parse(_cfg_raw())
    resolved, _c, _r = resolve_target(cfg, cfg.targets["oc"], _desc(), tmp_path, CACHE_KEY)
    assert resolved == {"fast": "tcl-4.1"}  # network dead -> last known good survives


# --- register_all -----------------------------------------------------------

def _run_register_all(monkeypatch, tmp_path, raw_section, full_config=None):
    monkeypatch.setattr(
        plugin_mod, "_load_raw_section", lambda: (raw_section, full_config or {})
    )
    monkeypatch.setattr(plugin_mod, "_hermes_home", lambda: tmp_path)
    register_all()


def _custom_full(catalog_provider: str = "targetco") -> dict:
    return {"providers": {"local": {"api": "http://127.0.0.1:9/v1",
                                     "catalog_provider": catalog_provider}}}


def test_register_all_registers_profile(tmp_path, monkeypatch, stub_providers, models_dev):
    monkeypatch.setattr(plugin_mod, "_memo_models_dev", lambda: models_dev)
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", lambda desc, timeout=3.0: None)
    _run_register_all(monkeypatch, tmp_path, _cfg_raw(), _custom_full())
    assert [p.name for p in stub_providers] == ["alias-oc"]
    p = stub_providers[0]
    assert p.normalize_model_id("fast") == "tcl-4.1"
    assert p.fetch_models() == ["fast"]


def test_register_all_strict_registers_nothing(tmp_path, monkeypatch, stub_providers, models_dev):
    """Strict failure must not partially register earlier targets (S4 contract)."""
    monkeypatch.setattr(plugin_mod, "_memo_models_dev", lambda: models_dev)
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", lambda desc, timeout=3.0: None)
    raw = {
        "strict": True,
        "targets": {
            "good": {"provider": "custom:local", "aliases": {"a": {"match": ["tcl-*"]}}},
            "ghost": {"provider": "custom:local", "aliases": {"g": {"match": ["nope-*"]}}},
        },
    }
    with pytest.raises(RuntimeError, match="ghost"):
        _run_register_all(monkeypatch, tmp_path, raw, _custom_full())
    assert stub_providers == []  # nothing registered despite 'good' resolving first


def test_register_all_config_error_registers_nothing(tmp_path, monkeypatch, stub_providers, caplog):
    with caplog.at_level(logging.ERROR, logger="model-aliases"):
        _run_register_all(monkeypatch, tmp_path, {"targets": {"Bad Key": {"provider": "x", "aliases": {"a": {"match": "y"}}}}})
    assert stub_providers == []
    assert any("invalid config" in r.message for r in caplog.records)


def test_register_all_empty_config_is_noop(tmp_path, monkeypatch, stub_providers):
    _run_register_all(monkeypatch, tmp_path, {})
    assert stub_providers == []

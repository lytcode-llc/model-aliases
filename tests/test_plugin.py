"""plugin v3: two-scope orchestration, naming guard, group provider ladder."""

from __future__ import annotations

import logging
import sys
import time
import types

import pytest

from model_aliases import plugin as plugin_mod
from model_aliases import state
from model_aliases.catalog import ProviderDescriptor
from model_aliases.config import AliasSpec, ConfigError, ProviderGroup, parse
from model_aliases.plugin import (
    TargetError,
    early_return_reason,
    referenced_providers,
    register_all,
    resolve_group_provider,
    resolve_target,
)

CACHE_KEY = "targetco"


# --- helpers ---------------------------------------------------------------


def _cfg_raw_groups(**over) -> dict:
    raw = {
        "provider_alias_to_many_model_aliases": {
            "ocg-alias": {
                "provider": "opencode-go",
                "fast": {"match": ["tcl-*"]},
            },
        },
    }
    raw.update(over)
    return raw


def _cfg_raw_global(**over) -> dict:
    raw = {
        "model_alias_to_all_provider_aliases": {
            "fast": {"match": ["tcl-*"]},
        },
    }
    raw.update(over)
    return raw


def _desc(name="targetco", **kw):
    kw.setdefault("name", name)
    return ProviderDescriptor(**kw)


@pytest.fixture(autouse=True)
def _patch_early_return_reason_for_hermeticity(monkeypatch):
    """Hermes is not a unit-test dependency; make the naming guard a no-op."""
    monkeypatch.setattr(plugin_mod, "early_return_reason", lambda name: None)


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


@pytest.fixture
def fake_opencode_profile():
    return types.SimpleNamespace(
        name="opencode-go",
        api_mode="codex_responses",
        auth_type="api_key",
        env_vars=("OPENCODE_BASE_URL", "OPENCODE_API_KEY"),
        base_url="https://api.opencode.ai/v1",
        models_url="",
        models_dev_id="targetco",
        aliases=(),
    )


# --- early_return_reason ---------------------------------------------------


def _install_fake_hermes(
    monkeypatch,
    *,
    aggregator=(),
    dot_to_hyphen=(),
    opencode_family=None,
    canonical=None,
):
    parent = types.ModuleType("hermes_cli")
    parent.__path__ = []  # make it a package so import finds submodules

    mod = types.ModuleType("hermes_cli.model_normalize")
    mod._AGGREGATOR_PROVIDERS = set(aggregator)
    mod._DOT_TO_HYPHEN_PROVIDERS = set(dot_to_hyphen)

    def _normalize_provider_alias(name):
        return canonical if canonical is not None else name

    mod._normalize_provider_alias = _normalize_provider_alias
    parent.model_normalize = mod

    models_mod = types.ModuleType("hermes_cli.models")

    def opencode_provider_family(name):
        return opencode_family if name in (opencode_family or ()) else None

    models_mod.opencode_provider_family = opencode_provider_family
    parent.models = models_mod

    monkeypatch.setitem(sys.modules, "hermes_cli", parent)
    monkeypatch.setitem(sys.modules, "hermes_cli.model_normalize", mod)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", models_mod)


def test_early_return_reason_aggregator(monkeypatch):
    _install_fake_hermes(monkeypatch, aggregator={"openrouter"})
    assert early_return_reason("openrouter") == "an aggregator family"


def test_early_return_reason_dot_to_hyphen(monkeypatch):
    _install_fake_hermes(monkeypatch, dot_to_hyphen={"perplexity"})
    assert early_return_reason("perplexity") == "a dot-to-hyphen family"


def test_early_return_reason_opencode_family(monkeypatch):
    _install_fake_hermes(monkeypatch, opencode_family={"opencode-go"})
    assert early_return_reason("opencode-go") == "the OpenCode family"


def test_early_return_reason_neutral(monkeypatch):
    _install_fake_hermes(monkeypatch, aggregator={"openrouter"}, dot_to_hyphen={"perplexity"})
    assert early_return_reason("ocg-alias") is None
    assert early_return_reason("local") is None


def test_early_return_reason_import_failure(monkeypatch):
    monkeypatch.delitem(sys.modules, "hermes_cli.model_normalize", raising=False)
    monkeypatch.delitem(sys.modules, "hermes_cli", raising=False)
    # Block import entirely.
    def _boom(*a, **k):
        raise ImportError("no hermes")
    monkeypatch.setattr("builtins.__import__", _boom)
    assert early_return_reason("anything") is None


# --- referenced_providers --------------------------------------------------


def test_referenced_providers_model_block():
    full = {"model": {"provider": "openai"}}
    cfg = parse({"model_alias_to_all_provider_aliases": {"a": {"match": "x*"}}})
    assert list(referenced_providers(full, cfg).keys()) == ["openai"]


def test_referenced_providers_auxiliary_fallback_delegation_smart_routing():
    full = {
        "model": {"provider": "openai"},
        "auxiliary": {"coder": {"provider": "anthropic"}, "vision": {"provider": "auto"}},
        "fallback_providers": [{"provider": "google"}, {"provider": ""}],
        "delegation": {"provider": "groq"},
        "smart_model_routing": {"cheap_model": {"provider": "deepseek"}},
    }
    cfg = parse({"model_alias_to_all_provider_aliases": {"a": {"match": "x*"}}})
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
    cfg = parse({"model_alias_to_all_provider_aliases": {"a": {"match": "x*"}}})
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
    cfg = parse({"wrap_all": True, "model_alias_to_all_provider_aliases": {"a": {"match": "x*"}}})
    refs = referenced_providers({}, cfg)
    assert list(refs.keys()) == ["alpha", "beta"]


def test_referenced_providers_skips_group_keys():
    full = {"model": {"provider": "ocg-alias"}}
    cfg = parse({
        "provider_alias_to_many_model_aliases": {
            "ocg-alias": {"provider": "opencode-go", "fast": {"match": "x*"}},
        },
    })
    refs = referenced_providers(full, cfg)
    assert "ocg-alias" not in refs


# --- resolve_group_provider ladder -----------------------------------------


def test_resolve_group_provider_name_join_custom_entry():
    full = {
        "custom_providers": [
            {"name": "ocg-alias", "api": "http://custom.example/v1", "key_env": "CUSTOM_KEY"},
        ],
    }
    group = ProviderGroup(key="ocg-alias", provider="opencode-go", aliases={"fast": AliasSpec(match=("tcl-*",))})
    name, entry, prof, desc = resolve_group_provider(group, full)
    assert name == "ocg-alias"
    assert entry["api"] == "http://custom.example/v1"
    assert prof.base_url == "http://custom.example/v1"
    assert desc.base_url == "http://custom.example/v1"


def test_resolve_group_provider_bundled_provider(monkeypatch, fake_opencode_profile):
    pkg = sys.modules["providers"]
    pkg.get_provider_profile = lambda name: fake_opencode_profile if name == "opencode-go" else None
    group = ProviderGroup(key="ocg-alias", provider="opencode-go", aliases={"fast": AliasSpec(match=("tcl-*",))})
    name, entry, prof, desc = resolve_group_provider(group, {})
    assert name == "opencode-go"
    assert entry is None
    assert prof is fake_opencode_profile
    assert desc.name == "opencode-go"
    assert desc.models_dev_id == "targetco"


def test_resolve_group_provider_slug_prefix_custom_entry():
    full = {
        "custom_providers": [
            {"name": "work", "api": "http://work.example/v1", "model": "opencode-go/glm-4o"},
        ],
    }
    group = ProviderGroup(key="ocg-alias", provider="opencode-go", aliases={"fast": AliasSpec(match=("tcl-*",))})
    name, entry, prof, desc = resolve_group_provider(group, full)
    assert name == "work"
    assert entry["model"] == "opencode-go/glm-4o"
    assert prof.base_url == "http://work.example/v1"


def test_resolve_group_provider_default_model_slug_prefix():
    full = {
        "custom_providers": [
            {"name": "work", "api": "http://work.example/v1", "default_model": "opencode-go/xyz"},
        ],
    }
    group = ProviderGroup(key="ocg-alias", provider="opencode-go", aliases={"fast": AliasSpec(match=("tcl-*",))})
    name, entry, prof, desc = resolve_group_provider(group, full)
    assert name == "work"


def test_resolve_group_provider_nothing_matches():
    group = ProviderGroup(key="ocg-alias", provider="opencode-go", aliases={"fast": AliasSpec(match=("tcl-*",))})
    with pytest.raises(TargetError, match="opencode-go"):
        resolve_group_provider(group, {})


def test_resolve_group_provider_name_join_missing_api():
    full = {
        "custom_providers": [
            {"name": "ocg-alias"},
        ],
    }
    group = ProviderGroup(key="ocg-alias", provider="opencode-go", aliases={"fast": AliasSpec(match=("tcl-*",))})
    with pytest.raises(TargetError, match="api/base_url"):
        resolve_group_provider(group, full)


# --- resolve_target --------------------------------------------------------


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

    cfg = parse(_cfg_raw_global())
    alias_map = dict(cfg.aliases)
    resolved, caps, rid_meta, real_ids = resolve_target(
        cfg, "targetco", _desc(), tmp_path, alias_map
    )
    assert resolved == {"fast": "tcl-4.1"}
    assert caps["fast"]["limit"]["context"] == 400000
    assert rid_meta["tcl-4.1"]["limit"]["context"] == 400000
    assert real_ids


def test_resolve_cold_saves_state_and_capabilities(tmp_path, monkeypatch, models_dev):
    monkeypatch.setattr(plugin_mod, "_memo_models_dev", lambda: models_dev)
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", lambda desc, timeout=3.0: None)
    cfg = parse(_cfg_raw_global())
    alias_map = dict(cfg.aliases)
    resolved, caps, rid_meta, real_ids = resolve_target(cfg, "targetco", _desc(), tmp_path, alias_map)
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
    cfg = parse(_cfg_raw_global())
    alias_map = dict(cfg.aliases)
    with caplog.at_level(logging.WARNING, logger="model-aliases"):
        resolved, _c, _r, _ri = resolve_target(cfg, "targetco", _desc(), tmp_path, alias_map)
    assert resolved["fast"] == "tcl-4.1"
    assert any("tcl-3.5 -> tcl-4.1" in r.message for r in caplog.records), caplog.records
    saved = state.load(tmp_path, CACHE_KEY)
    assert saved.history and saved.history[-1]["from"] == "tcl-3.5"


def test_resolve_strict_raises_on_unresolved(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_mod, "_memo_models_dev", lambda: {})
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", lambda desc, timeout=3.0: None)
    cfg = parse({**_cfg_raw_global(), "strict": True})
    alias_map = dict(cfg.aliases)
    with pytest.raises(TargetError, match="fast"):
        resolve_target(cfg, "targetco", _desc(), tmp_path, alias_map)


def test_resolve_tolerant_logs_error_on_unresolved(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(plugin_mod, "_memo_models_dev", lambda: {})
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", lambda desc, timeout=3.0: None)
    cfg = parse(_cfg_raw_global())
    alias_map = dict(cfg.aliases)
    with caplog.at_level(logging.ERROR, logger="model-aliases"):
        resolved, _c, _r, _ri = resolve_target(cfg, "targetco", _desc(), tmp_path, alias_map)
    assert resolved == {}
    assert any("fast" in r.message and "no models" in r.message for r in caplog.records)


def test_resolve_tolerant_keeps_last_known_good_when_fetches_fail(tmp_path, monkeypatch):
    seed = state.Resolution(resolved={"fast": "tcl-4.1"}, updated_at=1.0)  # stale
    state.save(tmp_path, CACHE_KEY, seed)

    monkeypatch.setattr(plugin_mod, "_memo_models_dev", lambda: None)
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", lambda desc, timeout=3.0: None)
    cfg = parse(_cfg_raw_global())
    alias_map = dict(cfg.aliases)
    resolved, _c, _r, _ri = resolve_target(cfg, "targetco", _desc(), tmp_path, alias_map)
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


def test_register_all_registers_group_wrapped_under_target_and_group_names(
    tmp_path, monkeypatch, stub_providers, models_dev, fake_opencode_profile
):
    monkeypatch.setattr(plugin_mod, "_memo_models_dev", lambda: models_dev)
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", lambda desc, timeout=3.0: None)
    pkg = sys.modules["providers"]
    pkg.get_provider_profile = lambda name: fake_opencode_profile if name == "opencode-go" else None

    _run_register_all(monkeypatch, tmp_path, _cfg_raw_groups())

    assert [p.name for p in stub_providers] == ["opencode-go"]
    p = stub_providers[0]
    assert p.normalize_model_id("fast") == "tcl-4.1"
    assert "fast" in p.fetch_models()
    assert "ocg-alias" in p.aliases


def test_register_all_custom_entry_extra_alias(tmp_path, monkeypatch, stub_providers, models_dev):
    monkeypatch.setattr(plugin_mod, "_memo_models_dev", lambda: models_dev)
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", lambda desc, timeout=3.0: None)
    _run_register_all(monkeypatch, tmp_path, _cfg_raw_global(), _custom_full())
    p = stub_providers[0]
    assert "custom:local" in p.aliases


def test_register_all_global_only_wraps_referenced_provider(
    tmp_path, monkeypatch, stub_providers, models_dev, fake_opencode_profile
):
    monkeypatch.setattr(plugin_mod, "_memo_models_dev", lambda: models_dev)
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", lambda desc, timeout=3.0: None)
    pkg = sys.modules["providers"]
    pkg.get_provider_profile = lambda name: fake_opencode_profile if name == "opencode-go" else None

    full = {"model": {"provider": "opencode-go"}}
    _run_register_all(monkeypatch, tmp_path, _cfg_raw_global(), full)

    assert [p.name for p in stub_providers] == ["opencode-go"]
    p = stub_providers[0]
    assert p.normalize_model_id("fast") == "tcl-4.1"
    # Global-only target has no group extra aliases.
    assert "custom:opencode-go" not in p.aliases


def test_register_all_strict_registers_nothing_with_ghost_group(
    tmp_path, monkeypatch, stub_providers, models_dev
):
    """Strict failure must not partially register earlier providers."""
    monkeypatch.setattr(plugin_mod, "_memo_models_dev", lambda: models_dev)
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", lambda desc, timeout=3.0: None)
    raw = {
        "strict": True,
        "provider_alias_to_many_model_aliases": {
            "ghost": {"provider": "targetco", "boo": {"match": ["no-such-model-*"]}},
        },
    }
    full = {
        "model": {"provider": "local"},
        "auxiliary": {"ghost": {"provider": "ghost"}},
        "custom_providers": [
            {"name": "local", "api": "http://127.0.0.1:9/v1", "catalog_provider": "targetco"},
        ],
    }
    with pytest.raises(RuntimeError, match="ghost|boo"):
        _run_register_all(monkeypatch, tmp_path, raw, full)
    assert stub_providers == []  # nothing registered despite 'local' resolving first


def test_register_all_config_error_registers_nothing(tmp_path, monkeypatch, stub_providers, caplog):
    with caplog.at_level(logging.ERROR, logger="model-aliases"):
        _run_register_all(monkeypatch, tmp_path, {"provider_alias_to_many_model_aliases": {"a": "bad"}})
    assert stub_providers == []
    assert any("invalid config" in r.message for r in caplog.records)


def test_register_all_empty_config_is_noop(tmp_path, monkeypatch, stub_providers):
    _run_register_all(monkeypatch, tmp_path, {})
    assert stub_providers == []


def test_register_all_naming_guard_raises_before_registration(
    tmp_path, monkeypatch, stub_providers, models_dev
):
    monkeypatch.setattr(plugin_mod, "_memo_models_dev", lambda: models_dev)
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", lambda desc, timeout=3.0: None)
    monkeypatch.setattr(plugin_mod, "early_return_reason", lambda name: "the OpenCode family" if name == "opencode-alias" else None)

    raw = {
        "provider_alias_to_many_model_aliases": {
            "opencode-alias": {"provider": "targetco", "fast": {"match": ["tcl-*"]}},
        },
    }
    with pytest.raises(ConfigError, match="OpenCode family"):
        _run_register_all(monkeypatch, tmp_path, raw)
    assert stub_providers == []


def test_register_all_custom_entry_alias_model_warning(
    tmp_path, monkeypatch, stub_providers, models_dev, caplog
):
    """A custom entry whose model is a known alias but unresolved on that provider logs."""
    monkeypatch.setattr(plugin_mod, "_memo_models_dev", lambda: models_dev)
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", lambda desc, timeout=3.0: None)

    raw = {
        "model_alias_to_all_provider_aliases": {
            "fast": {"match": ["tcl-*"]},  # known alias; will not match aggco models
        },
    }
    full = {
        "custom_providers": [
            {
                "name": "work",
                "api": "http://work.example/v1",
                "catalog_provider": "aggco",
                "model": "fast",
            },
        ],
    }
    with caplog.at_level(logging.ERROR, logger="model-aliases"):
        _run_register_all(monkeypatch, tmp_path, raw, full)

    assert len(stub_providers) == 1
    assert any(
        "work" in r.message and "fast" in r.message and "known alias" in r.message
        for r in caplog.records
    )


def test_register_all_custom_entry_alias_model_strict_error(
    tmp_path, monkeypatch, stub_providers, models_dev
):
    raw = {
        "strict": True,
        "model_alias_to_all_provider_aliases": {
            "fast": {"match": ["tcl-*"]},
        },
    }
    full = {
        "custom_providers": [
            {
                "name": "work",
                "api": "http://work.example/v1",
                "catalog_provider": "aggco",
                "model": "fast",
            },
        ],
    }
    monkeypatch.setattr(plugin_mod, "_memo_models_dev", lambda: models_dev)
    monkeypatch.setattr(plugin_mod, "fetch_live_ids", lambda desc, timeout=3.0: None)
    with pytest.raises(RuntimeError, match="work.*fast|fast.*work"):
        _run_register_all(monkeypatch, tmp_path, raw, full)
    assert stub_providers == []


def test_early_return_reason_canonicalization(monkeypatch):
    """The guard's second mechanism: names Hermes' provider-alias
    canonicalization rewrites (normalize_provider lowercases + alias table,
    e.g. local -> custom) never reach a profile registered under that name."""
    _install_fake_hermes(monkeypatch, canonical="ocg-alias")
    reason = early_return_reason("OCG-Alias")
    assert reason is not None
    assert "canonicaliz" in reason
    assert "ocg-alias" in reason


def test_group_key_collision_with_bundled_provider_rejected(tmp_path, monkeypatch, stub_providers):
    """A group key that IS a bundled provider name would shadow the real
    profile — the guard rejects it before any registration."""
    fake = types.SimpleNamespace(
        name="gemini", api_mode="chat_completions", supports_vision=False,
        auth_type="api_key", env_vars=(), base_url="https://x/v1", models_url="", models_dev_id="",
    )
    pkg = sys.modules["providers"]
    pkg.get_provider_profile = lambda name: fake if name == "gemini" else None
    _install_fake_hermes(monkeypatch, canonical=None)
    raw = {"provider_alias_to_many_model_aliases": {"gemini": {"provider": "gemini", "m": {"match": ["m*"]}}}}
    with pytest.raises(ConfigError, match="shadow"):
        _run_register_all(monkeypatch, tmp_path, raw, {})
    assert stub_providers == []


def test_ladder_prefix_boundary(tmp_path, monkeypatch, stub_providers, caplog):
    """Ladder (c) must not match 'gptx-9b' for prefix 'gpt' (boundary = '/' or exact).
    The entry itself is still wrapped — custom_providers entries are wrapped
    regardless of groups."""
    raw = {"provider_alias_to_many_model_aliases": {"pfx": {"provider": "gpt", "m": {"match": ["gpt*"]}}}}
    full = {"custom_providers": [{"name": "gptx", "api": "https://x/v1", "model": "gptx-9b"}]}
    _install_fake_hermes(monkeypatch, canonical=None)
    _run_register_all(monkeypatch, tmp_path, raw, full)
    assert [p.name for p in stub_providers] == ["gptx"]  # sweep wraps the entry
    # the GROUP failed the boundary rule and was reported
    assert any("slug prefix 'gpt'" in r.message for r in caplog.records), caplog.records

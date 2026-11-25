"""config.parse: v3 two-scope schema round-trip and ConfigError paths."""

from __future__ import annotations

import pytest

from model_aliases.config import ConfigError, parse


def _group(name: str = "ocg-alias", provider: str = "opencode-go") -> dict:
    return {
        "provider": provider,
        "fast": {"match": ["tcl-*"]},
        "stable": {"match": ["tcl-4.7*"]},
    }


def _full() -> dict:
    return {
        "wrap_all": True,
        "strict": True,
        "debug": True,
        "refresh_seconds": 120,
        "provider_alias_to_many_model_aliases": {
            "ocg-alias": {
                "provider": "opencode-go",
                "fast": {
                    "match": "glm-*",
                    "exclude": ["*-preview", "glm-legacy*"],
                    "filter": {
                        "minContext": 128000,
                        "capabilities": {"tools": True, "input": ["text", "image"], "output": "text"},
                        "minAge": "48h",
                    },
                    "select": {"strategy": "latest"},
                },
                "stable": {"match": ["glm-4.7*"]},
            },
        },
        "model_alias_to_all_provider_aliases": {
            "local": {"match": ["qwen*"]},
        },
    }


def test_parse_empty_is_defaults():
    cfg = parse({})
    assert cfg.wrap_all is False
    assert cfg.strict is False
    assert cfg.debug is False
    assert cfg.refresh_seconds == 3600
    assert cfg.groups == {}
    assert cfg.aliases == {}
    assert parse(None).aliases == {}


def test_parse_full_roundtrip():
    cfg = parse(_full())
    assert cfg.wrap_all is True
    assert cfg.strict is True
    assert cfg.debug is True
    assert cfg.refresh_seconds == 120
    assert list(cfg.groups) == ["ocg-alias"]
    group = cfg.groups["ocg-alias"]
    assert group.key == "ocg-alias"
    assert group.provider == "opencode-go"
    assert list(group.aliases) == ["fast", "stable"]
    fast = group.aliases["fast"]
    assert fast.match == ("glm-*",)
    assert fast.exclude == ("*-preview", "glm-legacy*")
    assert fast.min_context == 128000
    assert fast.tools is True
    assert fast.input_modalities == ("text", "image")
    assert fast.output_modalities == ("text",)
    assert fast.min_age_hours == 48
    assert group.aliases["stable"].match == ("glm-4.7*",)
    assert cfg.aliases["local"].match == ("qwen*",)


def test_groups_roundtrip():
    cfg = parse({
        "provider_alias_to_many_model_aliases": {
            "ocg-alias": _group(),
        },
    })
    assert len(cfg.groups) == 1
    assert "ocg-alias" in cfg.groups
    assert cfg.groups["ocg-alias"].provider == "opencode-go"
    assert list(cfg.groups["ocg-alias"].aliases) == ["fast", "stable"]


def test_global_roundtrip():
    cfg = parse({
        "model_alias_to_all_provider_aliases": {
            "glm-latest": {"match": ["glm-*"]},
        },
    })
    assert cfg.aliases["glm-latest"].match == ("glm-*",)


def test_min_age_units():
    cfg = parse({
        "model_alias_to_all_provider_aliases": {
            "a": {"match": "x*", "filter": {"minAge": "3d"}},
        },
    })
    assert cfg.aliases["a"].min_age_hours == 72
    cfg2 = parse({
        "model_alias_to_all_provider_aliases": {
            "a": {"match": "x*", "filter": {"minAge": "12h"}},
        },
    })
    assert cfg2.aliases["a"].min_age_hours == 12


def test_provider_value_is_stripped():
    cfg = parse({
        "provider_alias_to_many_model_aliases": {
            "ocg-alias": {
                "provider": " opencode-go ",
                "fast": {"match": "tcl-*"},
            },
        },
    })
    assert cfg.groups["ocg-alias"].provider == "opencode-go"


def _expect_error(fragment: str, raw: dict):
    with pytest.raises(ConfigError) as ei:
        parse(raw)
    assert fragment in str(ei.value), f"{fragment!r} not in {str(ei.value)!r}"


# --- reserved scalar errors ------------------------------------------------


def test_bad_wrap_all():
    _expect_error("wrap_all", {"wrap_all": "yes"})


def test_bad_strict():
    _expect_error("strict", {"strict": "yes"})


def test_bad_debug():
    _expect_error("debug", {"debug": "yes"})


def test_bad_refresh_seconds():
    _expect_error("refresh_seconds", {"refresh_seconds": 0})
    _expect_error("refresh_seconds", {"refresh_seconds": True})
    _expect_error("refresh_seconds", {"refresh_seconds": -1})


# --- top-level unknown key errors ------------------------------------------


def test_unknown_top_level_key():
    _expect_error("model_aliases", {"unknown_key": {}})


def test_flat_alias_keys_rejected_at_top_level():
    # v2 flat alias keys are now unknown at the top level.
    _expect_error("model_aliases", {"fast": {"match": "x*"}})


# --- group-level errors -----------------------------------------------------


def test_group_missing_provider():
    _expect_error(
        "provider_alias_to_many_model_aliases.ocg-alias.provider",
        {
            "provider_alias_to_many_model_aliases": {
                "ocg-alias": {"fast": {"match": "x*"}},
            },
        },
    )


def test_group_provider_empty():
    _expect_error(
        "provider_alias_to_many_model_aliases.ocg-alias.provider",
        {
            "provider_alias_to_many_model_aliases": {
                "ocg-alias": {"provider": "   ", "fast": {"match": "x*"}},
            },
        },
    )


def test_group_provider_not_string():
    _expect_error(
        "provider_alias_to_many_model_aliases.ocg-alias.provider",
        {
            "provider_alias_to_many_model_aliases": {
                "ocg-alias": {"provider": 123, "fast": {"match": "x*"}},
            },
        },
    )


def test_group_zero_aliases():
    _expect_error(
        "provider_alias_to_many_model_aliases.ocg-alias",
        {
            "provider_alias_to_many_model_aliases": {
                "ocg-alias": {"provider": "opencode-go"},
            },
        },
    )


def test_group_not_mapping():
    _expect_error(
        "provider_alias_to_many_model_aliases.ocg-alias",
        {
            "provider_alias_to_many_model_aliases": {
                "ocg-alias": ["fast"],
            },
        },
    )


def test_groups_not_mapping():
    _expect_error(
        "provider_alias_to_many_model_aliases",
        {"provider_alias_to_many_model_aliases": ["ocg-alias"]},
    )


# --- alias spec errors (inside groups) --------------------------------------


def test_group_unknown_alias_value_key():
    _expect_error(
        "provider_alias_to_many_model_aliases.ocg-alias.fast",
        {
            "provider_alias_to_many_model_aliases": {
                "ocg-alias": {
                    "provider": "opencode-go",
                    "fast": {"match": "x", "matchy": "y"},
                },
            },
        },
    )


def test_group_unknown_filter_key():
    _expect_error(
        "provider_alias_to_many_model_aliases.ocg-alias.fast.filter",
        {
            "provider_alias_to_many_model_aliases": {
                "ocg-alias": {
                    "provider": "opencode-go",
                    "fast": {"match": "x", "filter": {"miniContext": 1}},
                },
            },
        },
    )


def test_group_unknown_capabilities_key():
    _expect_error(
        "provider_alias_to_many_model_aliases.ocg-alias.fast.filter.capabilities",
        {
            "provider_alias_to_many_model_aliases": {
                "ocg-alias": {
                    "provider": "opencode-go",
                    "fast": {"match": "x", "filter": {"capabilities": {"toolz": True}}},
                },
            },
        },
    )


def test_group_missing_match():
    _expect_error(
        "provider_alias_to_many_model_aliases.ocg-alias.fast.match",
        {
            "provider_alias_to_many_model_aliases": {
                "ocg-alias": {"provider": "opencode-go", "fast": {}},
            },
        },
    )


def test_group_empty_match_list():
    _expect_error(
        "provider_alias_to_many_model_aliases.ocg-alias.fast.match",
        {
            "provider_alias_to_many_model_aliases": {
                "ocg-alias": {"provider": "opencode-go", "fast": {"match": []}},
            },
        },
    )


def test_group_strategy_not_latest():
    _expect_error(
        "provider_alias_to_many_model_aliases.ocg-alias.fast.select.strategy",
        {
            "provider_alias_to_many_model_aliases": {
                "ocg-alias": {
                    "provider": "opencode-go",
                    "fast": {"match": "x", "select": {"strategy": "oldest"}},
                },
            },
        },
    )


def test_group_min_context_non_positive():
    _expect_error(
        "provider_alias_to_many_model_aliases.ocg-alias.fast.filter.minContext",
        {
            "provider_alias_to_many_model_aliases": {
                "ocg-alias": {
                    "provider": "opencode-go",
                    "fast": {"match": "x", "filter": {"minContext": 0}},
                },
            },
        },
    )
    _expect_error(
        "provider_alias_to_many_model_aliases.ocg-alias.fast.filter.minContext",
        {
            "provider_alias_to_many_model_aliases": {
                "ocg-alias": {
                    "provider": "opencode-go",
                    "fast": {"match": "x", "filter": {"minContext": "128k"}},
                },
            },
        },
    )


def test_group_min_age_bad_format():
    _expect_error(
        "provider_alias_to_many_model_aliases.ocg-alias.fast.filter.minAge",
        {
            "provider_alias_to_many_model_aliases": {
                "ocg-alias": {
                    "provider": "opencode-go",
                    "fast": {"match": "x", "filter": {"minAge": "2 days"}},
                },
            },
        },
    )


def test_group_tools_not_bool():
    _expect_error(
        "provider_alias_to_many_model_aliases.ocg-alias.fast.filter.capabilities.tools",
        {
            "provider_alias_to_many_model_aliases": {
                "ocg-alias": {
                    "provider": "opencode-go",
                    "fast": {"match": "x", "filter": {"capabilities": {"tools": "yes"}}},
                },
            },
        },
    )


def test_group_alias_value_not_mapping():
    _expect_error(
        "provider_alias_to_many_model_aliases.ocg-alias.fast",
        {
            "provider_alias_to_many_model_aliases": {
                "ocg-alias": {"provider": "opencode-go", "fast": "glm-*"},
            },
        },
    )


def test_group_bad_types_in_filter():
    _expect_error(
        "provider_alias_to_many_model_aliases.ocg-alias.fast.filter",
        {
            "provider_alias_to_many_model_aliases": {
                "ocg-alias": {
                    "provider": "opencode-go",
                    "fast": {"match": "x", "filter": ["minContext"]},
                },
            },
        },
    )
    _expect_error(
        "provider_alias_to_many_model_aliases.ocg-alias.fast.filter.capabilities",
        {
            "provider_alias_to_many_model_aliases": {
                "ocg-alias": {
                    "provider": "opencode-go",
                    "fast": {"match": "x", "filter": {"capabilities": []}},
                },
            },
        },
    )


def test_group_bad_modalities_types():
    _expect_error(
        "provider_alias_to_many_model_aliases.ocg-alias.fast.filter.capabilities.input",
        {
            "provider_alias_to_many_model_aliases": {
                "ocg-alias": {
                    "provider": "opencode-go",
                    "fast": {"match": "x", "filter": {"capabilities": {"input": 123}}},
                },
            },
        },
    )
    _expect_error(
        "provider_alias_to_many_model_aliases.ocg-alias.fast.filter.capabilities.output",
        {
            "provider_alias_to_many_model_aliases": {
                "ocg-alias": {
                    "provider": "opencode-go",
                    "fast": {"match": "x", "filter": {"capabilities": {"output": {}}}},
                },
            },
        },
    )


def test_group_pattern_items_are_stripped():
    cfg = parse({
        "provider_alias_to_many_model_aliases": {
            "ocg-alias": {
                "provider": "opencode-go",
                "a": {"match": ["  glm-* "], "exclude": [" *-preview  "]},
            },
        },
    })
    assert cfg.groups["ocg-alias"].aliases["a"].match == ("glm-*",)
    assert cfg.groups["ocg-alias"].aliases["a"].exclude == ("*-preview",)


def test_group_padding_only_items_rejected():
    _expect_error(
        "provider_alias_to_many_model_aliases.ocg-alias.a.match[0]",
        {
            "provider_alias_to_many_model_aliases": {
                "ocg-alias": {
                    "provider": "opencode-go",
                    "a": {"match": ["   "]},
                },
            },
        },
    )


# --- alias spec errors (global scope) ---------------------------------------


def test_global_unknown_alias_value_key():
    _expect_error(
        "model_alias_to_all_provider_aliases.glm-latest",
        {"model_alias_to_all_provider_aliases": {"glm-latest": {"match": "x", "matchy": "y"}}},
    )


def test_global_missing_match():
    _expect_error(
        "model_alias_to_all_provider_aliases.glm-latest.match",
        {"model_alias_to_all_provider_aliases": {"glm-latest": {}}},
    )


def test_global_alias_value_not_mapping():
    _expect_error(
        "model_alias_to_all_provider_aliases.glm-latest",
        {"model_alias_to_all_provider_aliases": {"glm-latest": "glm-*"}},
    )


def test_global_not_mapping():
    _expect_error(
        "model_alias_to_all_provider_aliases",
        {"model_alias_to_all_provider_aliases": ["glm-latest"]},
    )

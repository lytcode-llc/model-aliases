"""config.parse: flat v2 schema defaults, round-trip, and ConfigErrors."""

from __future__ import annotations

import pytest

from model_aliases.config import ConfigError, parse


def _full() -> dict:
    return {
        "wrap_all": True,
        "strict": True,
        "debug": True,
        "refresh_seconds": 120,
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
        "local": {"match": ["qwen*"]},
    }


def test_parse_empty_is_defaults():
    cfg = parse({})
    assert cfg.wrap_all is False
    assert cfg.strict is False
    assert cfg.debug is False
    assert cfg.refresh_seconds == 3600
    assert cfg.aliases == {}
    assert parse(None).aliases == {}


def test_parse_full_roundtrip():
    cfg = parse(_full())
    assert cfg.wrap_all is True
    assert cfg.strict is True
    assert cfg.debug is True
    assert cfg.refresh_seconds == 120
    assert list(cfg.aliases) == ["fast", "stable", "local"]
    fast = cfg.aliases["fast"]
    assert fast.match == ("glm-*",)
    assert fast.exclude == ("*-preview", "glm-legacy*")
    assert fast.min_context == 128000
    assert fast.tools is True
    assert fast.input_modalities == ("text", "image")
    assert fast.output_modalities == ("text",)
    assert fast.min_age_hours == 48
    assert cfg.aliases["stable"].match == ("glm-4.7*",)
    assert cfg.aliases["local"].match == ("qwen*",)


def test_min_age_units():
    cfg = parse({"a": {"match": "x*", "filter": {"minAge": "3d"}}})
    assert cfg.aliases["a"].min_age_hours == 72
    cfg2 = parse({"a": {"match": "x*", "filter": {"minAge": "12h"}}})
    assert cfg2.aliases["a"].min_age_hours == 12


def _expect_error(fragment: str, raw: dict):
    with pytest.raises(ConfigError) as ei:
        parse(raw)
    assert fragment in str(ei.value), f"{fragment!r} not in {str(ei.value)!r}"


def test_top_level_keys_are_aliases_by_definition():
    # A typo at the top level is now just an alias slug; it only fails when its value is malformed.
    cfg = parse({"stric": {"match": "x*"}})
    assert "stric" in cfg.aliases


def test_typo_in_reserved_scalar_still_errors():
    _expect_error("strict", {"strict": "yes"})
    _expect_error("wrap_all", {"wrap_all": "yes"})
    _expect_error("debug", {"debug": "yes"})
    _expect_error("refresh_seconds", {"refresh_seconds": 0})
    _expect_error("refresh_seconds", {"refresh_seconds": True})


def test_unknown_alias_value_key():
    _expect_error("model_aliases.glm-latest", {"glm-latest": {"match": "x", "matchy": "y"}})


def test_unknown_filter_key():
    _expect_error(
        "model_aliases.fast.filter",
        {"fast": {"match": "x", "filter": {"miniContext": 1}}},
    )


def test_unknown_capabilities_key():
    _expect_error(
        "model_aliases.fast.filter.capabilities",
        {"fast": {"match": "x", "filter": {"capabilities": {"toolz": True}}}},
    )


def test_missing_match():
    _expect_error("model_aliases.fast.match", {"fast": {}})


def test_empty_match_list():
    _expect_error("model_aliases.fast.match", {"fast": {"match": []}})


def test_strategy_not_latest():
    _expect_error(
        "model_aliases.fast.select.strategy",
        {"fast": {"match": "x", "select": {"strategy": "oldest"}}},
    )


def test_min_context_non_positive():
    _expect_error(
        "model_aliases.fast.filter.minContext",
        {"fast": {"match": "x", "filter": {"minContext": 0}}},
    )
    _expect_error(
        "model_aliases.fast.filter.minContext",
        {"fast": {"match": "x", "filter": {"minContext": "128k"}}},
    )


def test_min_age_bad_format():
    _expect_error(
        "model_aliases.fast.filter.minAge",
        {"fast": {"match": "x", "filter": {"minAge": "2 days"}}},
    )


def test_tools_not_bool():
    _expect_error(
        "model_aliases.fast.filter.capabilities.tools",
        {"fast": {"match": "x", "filter": {"capabilities": {"tools": "yes"}}}},
    )


def test_alias_value_not_mapping():
    _expect_error("model_aliases.fast", {"fast": "glm-*"})


def test_bad_types_in_filter():
    _expect_error("model_aliases.fast.filter", {"fast": {"match": "x", "filter": ["minContext"]}})
    _expect_error(
        "model_aliases.fast.filter.capabilities",
        {"fast": {"match": "x", "filter": {"capabilities": []}}},
    )


def test_bad_modalities_types():
    _expect_error(
        "model_aliases.fast.filter.capabilities.input",
        {"fast": {"match": "x", "filter": {"capabilities": {"input": 123}}}},
    )
    _expect_error(
        "model_aliases.fast.filter.capabilities.output",
        {"fast": {"match": "x", "filter": {"capabilities": {"output": {}}}}},
    )


def test_pattern_items_are_stripped():
    cfg = parse({"a": {"match": ["  glm-* "], "exclude": [" *-preview  "]}})
    assert cfg.aliases["a"].match == ("glm-*",)
    assert cfg.aliases["a"].exclude == ("*-preview",)
    # padding-only items are rejected, not silently dropped
    _expect_error("model_aliases.a.match[0]", {"a": {"match": ["   "]}})

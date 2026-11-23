"""config.parse: defaults, full round-trip, and path-bearing ConfigErrors."""

from __future__ import annotations

import pytest

from model_aliases.config import ConfigError, parse


def _full() -> dict:
    return {
        "strict": True,
        "debug": True,
        "refresh_seconds": 120,
        "targets": {
            "oc": {
                "provider": "opencode-go",
                "aliases": {
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
            "work": {
                "provider": "custom:work",
                "aliases": {"local": {"match": ["qwen*"]}},
            },
        },
    }


def test_parse_empty_is_defaults():
    cfg = parse({})
    assert cfg.strict is False
    assert cfg.debug is False
    assert cfg.refresh_seconds == 3600
    assert cfg.targets == {}
    assert parse(None).targets == {}


def test_parse_full_roundtrip():
    cfg = parse(_full())
    assert cfg.strict is True and cfg.debug is True and cfg.refresh_seconds == 120
    assert list(cfg.targets) == ["oc", "work"]
    oc = cfg.targets["oc"]
    assert oc.provider == "opencode-go" and oc.key == "oc"
    assert list(oc.aliases) == ["fast", "stable"]
    fast = oc.aliases["fast"]
    assert fast.match == ("glm-*",)
    assert fast.exclude == ("*-preview", "glm-legacy*")
    assert fast.min_context == 128000
    assert fast.tools is True
    assert fast.input_modalities == ("text", "image")
    assert fast.output_modalities == ("text",)
    assert fast.min_age_hours == 48
    assert cfg.targets["work"].provider == "custom:work"


def test_min_age_units():
    cfg = parse({"targets": {"t": {"provider": "p", "aliases": {"a": {"match": "x*", "filter": {"minAge": "3d"}}}}}})
    assert cfg.targets["t"].aliases["a"].min_age_hours == 72


def _expect_error(fragment: str, raw: dict):
    with pytest.raises(ConfigError) as ei:
        parse(raw)
    assert fragment in str(ei.value), f"{fragment!r} not in {str(ei.value)!r}"


def test_unknown_top_level_key():
    _expect_error("model_aliases", {"stric": True})


def test_unknown_target_key():
    _expect_error("targets.oc", {"targets": {"oc": {"provider": "p", "alias": {}}}})


def test_unknown_alias_key():
    _expect_error("targets.oc.aliases.fast", {"targets": {"oc": {"provider": "p", "aliases": {"fast": {"match": "x", "matchy": "y"}}}}})


def test_unknown_filter_key():
    _expect_error("targets.oc.aliases.fast.filter", {"targets": {"oc": {"provider": "p", "aliases": {"fast": {"match": "x", "filter": {"miniContext": 1}}}}}})


def test_missing_match():
    _expect_error("targets.oc.aliases.fast.match", {"targets": {"oc": {"provider": "p", "aliases": {"fast": {}}}}})


def test_empty_match_list():
    _expect_error("targets.oc.aliases.fast.match", {"targets": {"oc": {"provider": "p", "aliases": {"fast": {"match": []}}}}})


def test_missing_provider():
    _expect_error("targets.oc.provider", {"targets": {"oc": {"aliases": {"a": {"match": "x"}}}}})


def test_empty_aliases():
    _expect_error("targets.oc.aliases", {"targets": {"oc": {"provider": "p", "aliases": {}}}})


def test_strategy_not_latest():
    _expect_error("strategy", {"targets": {"oc": {"provider": "p", "aliases": {"a": {"match": "x", "select": {"strategy": "oldest"}}}}}})


def test_min_context_non_positive():
    _expect_error("minContext", {"targets": {"oc": {"provider": "p", "aliases": {"a": {"match": "x", "filter": {"minContext": 0}}}}}})


def test_min_age_bad_format():
    _expect_error("minAge", {"targets": {"oc": {"provider": "p", "aliases": {"a": {"match": "x", "filter": {"minAge": "2 days"}}}}}})


def test_strict_not_bool():
    _expect_error("strict", {"strict": "yes"})


def test_refresh_not_positive_int():
    _expect_error("refresh_seconds", {"refresh_seconds": 0})
    _expect_error("refresh_seconds", {"refresh_seconds": True})


def test_tools_not_bool():
    _expect_error("tools", {"targets": {"oc": {"provider": "p", "aliases": {"a": {"match": "x", "filter": {"capabilities": {"tools": "yes"}}}}}}})


def test_alias_value_not_mapping():
    _expect_error("targets.oc.aliases.fast", {"targets": {"oc": {"provider": "p", "aliases": {"fast": "glm-*"}}}})


def test_target_key_must_be_slug_safe():
    # colliding/unsafe keys would clobber the derived profile and cache names
    _expect_error("targets", {"targets": {"A B": {"provider": "p", "aliases": {"a": {"match": "x"}}}}})
    _expect_error("targets", {"targets": {"UPPER": {"provider": "p", "aliases": {"a": {"match": "x"}}}}})
    _expect_error("targets", {"targets": {"-lead": {"provider": "p", "aliases": {"a": {"match": "x"}}}}})
    # valid shapes still pass
    assert parse({"targets": {"a.b_1-c": {"provider": "p", "aliases": {"a": {"match": "x"}}}}}).targets


def test_pattern_items_are_stripped():
    cfg = parse({"targets": {"oc": {"provider": "p", "aliases": {"a": {"match": ["  glm-* "], "exclude": [" *-preview  "]}}}}})
    spec = cfg.targets["oc"].aliases["a"]
    assert spec.match == ("glm-*",)
    assert spec.exclude == ("*-preview",)
    # padding-only items are rejected, not silently dropped
    _expect_error("match[0]", {"targets": {"oc": {"provider": "p", "aliases": {"a": {"match": ["   "]}}}}})


def test_bad_types_in_filter():
    _expect_error("filter", {"targets": {"oc": {"provider": "p", "aliases": {"a": {"match": "x", "filter": ["minContext"]}}}}})
    _expect_error("capabilities", {"targets": {"oc": {"provider": "p", "aliases": {"a": {"match": "x", "filter": {"capabilities": []}}}}}})

"""config.parse: the groups-only schema (global scope shelved — see PLAN.md BACKLOG)."""

from __future__ import annotations

import pytest

from model_aliases.config import ConfigError, parse


def _full() -> dict:
    return {
        "strict": True,
        "debug": True,
        "refresh_seconds": 120,
        "provider_alias_to_many_model_aliases": {
            "ocg-alias": {
                "provider": "opencode-go",
                "glm-latest": {
                    "match": "opencode-go/glm-*",
                    "exclude": ["*-preview"],
                    "filter": {
                        "minContext": 128000,
                        "capabilities": {"tools": True, "input": ["text", "image"], "output": "text"},
                        "minAge": "48h",
                    },
                    "select": {"strategy": "latest"},
                },
                "stable": {"match": ["glm-4.7*"]},
            },
            "fw-alias": {
                "provider": "accounts/fireworks/models",
                "kimi-latest": {"match": ["kimi-*"]},
            },
        },
    }


def test_parse_empty_is_defaults():
    cfg = parse({})
    assert cfg.strict is False
    assert cfg.debug is False
    assert cfg.refresh_seconds == 3600
    assert cfg.groups == {}


def test_parse_full_roundtrip():
    cfg = parse(_full())
    assert cfg.strict is True and cfg.debug is True and cfg.refresh_seconds == 120
    assert list(cfg.groups) == ["ocg-alias", "fw-alias"]
    ocg = cfg.groups["ocg-alias"]
    assert ocg.provider == "opencode-go" and ocg.key == "ocg-alias"
    assert list(ocg.aliases) == ["glm-latest", "stable"]
    fast = ocg.aliases["glm-latest"]
    assert fast.match == ("opencode-go/glm-*",)
    assert fast.exclude == ("*-preview",)
    assert fast.min_context == 128000
    assert fast.tools is True
    assert fast.input_modalities == ("text", "image")
    assert fast.output_modalities == ("text",)
    assert fast.min_age_hours == 48
    assert cfg.groups["fw-alias"].provider == "accounts/fireworks/models"
    assert cfg.groups["fw-alias"].aliases["kimi-latest"].match == ("kimi-*",)


def test_min_age_units():
    cfg = parse({
        "provider_alias_to_many_model_aliases": {
            "g": {"provider": "p", "a": {"match": "x*", "filter": {"minAge": "3d"}}},
        }
    })
    assert cfg.groups["g"].aliases["a"].min_age_hours == 72


def test_provider_value_is_stripped():
    cfg = parse({"provider_alias_to_many_model_aliases": {"g": {"provider": "  opencode-go  ", "a": {"match": "x*"}}}})
    assert cfg.groups["g"].provider == "opencode-go"


def _expect_error(fragment: str, raw: dict):
    with pytest.raises(ConfigError) as ei:
        parse(raw)
    assert fragment in str(ei.value), f"{fragment!r} not in {str(ei.value)!r}"


def test_global_scope_key_rejected():
    """The shelved feature: model_alias_to_all_provider_aliases is no longer a
    valid key (restoration spec lives in PLAN.md BACKLOG)."""
    _expect_error("model_alias_to_all_provider_aliases", {
        "model_alias_to_all_provider_aliases": {"glm-latest": {"match": ["glm-*"]}},
    })


def test_wrap_all_key_rejected():
    """wrap_all existed only to cover the global scope; both are shelved."""
    _expect_error("wrap_all", {"wrap_all": True})


def test_unknown_top_level_key():
    _expect_error("model_aliases", {"stric": True})


def test_unknown_group_key():
    _expect_error("provider_alias_to_many_model_aliases.ocg", {
        "provider_alias_to_many_model_aliases": {"ocg": {"provider": "p", "alias": {}}},
    })


def test_unknown_alias_key():
    _expect_error("provider_alias_to_many_model_aliases.ocg.fast", {
        "provider_alias_to_many_model_aliases": {
            "ocg": {"provider": "p", "fast": {"matchy": ["x"]}},
        },
    })


def test_missing_match():
    _expect_error("match", {
        "provider_alias_to_many_model_aliases": {"ocg": {"provider": "p", "fast": {}}},
    })


def test_empty_match_list():
    _expect_error("match", {
        "provider_alias_to_many_model_aliases": {"ocg": {"provider": "p", "fast": {"match": []}}},
    })


def test_missing_provider():
    _expect_error("provider", {
        "provider_alias_to_many_model_aliases": {"ocg": {"fast": {"match": "x"}}},
    })


def test_empty_group():
    _expect_error("group must declare at least one model alias", {
        "provider_alias_to_many_model_aliases": {"ocg": {"provider": "accounts/fireworks/models"}},
    })


def test_empty_group_message_is_actionable():
    with pytest.raises(ConfigError) as ei:
        parse({"provider_alias_to_many_model_aliases": {"fw-alias": {"provider": "p"}}})
    assert "commented out" in str(ei.value)


def test_group_value_not_mapping():
    _expect_error("expected a mapping", {
        "provider_alias_to_many_model_aliases": {"ocg": "bad"},
    })


def test_strategy_not_latest():
    _expect_error("strategy", {
        "provider_alias_to_many_model_aliases": {
            "ocg": {"provider": "p", "a": {"match": "x", "select": {"strategy": "oldest"}}},
        },
    })


def test_min_context_non_positive():
    _expect_error("minContext", {
        "provider_alias_to_many_model_aliases": {
            "ocg": {"provider": "p", "a": {"match": "x", "filter": {"minContext": 0}}},
        },
    })


def test_min_age_bad_format():
    _expect_error("minAge", {
        "provider_alias_to_many_model_aliases": {
            "ocg": {"provider": "p", "a": {"match": "x", "filter": {"minAge": "2 days"}}},
        },
    })


def test_strict_not_bool():
    _expect_error("strict", {"strict": "yes"})


def test_refresh_not_positive_int():
    _expect_error("refresh_seconds", {"refresh_seconds": 0})
    _expect_error("refresh_seconds", {"refresh_seconds": True})


def test_tools_not_bool():
    _expect_error("tools", {
        "provider_alias_to_many_model_aliases": {
            "ocg": {"provider": "p", "a": {"match": "x", "filter": {"capabilities": {"tools": "yes"}}}},
        },
    })


def test_alias_value_not_mapping():
    _expect_error("expected a mapping", {
        "provider_alias_to_many_model_aliases": {"ocg": {"provider": "p", "fast": "glm-*"}},
    })


def test_bad_types_in_filter():
    _expect_error("filter", {
        "provider_alias_to_many_model_aliases": {
            "ocg": {"provider": "p", "a": {"match": "x", "filter": ["minContext"]}},
        },
    })
    _expect_error("capabilities", {
        "provider_alias_to_many_model_aliases": {
            "ocg": {"provider": "p", "a": {"match": "x", "filter": {"capabilities": []}}},
        },
    })

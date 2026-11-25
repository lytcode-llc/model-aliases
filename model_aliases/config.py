"""Parse the two-scope ``model_aliases:`` section of ~/.hermes/config.yaml (pure).

v3 schema: developer-named provider-alias groups (each group's ``provider``
is the model-slug prefix identifying its target) plus a global scope that
resolves on every wrapped provider. No model slugs, patterns, alias names or
provider references are hard-coded here — everything user-specific lives in
config.yaml.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


class ConfigError(ValueError):
    """Invalid model_aliases config; the message includes the offending key path."""


@dataclass(frozen=True)
class AliasSpec:
    """One alias: what to match, what to filter out, how to pick the winner."""

    match: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    min_context: int | None = None
    tools: bool | None = None
    input_modalities: tuple[str, ...] | None = None
    output_modalities: tuple[str, ...] | None = None
    min_age_hours: int | None = None


@dataclass(frozen=True)
class ProviderGroup:
    """A developer-named provider-alias group."""

    key: str  # developer-chosen provider alias name (e.g. ocg-alias)
    provider: str  # model-slug prefix identifying the target provider
    aliases: dict[str, AliasSpec]


@dataclass(frozen=True)
class AliasesConfig:
    wrap_all: bool = False
    strict: bool = False
    debug: bool = False
    refresh_seconds: int = 3600
    groups: dict[str, ProviderGroup] = field(default_factory=dict)
    aliases: dict[str, AliasSpec] = field(default_factory=dict)


_MIN_AGE_RE = re.compile(r"^[0-9]+(h|d)$")


def _err(path: str, msg: str) -> None:
    raise ConfigError(f"{path}: {msg}")


def _unknown(path: str, raw: dict, allowed: set[str]) -> None:
    extra = set(raw) - allowed
    if extra:
        _err(path, f"unknown key(s): {', '.join(sorted(str(k) for k in extra))}")


def _as_str_tuple(value: Any, path: str) -> tuple[str, ...]:
    if isinstance(value, str):
        items: list[Any] = [value]
    elif isinstance(value, list):
        items = value
    else:
        _err(path, f"expected string or list of strings, got {type(value).__name__}")
        raise AssertionError  # unreachable
    if not items:
        _err(path, "must not be empty")
    out: list[str] = []
    for i, item in enumerate(items):
        if not isinstance(item, str) or not item.strip():
            _err(f"{path}[{i}]", "expected a non-empty string")
        out.append(item.strip())
    return tuple(out)


def _parse_alias(path: str, raw: Any) -> AliasSpec:
    if not isinstance(raw, dict):
        _err(path, f"expected a mapping, got {type(raw).__name__}")
    assert isinstance(raw, dict)
    _unknown(path, raw, {"match", "exclude", "filter", "select"})
    if "match" not in raw:
        _err(f"{path}.match", "required")
    match = _as_str_tuple(raw["match"], f"{path}.match")
    exclude = _as_str_tuple(raw["exclude"], f"{path}.exclude") if "exclude" in raw else ()

    min_context: int | None = None
    tools: bool | None = None
    input_modalities: tuple[str, ...] | None = None
    output_modalities: tuple[str, ...] | None = None
    min_age_hours: int | None = None

    if "filter" in raw:
        fpath = f"{path}.filter"
        flt = raw["filter"]
        if not isinstance(flt, dict):
            _err(fpath, f"expected a mapping, got {type(flt).__name__}")
        _unknown(fpath, flt, {"minContext", "capabilities", "minAge"})
        if "minContext" in flt:
            mc = flt["minContext"]
            if not isinstance(mc, int) or isinstance(mc, bool) or mc <= 0:
                _err(f"{fpath}.minContext", "expected a positive integer")
            min_context = mc
        if "minAge" in flt:
            ma = flt["minAge"]
            if not isinstance(ma, str) or not _MIN_AGE_RE.match(ma):
                _err(f"{fpath}.minAge", "expected '<n>h' or '<n>d', e.g. '48h' or '3d'")
            amount = int(ma[:-1])
            min_age_hours = amount if ma.endswith("h") else amount * 24
        if "capabilities" in flt:
            cpath = f"{fpath}.capabilities"
            caps = flt["capabilities"]
            if not isinstance(caps, dict):
                _err(cpath, f"expected a mapping, got {type(caps).__name__}")
            _unknown(cpath, caps, {"tools", "input", "output"})
            if "tools" in caps:
                if not isinstance(caps["tools"], bool):
                    _err(f"{cpath}.tools", "expected a boolean")
                tools = caps["tools"]
            if "input" in caps:
                input_modalities = _as_str_tuple(caps["input"], f"{cpath}.input")
            if "output" in caps:
                output_modalities = _as_str_tuple(caps["output"], f"{cpath}.output")

    if "select" in raw:
        spath = f"{path}.select"
        sel = raw["select"]
        if not isinstance(sel, dict):
            _err(spath, f"expected a mapping, got {type(sel).__name__}")
        _unknown(spath, sel, {"strategy"})
        strategy = sel.get("strategy")
        if strategy != "latest":
            _err(f"{spath}.strategy", f"only 'latest' is supported, got {strategy!r}")

    return AliasSpec(
        match=match,
        exclude=exclude,
        min_context=min_context,
        tools=tools,
        input_modalities=input_modalities,
        output_modalities=output_modalities,
        min_age_hours=min_age_hours,
    )


def _parse_group(path: str, key: str, raw: Any) -> ProviderGroup:
    if not isinstance(raw, dict):
        _err(path, f"expected a mapping, got {type(raw).__name__}")
    # 'provider' is reserved; every other key is an alias slug parsed below
    # (its value shape is validated by _parse_alias).
    if "provider" not in raw:
        _err(f"{path}.provider", "required (the model-slug prefix identifying the provider)")
    prefix = raw["provider"]
    if not isinstance(prefix, str) or not prefix.strip():
        _err(f"{path}.provider", "expected a non-empty string")
    aliases: dict[str, AliasSpec] = {}
    for slug, spec_raw in raw.items():
        if slug == "provider":
            continue
        apath = f"{path}.{slug}"
        aliases[slug] = _parse_alias(apath, spec_raw)
    if not aliases:
        _err(path, "group must declare at least one model alias")
    return ProviderGroup(key=key, provider=prefix.strip(), aliases=aliases)


def parse(raw: dict | None) -> AliasesConfig:
    """Parse the ``model_aliases:`` section. ``{}``/``None`` -> defaults (no aliases)."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        _err("model_aliases", f"expected a mapping, got {type(raw).__name__}")
    _unknown(
        "model_aliases",
        raw,
        {"provider_alias_to_many_model_aliases", "model_alias_to_all_provider_aliases",
         "wrap_all", "strict", "debug", "refresh_seconds"},
    )

    wrap_all = raw.get("wrap_all", False)
    if not isinstance(wrap_all, bool):
        _err("wrap_all", "expected a boolean")
    strict = raw.get("strict", False)
    if not isinstance(strict, bool):
        _err("strict", "expected a boolean")
    debug = raw.get("debug", False)
    if not isinstance(debug, bool):
        _err("debug", "expected a boolean")
    refresh = raw.get("refresh_seconds", 3600)
    if not isinstance(refresh, int) or isinstance(refresh, bool) or refresh <= 0:
        _err("refresh_seconds", "expected a positive integer")

    groups: dict[str, ProviderGroup] = {}
    groups_raw = raw.get("provider_alias_to_many_model_aliases", {})
    if not isinstance(groups_raw, dict):
        _err("provider_alias_to_many_model_aliases", f"expected a mapping, got {type(groups_raw).__name__}")
    for gkey, gval in groups_raw.items():
        gpath = f"provider_alias_to_many_model_aliases.{gkey}"
        if not isinstance(gkey, str) or not gkey.strip():
            _err("provider_alias_to_many_model_aliases", "group names must be non-empty strings")
        groups[gkey] = _parse_group(gpath, gkey, gval)

    aliases: dict[str, AliasSpec] = {}
    globals_raw = raw.get("model_alias_to_all_provider_aliases", {})
    if not isinstance(globals_raw, dict):
        _err("model_alias_to_all_provider_aliases", f"expected a mapping, got {type(globals_raw).__name__}")
    for slug, spec_raw in globals_raw.items():
        apath = f"model_alias_to_all_provider_aliases.{slug}"
        aliases[slug] = _parse_alias(apath, spec_raw)

    return AliasesConfig(
        wrap_all=wrap_all,
        strict=strict,
        debug=debug,
        refresh_seconds=refresh,
        groups=groups,
        aliases=aliases,
    )

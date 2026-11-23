"""Pure configuration model for the ``model_aliases:`` config section."""

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
class TargetSpec:
    key: str
    provider: str
    aliases: dict[str, AliasSpec]


@dataclass(frozen=True)
class AliasesConfig:
    strict: bool = False
    debug: bool = False
    refresh_seconds: int = 3600
    targets: dict[str, TargetSpec] = field(default_factory=dict)


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
        assert isinstance(flt, dict)
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
        assert isinstance(sel, dict)
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


def parse(raw: dict | None) -> AliasesConfig:
    """Parse the ``model_aliases:`` section. ``{}``/``None`` -> defaults (no targets)."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        _err("model_aliases", f"expected a mapping, got {type(raw).__name__}")
    assert isinstance(raw, dict)
    _unknown("model_aliases", raw, {"strict", "debug", "refresh_seconds", "targets"})

    strict = raw.get("strict", False)
    if not isinstance(strict, bool):
        _err("strict", "expected a boolean")
    debug = raw.get("debug", False)
    if not isinstance(debug, bool):
        _err("debug", "expected a boolean")
    refresh = raw.get("refresh_seconds", 3600)
    if not isinstance(refresh, int) or isinstance(refresh, bool) or refresh <= 0:
        _err("refresh_seconds", "expected a positive integer")

    targets_raw = raw.get("targets", {})
    if not isinstance(targets_raw, dict):
        _err("targets", f"expected a mapping, got {type(targets_raw).__name__}")

    targets: dict[str, TargetSpec] = {}
    for tkey, tval in targets_raw.items():
        tpath = f"targets.{tkey}"
        # Lowercase slug-safe keys only: _profile_name() derives the registry and
        # cache names from the key, so colliding spellings ("A B" vs "a-b") must
        # be rejected here rather than silently clobber each other later.
        if not isinstance(tkey, str) or not re.match(r"^[a-z0-9][a-z0-9._-]*$", tkey):
            _err("targets", "keys must match ^[a-z0-9][a-z0-9._-]*$ (lowercase; no spaces)")
        if not isinstance(tval, dict):
            _err(tpath, f"expected a mapping, got {type(tval).__name__}")
        _unknown(tpath, tval, {"provider", "aliases"})
        provider = tval.get("provider")
        if not isinstance(provider, str) or not provider.strip():
            _err(f"{tpath}.provider", "required non-empty string")
        aliases_raw = tval.get("aliases")
        if not isinstance(aliases_raw, dict) or not aliases_raw:
            _err(f"{tpath}.aliases", "required non-empty mapping")
        aliases: dict[str, AliasSpec] = {}
        for aname, aval in aliases_raw.items():
            apath = f"{tpath}.aliases.{aname}"
            if not isinstance(aname, str) or not aname.strip():
                _err(f"{tpath}.aliases", "keys must be non-empty strings")
            aliases[aname] = _parse_alias(apath, aval)
        targets[tkey] = TargetSpec(key=tkey, provider=provider, aliases=aliases)

    return AliasesConfig(strict=strict, debug=debug, refresh_seconds=refresh, targets=targets)

"""Registration orchestration: config -> targets -> resolve -> wrap -> register.

Called by the plugin root ``__init__.py`` at import (Hermes' loader contract:
``register_provider()`` at import). v3: developer-named provider-alias groups
(each group's ``provider`` value is the model-slug prefix identifying its
target) plus a global alias scope. One wrapped profile per TARGET provider,
claimed under the real provider name, every group name targeting it, and the
custom forms — so every consumer block just writes the developer's chosen
provider alias.

Wraps exist ONLY for provider-alias groups (the global scope is shelved — see
PLAN.md BACKLOG). Tolerant by default: a target that cannot be served or an
alias with no match on it logs loudly; ``model_aliases.strict`` turns any of it into a load
failure (nothing registered, Hermes logs the import error). Startup budget:
eager resolution runs concurrently across targets behind one shared
models.dev fetch — bounded well inside Hermes' 10s plugin load timeout.
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from . import state
from .catalog import (
    ProviderDescriptor,
    fetch_live_ids,
    fetch_models_dev,
    merge_candidates,
    models_dev_candidates,
)
from .config import AliasesConfig, AliasSpec, ConfigError, ProviderGroup, parse
from .profile import build_custom_profile, wrap_profile
from .resolver import resolve_aliases

logger = logging.getLogger("model-aliases")

_MODELS_DEV_MEMO: dict[str, Any] = {"ok": False, "catalog": None, "at": 0.0}
_MODELS_DEV_LOCK = threading.Lock()
_MEMO_FAILURE_RETRY_SECONDS = 60.0


class TargetError(RuntimeError):
    """A provider target cannot be served (unknown, or an alias resolved to nothing)."""


def _memo_models_dev() -> dict | None:
    """One models.dev fetch per process; failures retry after 60s. Never raises."""
    with _MODELS_DEV_LOCK:
        age = time.monotonic() - _MODELS_DEV_MEMO["at"]
        if _MODELS_DEV_MEMO["ok"]:
            return _MODELS_DEV_MEMO["catalog"]
        if age < _MEMO_FAILURE_RETRY_SECONDS and _MODELS_DEV_MEMO["at"] > 0.0:
            return None
        _MODELS_DEV_MEMO["at"] = time.monotonic()
        catalog = fetch_models_dev()
        if isinstance(catalog, dict):
            _MODELS_DEV_MEMO.update(ok=True, catalog=catalog)
            return catalog
        return None


def reset_models_dev_memo(catalog: dict | None = None) -> None:
    """Test seam: prime (catalog given) or clear the per-process memo."""
    with _MODELS_DEV_LOCK:
        _MODELS_DEV_MEMO.update(ok=catalog is not None, catalog=catalog, at=time.monotonic())


def _load_raw_section() -> tuple[dict, dict]:
    """(model_aliases section, full config dict) from the live Hermes config.
    Missing/unreadable config -> ({}, {})."""
    try:
        from hermes_cli.config import load_config

        full = load_config() or {}
    except Exception:
        logger.debug("model-aliases: hermes config unavailable", exc_info=True)
        return {}, {}
    if not isinstance(full, dict):
        return {}, {}
    section = full.get("model_aliases")
    return (section if isinstance(section, dict) else {}), full


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path.home() / ".hermes"


def _cache_key(provider_name: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", provider_name.strip().lower()).strip("-") or "default"


# --- naming guard (imports Hermes' family lists; never duplicates them) -----

def early_return_reason(name: str) -> str | None:
    """Why Hermes' normalize_model_for_provider would never reach a profile
    registered under this name, or None if it would.

    Two mechanisms, both imported from Hermes (never duplicated):
    1. The three early-return families (aggregators, opencode-*, anthropic).
    2. Provider-alias canonicalization (normalize_provider) — names like
       'local'/'ollama' collapse to 'custom' BEFORE any profile lookup.
    Import failure (unit tests without Hermes) -> None (guard skipped)."""
    try:
        from hermes_cli.model_normalize import (
            _AGGREGATOR_PROVIDERS,
            _DOT_TO_HYPHEN_PROVIDERS,
            _normalize_provider_alias,
        )
        # Canonical owner of family membership: models.py, not model_normalize
        # (the latter only imports it lazily inside its own function).
        from hermes_cli.models import opencode_provider_family
    except Exception:
        return None
    if name in _AGGREGATOR_PROVIDERS:
        return "an aggregator family"
    if name in _DOT_TO_HYPHEN_PROVIDERS:
        return "a dot-to-hyphen family"
    if opencode_provider_family(name) is not None:
        return "the OpenCode family"
    canonical = _normalize_provider_alias(name)
    if canonical != name:
        return f"provider-alias canonicalization (this name is normalized to {canonical!r} before any profile lookup)"
    return None


# --- provider discovery ------------------------------------------------------

def _is_provider_block(item: Any) -> bool:
    return isinstance(item, dict) and any(k in item for k in ("api", "base_url", "url"))


def _custom_entry_for(name: str, full: dict) -> dict | None:
    """A custom_providers list entry or providers: dict block for ``name``."""
    bare = name[7:] if name.lower().startswith("custom:") else name
    for block in (full.get("custom_providers"), full.get("providers")):
        if isinstance(block, list):
            for item in block:
                if isinstance(item, dict) and str(item.get("name") or "").strip() == bare:
                    return item
        elif isinstance(block, dict):
            item = block.get(bare)
            if _is_provider_block(item):
                return item
    return None


# --- slug-prefix -> provider ladder ------------------------------------------

def resolve_group_provider(group: ProviderGroup, full: dict) -> tuple[str, dict | None, Any, ProviderDescriptor]:
    """Resolve a group's slug prefix to its target: (name, entry, profile, descriptor).

    Ladder: (a) custom_providers entry whose name equals the group key (the
    name join); (b) bundled provider named by the prefix; (c) custom entry
    whose model/default_model slug starts with the prefix; (d) nothing ->
    TargetError."""
    from providers import get_provider_profile

    # (a) name join: the custom entry IS the developer's provider alias
    entry = _custom_entry_for(group.key, full)
    if entry is not None:
        prof = build_custom_profile(group.key, entry)
        if not prof.base_url:
            raise TargetError(f"provider alias {group.key!r}: custom entry has no api/base_url")
        desc = ProviderDescriptor(
            name=group.key,
            base_url=prof.base_url,
            models_dev_id=prof.models_dev_id,
            api_key_env=str(entry.get("key_env") or entry.get("api_key_env") or "").strip(),
        )
        return group.key, entry, prof, desc

    # (b) bundled provider named by the prefix
    prof = get_provider_profile(group.provider)
    if prof is not None:
        desc = ProviderDescriptor(
            name=group.provider,
            base_url=prof.base_url,
            models_url=prof.models_url,
            models_dev_id=prof.models_dev_id or "",
            api_key_env=next((v for v in (prof.env_vars or ()) if not str(v).endswith("_URL")), ""),
        )
        return group.provider, None, prof, desc

    # (c) custom entry whose model slug carries the prefix (boundary: the
    # prefix must end the slug or be followed by '/', so 'gpt' never matches
    # 'gptx-9b')
    prefix = group.provider
    matches: list[tuple[str, dict]] = []
    for block in (full.get("custom_providers"), full.get("providers")):
        items = block if isinstance(block, list) else list((block or {}).values())
        for item in items:
            if not isinstance(item, dict):
                continue
            slug = str(item.get("model") or item.get("default_model") or "").strip()
            if slug == prefix or slug.startswith(prefix + "/"):
                matches.append((str(item.get("name") or prefix).strip(), item))
    if len(matches) > 1:
        logger.warning(
            "model-aliases: slug prefix %r matches %d custom entries; using the first (%r)",
            prefix, len(matches), matches[0][0],
        )
    if matches:
        name, item = matches[0]
        prof = build_custom_profile(name, item)
        if not prof.base_url:
            raise TargetError(f"provider alias {group.key!r}: custom entry has no api/base_url")
        desc = ProviderDescriptor(
            name=name,
            base_url=prof.base_url,
            models_dev_id=prof.models_dev_id,
            api_key_env=str(item.get("key_env") or item.get("api_key_env") or "").strip(),
        )
        return name, item, prof, desc

    raise TargetError(
        f"provider alias {group.key!r}: slug prefix {group.provider!r} matches no bundled "
        "provider and no custom_providers entry"
    )


# --- per-target resolution ---------------------------------------------------

def _md_key(desc: ProviderDescriptor, catalog: dict | None) -> str:
    if not isinstance(catalog, dict):
        return ""
    for key in (desc.models_dev_id, desc.name):
        if key and isinstance(catalog.get(key), dict):
            return key
    return ""


def _capability_meta(catalog: dict | None, key: str, resolved: dict[str, str]) -> dict[str, dict]:
    caps: dict[str, dict] = {}
    if not isinstance(catalog, dict) or not key:
        return caps
    models = (catalog.get(key) or {}).get("models")
    if not isinstance(models, dict):
        return caps
    for alias, rid in resolved.items():
        entry = models.get(rid)
        if isinstance(entry, dict):
            caps[alias] = dict(entry)
    return caps


def _rid_meta(resolved: dict[str, str], caps: dict[str, dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for alias, entry in caps.items():
        rid = resolved.get(alias)
        if rid and isinstance(entry, dict):
            out[rid] = dict(entry)
    return out


def resolve_target(
    cfg: AliasesConfig,
    name: str,
    desc: ProviderDescriptor,
    cache_dir: Path,
    alias_map: dict[str, AliasSpec],
    *,
    now: float | None = None,
    catalog: dict | None = None,
    force_live: bool = False,
    live_timeout: float = 3.0,
) -> tuple[dict[str, str], dict, dict, list[str]]:
    """Resolve one target's alias scope (global + its groups) against its candidates.

    Returns (resolved, caps, rid_meta, real_ids). Warm cache (fresh + complete)
    short-circuits with zero network; otherwise models.dev (shared memo) +
    live /models. State keeps last-known-good on fetch failure and records
    drift; strict raises on any alias left unresolved."""
    now = time.time() if now is None else now
    prev = state.load(cache_dir, _cache_key(name))
    stale = prev is None or (now - prev.updated_at) >= cfg.refresh_seconds
    complete = prev is not None and all(a in prev.resolved for a in alias_map)

    if not force_live and complete and not stale:
        resolved = {a: prev.resolved[a] for a in alias_map if a in prev.resolved}
        caps = {a: dict(m) for a, m in prev.capabilities.items() if a in resolved}
        return resolved, caps, _rid_meta(resolved, caps), list(prev.real_ids)

    md = catalog if catalog is not None else _memo_models_dev()
    key = _md_key(desc, md)
    md_cands = models_dev_candidates(md, desc) if md else []
    new_map = resolve_aliases(alias_map, md_cands, provider=name)
    unresolved = [a for a in alias_map if not new_map.get(a)]
    live_rows = None
    cands = md_cands
    if force_live or stale or unresolved:
        live_rows = fetch_live_ids(desc, timeout=live_timeout)
        if live_rows is not None:
            cands = merge_candidates(md_cands, live_rows)
            new_map = resolve_aliases(alias_map, cands, provider=name)
    merged, drift = state.merge(prev, new_map, now=now)
    for d in drift:
        when = _dt.datetime.fromtimestamp(d["at"]).isoformat(timespec="seconds")
        logger.warning(
            "model-aliases: %s/%s: %s -> %s (detected %s)",
            name, d["alias"], d["from"], d["to"], when,
        )
    resolved = {a: merged.resolved[a] for a in alias_map if a in merged.resolved}
    caps = _capability_meta(md, key, resolved)
    if caps and caps != merged.capabilities:
        merged.capabilities = caps
    real_ids = [c.id for c in cands]
    if real_ids:
        merged.real_ids = real_ids
    changed = merged.resolved != (prev.resolved if prev else {}) or merged.real_ids != (prev.real_ids if prev else [])
    caps_changed = bool(caps) and caps != (prev.capabilities if prev else {})
    if prev is None or drift or changed or caps_changed or (live_rows is not None and stale):
        state.save(cache_dir, _cache_key(name), merged)
    unresolved = [a for a in alias_map if a not in resolved]
    if unresolved:
        msg = (
            f"provider {name!r}: alias(es) {', '.join(repr(u) for u in unresolved)} matched "
            "no models (check match globs against the catalog)"
        )
        if cfg.strict:
            raise TargetError(msg)
        logger.error("model-aliases: %s", msg)
    if cfg.debug:
        logger.info("model-aliases: %s resolved %s", name, resolved)
    final_caps = caps or merged.capabilities
    final_real = real_ids or list(merged.real_ids)
    return resolved, final_caps, _rid_meta(resolved, final_caps), final_real


def make_refresh(
    cfg: AliasesConfig,
    name: str,
    desc: ProviderDescriptor,
    cache_dir: Path,
    alias_map: dict[str, AliasSpec],
    *,
    timeout: float = 8.0,
) -> Callable[[float | None], tuple[dict, dict, dict, list] | None]:
    """Closure for wrapped fetch_models: force a live re-resolution and persist."""
    def refresh(live_timeout: float | None = None) -> tuple[dict, dict, dict, list] | None:
        try:
            budget = float(live_timeout) if live_timeout else timeout
            return resolve_target(
                cfg, name, desc, cache_dir, alias_map,
                force_live=True, live_timeout=budget,
            )
        except Exception:
            logger.debug("model-aliases: %s refresh failed", name, exc_info=True)
            return None
    return refresh


def _resolve_all(
    cfg: AliasesConfig,
    described: list[tuple[str, ProviderDescriptor, dict[str, AliasSpec]]],
    cache_dir: Path,
    errors: list[str],
) -> dict[int, tuple[dict, dict, dict, list]]:
    results: dict[int, tuple[dict, dict, dict, list]] = {}
    if len(described) > 1:
        with ThreadPoolExecutor(max_workers=min(6, len(described))) as pool:
            futures = [
                pool.submit(resolve_target, cfg, n, d, cache_dir, am)
                for n, d, am in described
            ]
            for i, fut in enumerate(futures):
                try:
                    results[i] = fut.result()
                except TargetError as e:
                    errors.append(str(e))
                except Exception as e:
                    errors.append(f"provider {described[i][0]!r}: {type(e).__name__}: {e}")
                    logger.debug("model-aliases: provider %s failed", described[i][0], exc_info=True)
        return results
    for i, (name, desc, alias_map) in enumerate(described):
        try:
            results[i] = resolve_target(cfg, name, desc, cache_dir, alias_map)
        except TargetError as e:
            errors.append(str(e))
        except Exception as e:
            errors.append(f"provider {name!r}: {type(e).__name__}: {e}")
            logger.debug("model-aliases: provider %s failed", name, exc_info=True)
    return results


def register_all() -> None:
    """Entry point run at plugin import (Hermes loader contract)."""
    raw, full = _load_raw_section()
    try:
        cfg = parse(raw)
    except ConfigError as e:
        # Raise (not just log): Hermes surfaces an import failure as a startup
        # warning and in `hermes plugins`, so an invalid section is VISIBLE.
        # Operationally identical to log-and-return (zero registration either
        # way) — only the visibility changes.
        logger.error("model-aliases: invalid config: %s", e)
        raise ConfigError(f"invalid config: {e}") from e
    if not cfg.groups:
        return
    try:
        from providers import register_provider
    except Exception as e:
        logger.error("model-aliases: provider registry unavailable: %s", e)
        return

    # Naming guard: a group name that Hermes' normalization would never route
    # to a profile is a silent-runtime-failure waiting to happen — fail at load.
    for gkey in cfg.groups:
        if gkey.lower() in {"custom", "auto", "none"}:
            reason = "a reserved bucket name"
        else:
            reason = early_return_reason(gkey)
            if reason is None:
                # a group key that IS a bundled provider name would shadow the
                # real profile (last-writer-wins registration under one name).
                # Guard only genuine name matches: the generic 'custom' fallback
                # (name != group key) is not a collision, and profiles carrying
                # our wrap marker (_alias_ids) are our own earlier registrations.
                from providers import get_provider_profile

                prof = get_provider_profile(gkey)
                if (
                    prof is not None
                    and prof.name == gkey
                    and not hasattr(prof, "_alias_ids")
                ):
                    reason = f"the bundled provider name {gkey!r} (it would shadow the real profile)"
        if reason is not None:
            msg = (
                f"provider alias {gkey!r}: this name falls into {reason}, whose normalization "
                "never consults provider profiles. Rename it (e.g. a name that does not "
                "start with a family slug)."
            )
            logger.error("model-aliases: %s", msg)
            raise ConfigError(msg)

    cache_dir = _hermes_home() / "cache" / "model-aliases"
    errors: list[str] = []

    # --- targets: groups first (they own their names), then referenced refs ---
    targets: dict[str, dict[str, Any]] = {}
    for group in cfg.groups.values():
        try:
            tname, entry, prof, desc = resolve_group_provider(group, full)
        except TargetError as e:
            errors.append(str(e))
            continue
        except Exception as e:
            errors.append(f"provider alias {group.key!r}: {type(e).__name__}: {e}")
            logger.debug("model-aliases: group %s failed", group.key, exc_info=True)
            continue
        t = targets.setdefault(
            tname, {"entry": entry, "prof": prof, "desc": desc, "groups": {}, "is_custom": entry is not None}
        )
        for prev_key, prev_aliases in t["groups"].items():
            clash = next((s for s in group.aliases if s in prev_aliases), None)
            if clash is not None:
                logger.warning(
                    "model-aliases: group %r overrides group %r alias %r on %s",
                    group.key, prev_key, clash, tname,
                )
        t["groups"][group.key] = group.aliases

    # --- resolve each target's scope (the groups targeting it; groups sharing
    # a target share one resolution) ---
    def _union_scope(t: dict[str, Any]) -> dict[str, AliasSpec]:
        scope: dict[str, AliasSpec] = {}
        for galiases in t["groups"].values():
            scope.update(galiases)
        return scope

    described = [(n, t["desc"], _union_scope(t)) for n, t in targets.items()]
    results = _resolve_all(cfg, described, cache_dir, errors)

    # --- wrap each target: one wrap per GROUP (primary name = the group key)
    # plus a target-name wrap (picker + real-name consumers) ---
    # resolve_provider canonicalizes provider aliases to the profile's NAME, so
    # the group wrap's primary name must BE the group key: canonicalizing to an
    # early-return family name (the target) would hand agent-init a name whose
    # normalization never consults the profile — the whole point of developer
    # naming is that the canonical name dodges those families.
    wrapped: list[Any] = []
    seen: set[str] = set()
    for i, (name, t) in enumerate(targets.items()):
        if i not in results:
            continue
        resolved, caps, rid_meta, real_ids = results[i]
        alias_map = _union_scope(t)
        refresh = make_refresh(cfg, name, t["desc"], cache_dir, alias_map)

        for gname in t["groups"].keys():
            if gname in seen:
                continue
            seen.add(gname)
            extra = (f"custom:{gname}",) if (t["is_custom"] and gname == name) else ()
            try:
                wrapped.append(
                    wrap_profile(
                        t["prof"],
                        provider_name=gname,
                        aliases=alias_map,
                        resolved=resolved,
                        caps=caps,
                        rid_meta=rid_meta,
                        real_ids=real_ids,
                        refresh=refresh,
                        extra_aliases=extra,
                        picker_alias_only=True,  # group rows list ONLY the alias table
                    )
                )
            except Exception as e:
                errors.append(f"provider alias {gname!r}: {type(e).__name__}: {e}")
                logger.debug("model-aliases: group wrap for %s failed", gname, exc_info=True)

        if name not in seen:
            seen.add(name)
            extra = (f"custom:{name}",) if t["is_custom"] else ()
            try:
                wrapped.append(
                    wrap_profile(
                        t["prof"],
                        provider_name=name,
                        aliases=alias_map,
                        resolved=resolved,
                        caps=caps,
                        rid_meta=rid_meta,
                        real_ids=real_ids,
                        refresh=refresh,
                        extra_aliases=extra,
                    )
                )
            except Exception as e:
                errors.append(f"provider {name!r}: {type(e).__name__}: {e}")
                logger.debug("model-aliases: wrap for %s failed", name, exc_info=True)

    # --- custom entries whose model/default_model is a known alias: validate ---
    for idx, (name, t) in enumerate(targets.items()):
        entry = t["entry"]
        if not entry:
            continue
        slug = str(entry.get("model") or entry.get("default_model") or "").strip().lower()
        target_known: set[str] = set()
        for galiases in t["groups"].values():
            target_known.update(s.lower() for s in galiases)
        if not slug or slug not in target_known:
            continue  # concrete id (or empty / another target's alias) — nothing to validate
        result = results.get(idx)
        if result is None:
            continue  # resolve already reported this target's failure
        if slug in {s.lower() for s in result[0]}:
            continue  # resolved on this provider — normal
        msg = (
            f"custom provider {name!r}: model {slug!r} is a known alias but matched no "
            "models on this provider (check match globs against the catalog)"
        )
        if cfg.strict:
            errors.append(msg)
        else:
            logger.error("model-aliases: %s", msg)

    if errors:
        joined = "model-aliases: " + "; ".join(errors)
        if cfg.strict:
            logger.error(joined)
            raise RuntimeError(joined)
        for e in errors:
            logger.error("model-aliases: %s", e)
    for prof in wrapped:
        register_provider(prof)
    if cfg.debug:
        logger.info("model-aliases: registered %s", [p.name for p in wrapped])

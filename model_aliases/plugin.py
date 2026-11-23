"""Registration orchestration: config -> target descriptors -> resolve -> register.

Called by the plugin root ``__init__.py`` at import (Hermes' loader contract:
``register_provider()`` at import). Tolerant by default: a bad target or an
unresolvable alias logs loudly and skips/degrades; ``model_aliases.strict``
turns any of it into a load failure (nothing registered, Hermes logs the
import error).

Startup latency budget (Hermes' plugin load timeout is 10s): a warm cache
(fresh + fully resolved) short-circuits with zero network; otherwise one
shared models.dev fetch (serialized behind its memo lock) plus live ``/models``
fetches run concurrently across targets.
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
from .config import AliasesConfig, ConfigError, TargetSpec, parse
from .resolver import resolve_aliases

logger = logging.getLogger("model-aliases")

_MODELS_DEV_MEMO: dict[str, Any] = {"ok": False, "catalog": None, "at": 0.0}
_MODELS_DEV_LOCK = threading.Lock()
# Failures are remembered briefly (so a cold loop doesn't refetch per target)
# but never forever (a long-running process must recover when the net returns).
_MEMO_FAILURE_RETRY_SECONDS = 60.0


class TargetError(RuntimeError):
    """A target cannot produce a usable profile."""


def _memo_models_dev() -> dict | None:
    """One models.dev fetch per process; failures retry after 60s. Never raises."""
    with _MODELS_DEV_LOCK:
        age = time.monotonic() - _MODELS_DEV_MEMO["at"]
        if _MODELS_DEV_MEMO["ok"]:
            return _MODELS_DEV_MEMO["catalog"]
        if age < _MEMO_FAILURE_RETRY_SECONDS and _MODELS_DEV_MEMO["at"] > 0.0:
            return None
        _MODELS_DEV_MEMO["at"] = time.monotonic()  # attempt marker (prevents re-entry after failure)
        catalog = fetch_models_dev()
        if isinstance(catalog, dict):
            _MODELS_DEV_MEMO.update(ok=True, catalog=catalog)
            return catalog
        return None


def reset_models_dev_memo(catalog: dict | None = None) -> None:
    """Test seam: prime (catalog given) or clear the per-process memo."""
    with _MODELS_DEV_LOCK:
        _MODELS_DEV_MEMO.update(
            ok=catalog is not None,
            catalog=catalog,
            at=time.monotonic(),
        )


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


def _profile_name(target_key: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", target_key.strip().lower()).strip("-")
    return f"alias-{slug or 'target'}"


def describe_target(provider_str: str, full_config: dict) -> tuple[ProviderDescriptor, dict[str, Any]]:
    """Resolve a configured target provider string to a wire descriptor.

    Order: named custom block in ``providers:`` / ``custom_providers:`` first
    (handles ``custom:<name>`` and bare names), then the bundled provider
    registry. Raises TargetError when the target cannot be served."""
    ps = (provider_str or "").strip()
    if not ps:
        raise TargetError("empty target provider")
    extras: dict[str, Any] = {
        "api_mode": "",
        "supports_vision": False,
        "inline_api_key": None,
        "warn": None,
    }
    blocks = full_config.get("providers") or full_config.get("custom_providers") or {}
    bare = ps[7:] if ps.lower().startswith("custom:") else ps
    block = blocks.get(bare) if isinstance(blocks, dict) else None
    if isinstance(block, dict):
        if block.get("enabled") is False:
            raise TargetError(f"named custom provider {bare!r} is disabled (enabled: false)")
        base = str(block.get("api") or block.get("base_url") or block.get("url") or "").strip()
        if not base:
            raise TargetError(f"providers.{bare} has no api/base_url")
        key_env = str(block.get("key_env") or block.get("api_key_env") or "").strip()
        inline = block.get("api_key")
        extras["inline_api_key"] = inline.strip() if isinstance(inline, str) and inline.strip() else None
        extras["api_mode"] = str(block.get("transport") or block.get("api_mode") or "")
        if block.get("key_cmd"):
            extras["warn"] = (
                f"providers.{bare} uses key_cmd, which an alias profile cannot resolve; "
                "set key_env or api_key"
            )
        desc = ProviderDescriptor(
            name=ps,
            base_url=base,
            models_dev_id=str(block.get("catalog_provider") or "").strip(),
            api_key_env=key_env,
        )
        return desc, extras

    from providers import get_provider_profile

    p = get_provider_profile(ps)
    if p is None:
        raise TargetError(f"unknown target provider {ps!r} (not bundled, not under providers:)")
    if p.name.startswith("alias-"):
        raise TargetError(f"target {ps!r} resolves to a model-aliases profile (self-reference)")
    extras["api_mode"] = p.api_mode
    extras["supports_vision"] = bool(p.supports_vision)
    if p.auth_type != "api_key":
        extras["warn"] = (
            f"target {ps!r} uses auth_type={p.auth_type!r}; an alias profile only carries "
            "api_key-style credentials, so inference may fail without an env key"
        )
    key_env = next((v for v in (p.env_vars or ()) if not str(v).endswith("_URL")), "")
    desc = ProviderDescriptor(
        name=ps,
        base_url=p.base_url,
        models_url=p.models_url,
        models_dev_id=p.models_dev_id or "",
        api_key_env=key_env,
    )
    return desc, extras


def _md_key(desc: ProviderDescriptor, catalog: dict | None) -> str:
    if not isinstance(catalog, dict):
        return ""
    for key in (desc.models_dev_id, desc.name):
        if key and isinstance(catalog.get(key), dict):
            return key
    return ""


def _capability_meta(catalog: dict | None, key: str, resolved: dict[str, str]) -> dict[str, dict]:
    """alias -> models.dev entry (models.dev shape), keyed for model_capabilities."""
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
    """resolved-id -> entry, derived from alias-keyed capabilities."""
    out: dict[str, dict] = {}
    for alias, entry in caps.items():
        rid = resolved.get(alias)
        if rid and isinstance(entry, dict):
            out[rid] = dict(entry)
    return out


def resolve_target(
    cfg: AliasesConfig,
    spec: TargetSpec,
    desc: ProviderDescriptor,
    cache_dir: Path,
    profile_name: str,
    *,
    now: float | None = None,
    catalog: dict | None = None,
    force_live: bool = False,
    live_timeout: float = 3.0,
) -> tuple[dict[str, str], dict, dict]:
    """Resolve every alias for one target.

    Returns (resolved alias->id restricted to configured aliases,
    alias->models.dev entry, resolved-id->models.dev entry).

    Warm short-circuit: when the cache is fresh (refresh_seconds) and already
    resolves every configured alias, returns straight from disk with zero
    network. Otherwise: models.dev candidates first (shared memo), live
    /models only when stale/unresolved/forced. State merge keeps
    last-known-good on failure and records drift; strict raises TargetError
    on any alias left without a resolution."""
    now = time.time() if now is None else now
    prev = state.load(cache_dir, profile_name)
    stale = prev is None or (now - prev.updated_at) >= cfg.refresh_seconds
    complete = prev is not None and all(a in prev.resolved for a in spec.aliases)

    if not force_live and complete and not stale:
        resolved = {a: prev.resolved[a] for a in spec.aliases}
        caps = {a: dict(m) for a, m in prev.capabilities.items() if a in resolved}
        if cfg.debug:
            logger.info("model-aliases: %s warm cache %s", spec.key, resolved)
        return resolved, caps, _rid_meta(resolved, caps)

    md = catalog if catalog is not None else _memo_models_dev()
    key = _md_key(desc, md)
    md_cands = models_dev_candidates(md, desc) if md else []
    new_map = resolve_aliases(spec.aliases, md_cands, desc.name)
    unresolved = [a for a in spec.aliases if not new_map.get(a)]
    live_rows = None
    if force_live or stale or unresolved:
        live_rows = fetch_live_ids(desc, timeout=live_timeout)
        if live_rows is not None:
            cands = merge_candidates(md_cands, live_rows)
            new_map = resolve_aliases(spec.aliases, cands, desc.name)
    merged, drift = state.merge(prev, new_map, now=now)
    for d in drift:
        when = _dt.datetime.fromtimestamp(d["at"]).isoformat(timespec="seconds")
        logger.warning(
            "model-aliases: %s/%s: %s -> %s (detected %s)",
            profile_name, d["alias"], d["from"], d["to"], when,
        )
    resolved = {a: merged.resolved[a] for a in spec.aliases if a in merged.resolved}
    caps = _capability_meta(md, key, resolved)
    if caps and caps != merged.capabilities:
        merged.capabilities = caps
    changed = merged.resolved != (prev.resolved if prev else {})
    caps_changed = bool(caps) and caps != (prev.capabilities if prev else {})
    if prev is None or drift or changed or caps_changed or (live_rows is not None and stale):
        state.save(cache_dir, profile_name, merged)
    unresolved = [a for a in spec.aliases if a not in resolved]
    if unresolved:
        msg = (
            f"target {spec.key!r}: alias(es) {', '.join(repr(u) for u in unresolved)} resolved "
            "to no model (check match globs against the catalog)"
        )
        if cfg.strict:
            raise TargetError(msg)
        logger.error("model-aliases: %s", msg)
    if cfg.debug:
        logger.info("model-aliases: %s resolved %s", spec.key, resolved)
    return resolved, caps, _rid_meta(resolved, caps)


def make_refresh(
    cfg: AliasesConfig,
    spec: TargetSpec,
    desc: ProviderDescriptor,
    cache_dir: Path,
    profile_name: str,
    *,
    timeout: float = 8.0,
) -> Callable[[float | None], tuple[dict[str, str], dict, dict] | None]:
    """Closure for profile.fetch_models: force a live re-resolution and persist.
    The profile passes its own fetch timeout through. Never raises."""
    def refresh(live_timeout: float | None = None) -> tuple[dict[str, str], dict, dict] | None:
        try:
            budget = float(live_timeout) if live_timeout else timeout
            return resolve_target(
                cfg, spec, desc, cache_dir, profile_name,
                force_live=True, live_timeout=budget,
            )
        except Exception:
            logger.debug("model-aliases: %s refresh failed", profile_name, exc_info=True)
            return None
    return refresh


def _resolve_all(
    cfg: AliasesConfig,
    prepped: list[tuple[TargetSpec, ProviderDescriptor, dict[str, Any], str]],
    cache_dir: Path,
    errors: list[str],
) -> dict[int, tuple[dict[str, str], dict, dict]]:
    """Resolve all prepared targets; live fetches overlap across targets
    (the shared models.dev fetch serializes behind its memo lock)."""
    results: dict[int, tuple[dict[str, str], dict, dict]] = {}
    if len(prepped) > 1:
        with ThreadPoolExecutor(max_workers=min(4, len(prepped))) as pool:
            futures = [
                pool.submit(resolve_target, cfg, spec, desc, cache_dir, pname)
                for spec, desc, _extras, pname in prepped
            ]
            for i, fut in enumerate(futures):
                try:
                    results[i] = fut.result()
                except TargetError as e:
                    errors.append(str(e))
                except Exception as e:
                    errors.append(f"target {prepped[i][0].key!r}: {type(e).__name__}: {e}")
                    logger.debug("model-aliases: target %s failed", prepped[i][0].key, exc_info=True)
        return results
    for i, (spec, desc, _extras, pname) in enumerate(prepped):
        try:
            results[i] = resolve_target(cfg, spec, desc, cache_dir, pname)
        except TargetError as e:
            errors.append(str(e))
        except Exception as e:
            errors.append(f"target {spec.key!r}: {type(e).__name__}: {e}")
            logger.debug("model-aliases: target %s failed", spec.key, exc_info=True)
    return results


def register_all() -> None:
    """Entry point run at plugin import (Hermes loader contract)."""
    raw, full = _load_raw_section()
    try:
        cfg = parse(raw)
    except ConfigError as e:
        logger.error("model-aliases: invalid config: %s", e)
        return
    if not cfg.targets:
        return
    try:
        from providers import register_provider
    except Exception as e:
        logger.error("model-aliases: provider registry unavailable: %s", e)
        return

    from .profile import build_profile

    cache_dir = _hermes_home() / "cache" / "model-aliases"
    errors: list[str] = []
    prepped: list[tuple[TargetSpec, ProviderDescriptor, dict[str, Any], str]] = []
    for key, spec in cfg.targets.items():
        try:
            desc, extras = describe_target(spec.provider, full)
            prepped.append((spec, desc, extras, _profile_name(key)))
        except TargetError as e:
            errors.append(str(e))
        except Exception as e:  # one broken target must not take down the rest
            errors.append(f"target {key!r}: {type(e).__name__}: {e}")
            logger.debug("model-aliases: target %s failed", key, exc_info=True)

    results = _resolve_all(cfg, prepped, cache_dir, errors)

    built: list[Any] = []
    for i, (spec, desc, extras, pname) in enumerate(prepped):
        if i not in results:
            continue
        resolved, caps, rid_meta = results[i]
        try:
            built.append(
                build_profile(
                    profile_name=pname,
                    spec=spec,
                    desc=desc,
                    extras=extras,
                    resolved=resolved,
                    capability_meta=caps,
                    rid_meta=rid_meta,
                    refresh=make_refresh(cfg, spec, desc, cache_dir, pname),
                )
            )
        except Exception as e:
            errors.append(f"target {spec.key!r}: {type(e).__name__}: {e}")
            logger.debug("model-aliases: build for %s failed", spec.key, exc_info=True)

    if errors:
        joined = "model-aliases: " + "; ".join(errors)
        if cfg.strict:
            logger.error(joined)
            raise RuntimeError(joined)
        for e in errors:
            logger.error("model-aliases: %s", e)
    for profile in built:
        register_provider(profile)
    if cfg.debug:
        logger.info("model-aliases: registered %s", [p.name for p in built])

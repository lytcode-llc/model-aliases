"""Registration orchestration: config -> referenced providers -> resolve -> wrap -> register.

Called by the plugin root ``__init__.py`` at import (Hermes' loader contract:
``register_provider()`` at import). The flat alias table is resolved against
each REFERENCED provider (model.provider, auxiliary.*.provider, fallback,
delegation, smart routing, custom_providers entries — plus every registered
provider when wrap_all is set), then each provider is wrapped: a profile under
the real provider name carrying the alias table.

Tolerant by default: a provider that cannot be served or an alias with no
match on it logs loudly; ``model_aliases.strict`` turns any of it into a load
failure (nothing registered, Hermes logs the import error). Startup budget:
eager resolution runs concurrently across providers behind one shared
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
from .config import AliasesConfig, ConfigError, parse
from .profile import build_custom_profile, wrap_profile
from .resolver import resolve_aliases

logger = logging.getLogger("model-aliases")

_MODELS_DEV_MEMO: dict[str, Any] = {"ok": False, "catalog": None, "at": 0.0}
_MODELS_DEV_LOCK = threading.Lock()
_MEMO_FAILURE_RETRY_SECONDS = 60.0


class TargetError(RuntimeError):
    """A provider cannot be served (unknown, or an alias resolved to nothing)."""


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


# --- referenced-provider discovery -----------------------------------------

def _add_referenced(store: dict[str, Any], provider: Any, entry: dict | None) -> None:
    name = str(provider or "").strip()
    if not name or name.lower() in {"auto", "none", "custom"}:
        return  # 'custom' is the generic bucket — wrapping it logs noise forever
    if name not in store:
        store[name] = entry  # first mention wins; entry None = bundled


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


def referenced_providers(full: dict, cfg: AliasesConfig) -> dict[str, dict | None]:
    """Every provider name referenced anywhere in config (-> custom entry or None).
    When wrap_all is set, every registered provider joins the map."""
    store: dict[str, Any] = {}

    model_cfg = full.get("model") if isinstance(full.get("model"), dict) else {}
    _add_referenced(store, model_cfg.get("provider"), _custom_entry_for(str(model_cfg.get("provider") or ""), full))

    aux = full.get("auxiliary") if isinstance(full.get("auxiliary"), dict) else {}
    for task, tcfg in aux.items():
        if isinstance(tcfg, dict):
            _add_referenced(store, tcfg.get("provider"), _custom_entry_for(str(tcfg.get("provider") or ""), full))

    for fb in (full.get("fallback_providers") or []):
        if isinstance(fb, dict):
            _add_referenced(store, fb.get("provider"), _custom_entry_for(str(fb.get("provider") or ""), full))

    deleg = full.get("delegation") if isinstance(full.get("delegation"), dict) else {}
    _add_referenced(store, deleg.get("provider"), _custom_entry_for(str(deleg.get("provider") or ""), full))

    smr = full.get("smart_model_routing") if isinstance(full.get("smart_model_routing"), dict) else {}
    cheap = smr.get("cheap_model") if isinstance(smr.get("cheap_model"), dict) else {}
    _add_referenced(store, cheap.get("provider"), _custom_entry_for(str(cheap.get("provider") or ""), full))

    # custom_providers entries are wrapped even if no block references them yet
    for block in (full.get("custom_providers"), full.get("providers")):
        if isinstance(block, list):
            for item in block:
                if isinstance(item, dict) and item.get("name"):
                    _add_referenced(store, str(item["name"]).strip(), item)
        elif isinstance(block, dict):
            for key, item in block.items():
                if _is_provider_block(item):
                    _add_referenced(store, key, item)

    if cfg.wrap_all:
        try:
            from providers import list_providers

            for prof in list_providers():
                _add_referenced(store, prof.name, None)
        except Exception:
            logger.debug("model-aliases: list_providers failed for wrap_all", exc_info=True)
    return store


# --- per-provider resolution -------------------------------------------------

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


def describe_provider(name: str, entry: dict | None, full: dict) -> tuple[Any, ProviderDescriptor]:
    """Resolve a referenced provider to (profile-to-wrap, wire descriptor).

    Bundled: the registered profile itself (deepcopied by wrap_profile).
    Custom entry: a freshly built profile from the entry fields."""
    from providers import get_provider_profile

    if entry is None:
        prof = get_provider_profile(name)
        if prof is None:
            raise TargetError(f"unknown provider {name!r} (not bundled, not under custom_providers/providers:)")
        desc = ProviderDescriptor(
            name=name,
            base_url=prof.base_url,
            models_url=prof.models_url,
            models_dev_id=prof.models_dev_id or "",
            api_key_env=next((v for v in (prof.env_vars or ()) if not str(v).endswith("_URL")), ""),
        )
        return prof, desc
    prof = build_custom_profile(name, entry)
    if not prof.base_url:
        raise TargetError(f"custom provider {name!r} has no api/base_url")
    key_env = str(entry.get("key_env") or entry.get("api_key_env") or "").strip()
    desc = ProviderDescriptor(
        name=name,
        base_url=prof.base_url,
        models_dev_id=prof.models_dev_id,
        api_key_env=key_env,
    )
    return prof, desc


def resolve_provider(
    cfg: AliasesConfig,
    name: str,
    desc: ProviderDescriptor,
    cache_dir: Path,
    *,
    now: float | None = None,
    catalog: dict | None = None,
    force_live: bool = False,
    live_timeout: float = 3.0,
) -> tuple[dict[str, str], dict, dict, list[str]]:
    """Resolve the global alias table against ONE provider's candidates.

    Returns (resolved, caps, rid_meta, real_ids). Warm cache (fresh +
    complete) short-circuits with zero network; otherwise models.dev (shared
    memo) + live /models. State keeps last-known-good on fetch failure and
    records drift; strict raises on any alias left unresolved."""
    now = time.time() if now is None else now
    prev = state.load(cache_dir, _cache_key(name))
    stale = prev is None or (now - prev.updated_at) >= cfg.refresh_seconds
    complete = prev is not None and all(a in prev.resolved for a in cfg.aliases)

    if not force_live and complete and not stale:
        resolved = {a: prev.resolved[a] for a in cfg.aliases}
        caps = {a: dict(m) for a, m in prev.capabilities.items() if a in resolved}
        return resolved, caps, _rid_meta(resolved, caps), list(prev.real_ids)

    md = catalog if catalog is not None else _memo_models_dev()
    key = _md_key(desc, md)
    md_cands = models_dev_candidates(md, desc) if md else []
    new_map = resolve_aliases(cfg.aliases, md_cands, provider=name)
    unresolved = [a for a in cfg.aliases if not new_map.get(a)]
    live_rows = None
    if force_live or stale or unresolved:
        live_rows = fetch_live_ids(desc, timeout=live_timeout)
        if live_rows is not None:
            cands = merge_candidates(md_cands, live_rows)
            new_map = resolve_aliases(cfg.aliases, cands, provider=name)
    merged, drift = state.merge(prev, new_map, now=now)
    for d in drift:
        when = _dt.datetime.fromtimestamp(d["at"]).isoformat(timespec="seconds")
        logger.warning(
            "model-aliases: %s/%s: %s -> %s (detected %s)",
            name, d["alias"], d["from"], d["to"], when,
        )
    resolved = {a: merged.resolved[a] for a in cfg.aliases if a in merged.resolved}
    caps = _capability_meta(md, key, resolved)
    if caps and caps != merged.capabilities:
        merged.capabilities = caps
    real_ids = [c.id for c in (merge_candidates(md_cands, live_rows) if live_rows is not None else md_cands)]
    if real_ids:
        merged.real_ids = real_ids
    changed = merged.resolved != (prev.resolved if prev else {}) or merged.real_ids != (prev.real_ids if prev else [])
    caps_changed = bool(caps) and caps != (prev.capabilities if prev else {})
    if prev is None or drift or changed or caps_changed or (live_rows is not None and stale):
        state.save(cache_dir, _cache_key(name), merged)
    unresolved = [a for a in cfg.aliases if a not in resolved]
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
    # A failed fresh pass must not drop what state.merge preserved: caps and
    # real_ids fall back to the last-known-good values (the wrap then keeps
    # the last good picker/metadata until a refresh succeeds).
    final_caps = caps or merged.capabilities
    final_real = real_ids or list(merged.real_ids)
    return resolved, final_caps, _rid_meta(resolved, final_caps), final_real


def make_refresh(
    cfg: AliasesConfig,
    name: str,
    desc: ProviderDescriptor,
    cache_dir: Path,
    *,
    timeout: float = 8.0,
) -> Callable[[float | None], tuple[dict, dict, dict, list] | None]:
    """Closure for wrapped fetch_models: force a live re-resolution and persist.
    Returns (resolved, caps, rid_meta, real_ids) or None on failure. Never raises."""
    def refresh(live_timeout: float | None = None) -> tuple[dict, dict, dict, list] | None:
        try:
            budget = float(live_timeout) if live_timeout else timeout
            return resolve_provider(
                cfg, name, desc, cache_dir,
                force_live=True, live_timeout=budget,
            )
        except Exception:
            logger.debug("model-aliases: %s refresh failed", name, exc_info=True)
            return None
    return refresh


def _resolve_all(
    cfg: AliasesConfig,
    described: list[tuple[str, ProviderDescriptor]],
    cache_dir: Path,
    errors: list[str],
) -> dict[int, tuple[dict, dict, dict, list]]:
    results: dict[int, tuple[dict, dict, dict, list]] = {}
    if len(described) > 1:
        with ThreadPoolExecutor(max_workers=min(6, len(described))) as pool:
            futures = [
                pool.submit(resolve_provider, cfg, name, desc, cache_dir)
                for name, desc in described
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
    for i, (name, desc) in enumerate(described):
        try:
            results[i] = resolve_provider(cfg, name, desc, cache_dir)
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
        logger.error("model-aliases: invalid config: %s", e)
        return
    if not cfg.aliases:
        return
    try:
        from providers import register_provider
    except Exception as e:
        logger.error("model-aliases: provider registry unavailable: %s", e)
        return

    cache_dir = _hermes_home() / "cache" / "model-aliases"
    errors: list[str] = []
    described: list[tuple[str, ProviderDescriptor, Any, bool]] = []
    referenced = referenced_providers(full, cfg)
    # alias-<x> references resolve back to <x> for wrapping: the neutral name
    # is an alias OF the real wrap, so describing it would be a chicken-and-egg
    # miss (the alias exists only once the wrap registers).
    resolved_refs: dict[str, Any] = {}
    for name, entry in referenced.items():
        real, resolved_entry = name, entry
        if entry is None and name.startswith("alias-"):
            from providers import get_provider_profile

            base = name[len("alias-"):]
            if get_provider_profile(base) is not None:
                real = base
            else:
                custom = _custom_entry_for(base, full)
                if custom is not None:
                    real, resolved_entry = base, custom
        # a concrete entry beats a bundled None on collision (first-mention
        # order must not shadow a custom entry behind an alias- reference)
        if real not in resolved_refs or (resolved_refs[real] is None and resolved_entry is not None):
            resolved_refs[real] = resolved_entry
    for name, entry in resolved_refs.items():
        try:
            prof, desc = describe_provider(name, entry, full)
            described.append((name, desc, prof, entry is not None))
        except TargetError as e:
            errors.append(str(e))
        except Exception as e:
            errors.append(f"provider {name!r}: {type(e).__name__}: {e}")
            logger.debug("model-aliases: provider %s failed", name, exc_info=True)

    results = _resolve_all(cfg, [(n, d) for n, d, _p, _c in described], cache_dir, errors)

    wrapped: list[Any] = []
    for i, (name, desc, prof, is_custom) in enumerate(described):
        if i not in results:
            continue
        resolved, caps, rid_meta, real_ids = results[i]
        try:
            wrapped.append(
                wrap_profile(
                    prof,
                    provider_name=name,
                    aliases=cfg.aliases,
                    resolved=resolved,
                    caps=caps,
                    rid_meta=rid_meta,
                    real_ids=real_ids,
                    refresh=make_refresh(cfg, name, desc, cache_dir),
                    # Every wrap also answers to alias-<provider>: Hermes'
                    # normalize_model_for_provider early-returns for aggregator,
                    # OpenCode-family and dot-hyphen providers BEFORE consulting
                    # any profile, so request paths on those families need a
                    # neutral name that dodges those lists. Custom entries also
                    # claim custom:<name> and the bare name (the generic 'custom'
                    # profile's bare-name aliases would hijack lookup).
                    extra_aliases=(
                        *((f"custom:{name}", name) if is_custom else ()),
                        f"alias-{name}",
                    ),
                )
            )
        except Exception as e:
            errors.append(f"provider {name!r}: {type(e).__name__}: {e}")
            logger.debug("model-aliases: wrap for %s failed", name, exc_info=True)

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

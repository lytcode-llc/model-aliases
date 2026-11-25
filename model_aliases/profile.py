"""Wrap provider profiles so they resolve the global alias table.

Wrapping is the whole mechanism: a profile registered UNDER the real provider
name (field-identical to the target, so credential pools, auth_type, env
ladders and registry sync all key identically) with three instance-level
overrides — normalize_model_id (alias resolution, pure dict lookups only),
fetch_models (real ids + aliases for the picker), live_context_length
(resolved-model metadata). Real model ids pass through untouched.
"""

from __future__ import annotations

import copy
import logging
import types
from typing import Any, Callable

from .config import AliasSpec

logger = logging.getLogger("model-aliases")


def _normalize(
    prof: Any,
    model: str,
    *,
    api_key: str = "",
) -> str | None:
    """Alias -> resolved id for this provider; None = passthrough (real ids
    and unknown slugs go to Hermes' generic rules). Known alias that resolved
    to nothing on this provider logs one warning per (provider, alias)."""
    if not isinstance(model, str):
        return None
    key = model.strip().lower()
    resolved = prof._resolved_lower
    if key in resolved:
        return resolved[key]
    if key in prof._alias_names_lower:
        warned = prof._warned_unmatched
        if (prof.name, key) not in warned:
            warned.add((prof.name, key))
            logger.warning(
                "model-aliases: alias %r matched no models on %r",
                model, prof.name,
            )
    return None


def _fetch_models(
    prof: Any,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: float = 8.0,
) -> list[str] | None:
    """Real model ids + alias ids for the /model picker. Refreshes resolution
    (network allowed here) and swaps state atomically; a failed refresh keeps
    the current snapshot."""
    if prof._refresh is not None:
        try:
            out = prof._refresh(timeout)
        except Exception:  # refresh must never break the picker
            logger.debug("model-aliases: refresh failed for %s", prof.name, exc_info=True)
            out = None
        if isinstance(out, tuple) and len(out) == 4:
            fresh_resolved, fresh_caps, fresh_rid, fresh_real = out
            new_resolved = {k: v for k, v in fresh_resolved.items() if isinstance(k, str) and isinstance(v, str)}
            if new_resolved:
                prof._resolved = new_resolved
                prof._resolved_lower = {k.lower(): v for k, v in new_resolved.items()}
                if fresh_caps:
                    prof._caps = dict(fresh_caps)
                    prof.model_capabilities = {a: m for a, m in fresh_caps.items() if isinstance(m, dict)}
                prof._rid_meta = {k: dict(v) for k, v in fresh_rid.items() if isinstance(v, dict)}
            if fresh_real:
                prof._real_ids = [str(x) for x in fresh_real]
    return list(dict.fromkeys(list(prof._real_ids) + list(prof._alias_ids)))


def _live_context_length(
    prof: Any,
    model: str,
    *,
    api_key: str = "",
    base_url: str = "",
) -> int | None:
    """Context window of the resolved target model; delegates to the wrapped
    profile's own lookup for real ids."""
    if isinstance(model, str) and model.strip().lower() in prof._resolved_lower:
        rid = prof._resolved_lower[model.strip().lower()]
        entry = prof._rid_meta.get(rid)
        if isinstance(entry, dict):
            ctx = (entry.get("limit") or {}).get("context") if isinstance(entry.get("limit"), dict) else None
            return int(ctx) if isinstance(ctx, int) else None
    original = getattr(type(prof), "__dict__", {}).get("live_context_length")
    if original is not None:
        return original(prof, model, api_key=api_key, base_url=base_url)
    return None


def wrap_profile(
    target: Any,
    *,
    provider_name: str,
    aliases: dict[str, AliasSpec],
    resolved: dict[str, str],
    caps: dict[str, dict],
    rid_meta: dict[str, dict],
    real_ids: list[str],
    refresh: Callable[[float | None], tuple[dict, dict, dict, list] | None],
    extra_aliases: tuple[str, ...] = (),
) -> Any:
    """Wrap a target profile (bundled or custom-built) under ``provider_name``.

    The target is deepcopied (67 init fields, zero init=False — verified in
    spike), then normalize/fetch/live are shadowed as instance attributes;
    the class methods stay intact for anything that reads them unbound."""
    prof = copy.deepcopy(target)
    prof.name = provider_name
    if extra_aliases:
        prof.aliases = tuple(dict.fromkeys([*(target.aliases or ()), *extra_aliases]))
    prof._alias_ids = tuple(aliases.keys())
    prof._alias_names_lower = {a.lower() for a in aliases}
    prof._resolved = dict(resolved)
    prof._resolved_lower = {k.lower(): v for k, v in resolved.items()}
    prof._caps = dict(caps)
    prof.model_capabilities = {a: dict(m) for a, m in caps.items() if isinstance(m, dict)}
    prof._rid_meta = {k: dict(v) for k, v in rid_meta.items() if isinstance(v, dict)}
    prof._real_ids = list(real_ids)
    prof._refresh = refresh
    prof._warned_unmatched: set[tuple[str, str]] = set()
    prof.normalize_model_id = types.MethodType(_normalize, prof)
    prof.fetch_models = types.MethodType(_fetch_models, prof)
    prof.live_context_length = types.MethodType(_live_context_length, prof)
    return prof


def build_custom_profile(entry_name: str, entry: dict) -> Any:
    """Build a ProviderProfile from a ``custom_providers:`` list entry."""
    from providers.base import ProviderProfile

    base = str(entry.get("api") or entry.get("base_url") or entry.get("url") or "").strip()
    key_env = str(entry.get("key_env") or entry.get("api_key_env") or "").strip()
    transport = str(entry.get("transport") or entry.get("api_mode") or "").strip()
    models_dev_id = str(entry.get("catalog_provider") or "").strip()
    return ProviderProfile(
        name=entry_name,
        base_url=base,
        env_vars=(key_env,) if key_env else (),
        api_mode=transport or "chat_completions",
        models_dev_id=models_dev_id,
        auth_type="api_key",
        supports_health_check=False,
        supports_model_listing=True,
    )

"""Build the Hermes ProviderProfile that serves floating aliases for one target.

A profile is registered per configured target (e.g. ``alias-oc`` wrapping
``opencode-go``). It copies the target's wire settings so auth, transport and
model metadata resolve exactly as the real provider's would, while its model
ids are the user's aliases: ``normalize_model_id`` maps alias -> newest
resolved concrete id (consulted by Hermes at agent init, /model, aux calls and
fallbacks), ``fetch_models`` lists the alias ids in the /model picker.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .catalog import ProviderDescriptor
from .config import TargetSpec

logger = logging.getLogger("model-aliases")


def build_profile(
    *,
    profile_name: str,
    spec: TargetSpec,
    desc: ProviderDescriptor,
    extras: dict[str, Any],
    resolved: dict[str, str],
    capability_meta: dict[str, dict],
    rid_meta: dict[str, dict],
    refresh: Callable[[], tuple[dict[str, str], dict[str, dict], dict[str, dict]] | None] | None,
):
    """Construct the ProviderProfile for one target. Requires the ``providers``
    package (Hermes runtime); raises ImportError when unavailable."""
    from providers.base import ProviderProfile

    alias_ids: tuple[str, ...] = tuple(spec.aliases.keys())
    resolved = dict(resolved)
    lower = {k.lower(): v for k, v in resolved.items()}
    inline_key = extras.get("inline_api_key")
    api_mode = str(extras.get("api_mode") or "") or "chat_completions"
    env_vars = (desc.api_key_env,) if desc.api_key_env else ()

    class AliasProviderProfileImpl(ProviderProfile):
        # Not dataclass fields: runtime state assigned right after __init__.
        _alias_ids: tuple[str, ...] = ()
        _alias_resolved: dict[str, str] = {}
        _alias_resolved_lower: dict[str, str] = {}
        _capability_meta: dict[str, dict] = {}
        _rid_meta: dict[str, dict] = {}
        _refresh: Any = None
        _inline_key: str | None = None

        def normalize_model_id(self, model: str, *, api_key: str = "") -> str | None:
            """Alias -> newest resolved concrete id; None for anything else so
            Hermes' generic normalization handles concrete ids untouched.
            Pure dict lookups only (hot-path rule: no I/O)."""
            if not isinstance(model, str):
                return None
            r = self._alias_resolved
            if model in r:
                return r[model]
            return self._alias_resolved_lower.get(model.strip().lower())

        def fetch_models(self, *, api_key: str | None = None, base_url: str | None = None,
                         timeout: float = 8.0) -> list[str] | None:
            """Alias ids for the /model picker. Also refreshes resolution
            (network allowed here, unlike normalize) and swaps state atomically."""
            if self._refresh is not None:
                try:
                    out = self._refresh(timeout)
                except Exception:  # refresh must never break the picker
                    logger.debug("model-aliases: refresh failed for %s", self.name, exc_info=True)
                    out = None
                if isinstance(out, tuple) and len(out) == 3:
                    fresh_resolved, fresh_caps, fresh_rid = out
                    new_resolved = {k: v for k, v in fresh_resolved.items() if isinstance(k, str) and isinstance(v, str)}
                    if new_resolved:  # never wipe a good snapshot with an empty result
                        self._alias_resolved = new_resolved
                        self._alias_resolved_lower = {k.lower(): v for k, v in new_resolved.items()}
                        if fresh_caps:
                            self._capability_meta = dict(fresh_caps)
                            self.model_capabilities = {a: m for a, m in fresh_caps.items() if isinstance(m, dict)}
                        self._rid_meta = {k: dict(v) for k, v in fresh_rid.items() if isinstance(v, dict)}
            return list(self._alias_ids)

        def live_context_length(self, model: str, *, api_key: str = "", base_url: str = "") -> int | None:
            """Context window of the resolved target model (models.dev metadata)."""
            rid = self.normalize_model_id(model) or (model if isinstance(model, str) else None)
            if not rid:
                return None
            entry = self._rid_meta.get(rid)
            if not isinstance(entry, dict):
                return None
            ctx = (entry.get("limit") or {}).get("context") if isinstance(entry.get("limit"), dict) else None
            return int(ctx) if isinstance(ctx, int) else None

        def resolve_api_key_secret(self) -> tuple[str, str] | None:
            """Inline ``providers.<name>.api_key`` support: the generic env/pool
            ladder cannot see config-held keys under our profile name."""
            if self._inline_key:
                return self._inline_key, "config:inline-api-key"
            return None

    profile = AliasProviderProfileImpl(
        name=profile_name,
        api_mode=api_mode,
        display_name=f"Aliases ({spec.provider})",
        description=(
            f"Floating model aliases for {spec.provider}: stable ids that resolve "
            "to the newest matching model at startup"
        ),
        env_vars=env_vars,
        base_url=desc.base_url,
        models_url=desc.models_url,
        auth_type="api_key",
        supports_model_listing=True,
        supports_vision=bool(extras.get("supports_vision")),
        fallback_models=alias_ids,
        static_models=alias_ids,
        models_dev_id=desc.models_dev_id,
        model_capabilities={a: dict(m) for a, m in capability_meta.items() if isinstance(m, dict)},
    )
    profile._alias_ids = alias_ids
    profile._alias_resolved = resolved
    profile._alias_resolved_lower = lower
    profile._capability_meta = {a: dict(m) for a, m in capability_meta.items()}
    profile._rid_meta = {k: dict(v) for k, v in rid_meta.items() if isinstance(v, dict)}
    profile._refresh = refresh
    profile._inline_key = inline_key
    if extras.get("warn"):
        logger.warning("model-aliases: %s: %s", profile_name, extras["warn"])
    return profile

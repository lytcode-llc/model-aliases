"""Alias matching, capability filtering, and latest-version selection (pure).

v2: the alias table is global — patterns match raw model ids (and each
"/"-suffix segment of them, so aggregator ids like ``google/gemini-3-flash``
also match ``gemini-*``). Resolution happens per-provider against that
provider's candidates.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from datetime import date, timedelta

from .config import AliasSpec

_DIGITS = re.compile(r"(\d+)")


@dataclass(frozen=True)
class Candidate:
    """One model that could win an alias: id plus metadata used by filters.

    ``None`` metadata means unknown; filters treat unknown conservatively
    (capability requirements fail, recency checks pass)."""

    id: str
    release_date: str | None = None
    context: int | None = None
    tool_call: bool | None = None
    input_modalities: tuple[str, ...] | None = None
    output_modalities: tuple[str, ...] | None = None


def natural_key(s: str) -> tuple:
    """Total-order sort key: digit runs compare numerically, others lexically.

    Tagged parts ((0, int) for digits, (1, str) otherwise) keep mixed-type
    comparison from ever raising: 'glm-4.7' > 'glm-4.6' > 'glm-4'."""
    key: list[tuple[int, object]] = []
    for part in _DIGITS.split(s or ""):
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part))
    return tuple(key)


def _forms(c: Candidate, provider: str = "") -> tuple[str, ...]:
    """Raw id, every "/"-suffix segment (aggregator ids), and the
    provider-prefixed form when a provider is in play (v1-style patterns
    like 'opencode-go/glm-*' keep working)."""
    forms = [c.id]
    if "/" in c.id:
        parts = c.id.split("/")
        for i in range(1, len(parts)):
            forms.append("/".join(parts[i:]))
    if provider:
        forms.append(f"{provider}/{c.id}")
    return tuple(forms)


def _hits(forms: tuple[str, ...], patterns: tuple[str, ...]) -> bool:
    return any(
        fnmatch.fnmatchcase(form, pattern)
        for pattern in patterns
        for form in forms
    )


def candidate_matches(c: Candidate, spec: AliasSpec, provider: str = "") -> bool:
    """Included by spec.match and not by spec.exclude (exclude always wins)."""
    if not spec.match:
        return False
    forms = _forms(c, provider)
    if spec.exclude and _hits(forms, spec.exclude):
        return False
    return _hits(forms, spec.match)


def _release(c: Candidate) -> date | None:
    if not isinstance(c.release_date, str) or not c.release_date:
        return None
    try:
        return date.fromisoformat(c.release_date)
    except ValueError:
        return None


def _has_all(have: tuple[str, ...], need: tuple[str, ...]) -> bool:
    have_lower = {str(m).lower() for m in have}
    return all(str(n).lower() in have_lower for n in need)


def _passes_filters(c: Candidate, spec: AliasSpec, now: date) -> bool:
    """Capability/age filters. Unknown metadata: capability requirements FAIL
    (cannot verify), minAge PASSES (no evidence of recency). Boundary is
    inclusive: release == cutoff passes."""
    if spec.min_context is not None and (c.context is None or c.context < spec.min_context):
        return False
    if spec.tools is not None and c.tool_call is not spec.tools:
        return False
    if spec.input_modalities is not None and (
        c.input_modalities is None or not _has_all(c.input_modalities, spec.input_modalities)
    ):
        return False
    if spec.output_modalities is not None and (
        c.output_modalities is None or not _has_all(c.output_modalities, spec.output_modalities)
    ):
        return False
    if spec.min_age_hours is not None:
        rel = _release(c)
        if rel is not None and rel > now - timedelta(hours=spec.min_age_hours):
            return False
    return True


def select_latest(candidates: list[Candidate], spec: AliasSpec, *, now: date | None = None) -> str | None:
    """Best survivor: filters applied, then newest release_date first (undated
    sorts last), natural-version DESC and id DESC as tiebreaks. Candidates are
    assumed pre-matched (see resolve_aliases)."""
    today = now or date.today()
    survivors = [c for c in candidates if _passes_filters(c, spec, today)]
    if not survivors:
        return None
    survivors.sort(
        key=lambda c: (c.release_date or "", natural_key(c.id), c.id),
        reverse=True,
    )
    return survivors[0].id


def resolve_aliases(
    aliases: dict[str, AliasSpec],
    candidates: list[Candidate],
    *,
    now: date | None = None,
    provider: str = "",
) -> dict[str, str | None]:
    """Resolve every alias against ONE provider's candidates; input order
    preserved; no match -> None."""
    out: dict[str, str | None] = {}
    for name, spec in aliases.items():
        pool = [c for c in candidates if candidate_matches(c, spec, provider)]
        out[name] = select_latest(pool, spec, now=now)
    return out


def make_candidate(entry: dict, *, model_id: str | None = None) -> Candidate:
    """Convert one models.dev-style model entry dict to a Candidate."""
    if not isinstance(entry, dict):
        raise ValueError(f"candidate entry must be a mapping, got {type(entry).__name__}")
    mid = model_id if model_id is not None else entry.get("id")
    if not isinstance(mid, str) or not mid:
        raise ValueError("candidate id must be a non-empty string")
    rel = entry.get("release_date")
    rel = rel if isinstance(rel, str) and rel else None

    ctx: int | None = None
    limit = entry.get("limit")
    if isinstance(limit, dict):
        raw_ctx = limit.get("context")
        if isinstance(raw_ctx, int) and not isinstance(raw_ctx, bool) and raw_ctx > 0:
            ctx = raw_ctx

    tc = entry.get("tool_call")
    tc = tc if isinstance(tc, bool) else None

    def _mods(key: str) -> tuple[str, ...] | None:
        mods = entry.get("modalities")
        if not isinstance(mods, dict):
            return None
        lst = mods.get(key)
        if not isinstance(lst, list):
            return None
        return tuple(str(m) for m in lst)

    return Candidate(
        id=mid,
        release_date=rel,
        context=ctx,
        tool_call=tc,
        input_modalities=_mods("input"),
        output_modalities=_mods("output"),
    )

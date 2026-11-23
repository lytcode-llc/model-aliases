"""Per-profile resolution cache with drift history.

Cache dir is passed in explicitly by the caller (typically
``~/.hermes/cache/model-aliases``). On-disk format is versioned JSON; a
version bump or any malformed file simply reads as "no cache".
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("model-aliases")

VERSION = 1
MAX_HISTORY = 20

_SAFE_NAME = re.compile(r"[^a-z0-9._-]")


@dataclass
class Resolution:
    """Last-known-good alias -> model id mapping, drift history, and the
    models.dev capability entries captured at resolution time (so warm
    starts need no network for picker badges/routing metadata)."""

    resolved: dict[str, str]
    updated_at: float
    history: list[dict] = field(default_factory=list)
    capabilities: dict[str, dict] = field(default_factory=dict)


def cache_path(cache_dir: Path, profile_name: str) -> Path:
    """Return ``cache_dir / '<sanitized-profile>.json'``."""
    safe = _SAFE_NAME.sub("_", profile_name)
    return Path(cache_dir) / f"{safe}.json"


def load(cache_dir: Path, profile_name: str) -> Resolution | None:
    """Read the cache file. Never raises; any problem -> None."""
    path = cache_path(cache_dir, profile_name)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("version") != VERSION:
        return None
    resolved = raw.get("resolved")
    updated_at = raw.get("updated_at")
    history = raw.get("history")
    capabilities = raw.get("capabilities", {})
    if not isinstance(resolved, dict):
        return None
    if not isinstance(updated_at, (int, float)) or isinstance(updated_at, bool):
        return None
    if not isinstance(history, list):
        return None
    if not isinstance(capabilities, dict):
        capabilities = {}
    caps = {str(k): dict(v) for k, v in capabilities.items() if isinstance(v, dict)}
    return Resolution(
        resolved={str(k): str(v) for k, v in resolved.items()},
        updated_at=float(updated_at),
        history=list(history),
        capabilities=caps,
    )


def save(cache_dir: Path, profile_name: str, res: Resolution) -> None:
    """Atomically persist ``res``. Never raises (logs a warning on OSError)."""
    path = cache_path(cache_dir, profile_name)
    tmp = path.with_name(path.name + ".tmp")
    payload = {
        "version": VERSION,
        "updated_at": res.updated_at,
        "resolved": res.resolved,
        "history": res.history,
    }
    if res.capabilities:
        payload["capabilities"] = res.capabilities
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:  # noqa: BLE001 - never raise from save
        logger.warning("model-aliases: failed to save cache %s: %s", path, exc)


def merge(
    prev: Resolution | None,
    new: dict[str, str | None],
    *,
    now: float,
    capabilities: dict[str, dict] | None = None,
) -> tuple[Resolution, list[dict]]:
    """Fold a fresh resolution attempt into the previous cache.

    New non-None values win (drift event when they differ from prev);
    None values keep the last-known-good entry. History is capped at
    ``MAX_HISTORY`` oldest-first drop. ``capabilities`` replaces the stored
    capability entries when given (fresh models.dev data); ``None`` keeps
    the previous entries.
    """
    merged: dict[str, str] = dict(prev.resolved) if prev is not None else {}
    events: list[dict] = []
    for alias, value in new.items():
        if value is None:
            continue  # keep last-known-good; absent prev -> key stays absent
        old = merged.get(alias)
        merged[alias] = value
        if old is not None and old != value:
            event = {"alias": alias, "from": old, "to": value, "at": now}
            events.append(event)
    history: list[dict] = list(prev.history) if prev is not None else []
    history.extend(events)
    if len(history) > MAX_HISTORY:
        history = history[len(history) - MAX_HISTORY :]
    if capabilities is None:
        caps = dict(prev.capabilities) if prev is not None else {}
    else:
        caps = {k: dict(v) for k, v in capabilities.items() if isinstance(v, dict)}
    res = Resolution(resolved=merged, updated_at=now, history=history, capabilities=caps)
    return res, events

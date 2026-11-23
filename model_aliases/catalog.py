"""Candidate sourcing: models.dev catalog + a provider's live /models endpoint.

HTTP goes through the ``_fetch_json`` seam so tests can monkeypatch it.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import dataclass

from .resolver import Candidate, make_candidate

DEFAULT_MODELS_DEV_URL = "https://models.dev/api.json"
MAX_RESPONSE_BYTES = 20 * 1024 * 1024  # a misbehaving endpoint must not OOM the agent
logger = logging.getLogger("model-aliases")


@dataclass(frozen=True)
class ProviderDescriptor:
    """Where and how to fetch candidates for one target provider."""

    name: str  # target provider string as configured
    base_url: str = ""  # "" = unknown/no endpoint
    models_url: str = ""  # explicit models endpoint; else base_url + "/models"
    models_dev_id: str = ""  # models.dev provider key ("" = none possible)
    api_key_env: str = ""  # env var holding Bearer token for live fetch


def _fetch_json(url: str, *, headers: dict[str, str] | None = None, timeout: float = 3.0) -> object:
    """GET ``url`` and parse the body as JSON. Raises on HTTP/network/JSON errors."""
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError(f"response from {url} exceeds {MAX_RESPONSE_BYTES} bytes")
    return json.loads(body)


def fetch_models_dev(*, timeout: float = 3.0) -> dict | None:
    """Fetch the models.dev catalog. None on any failure; never raises."""
    try:
        payload = _fetch_json(DEFAULT_MODELS_DEV_URL, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - network layer may raise anything
        logger.debug("model-aliases: models.dev fetch failed: %s", exc)
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _lookup_provider(catalog: dict, desc: ProviderDescriptor) -> dict | None:
    """Look up the provider entry: models_dev_id first, then name."""
    if not isinstance(catalog, dict):
        return None
    keys = [k for k in (desc.models_dev_id, desc.name) if k]
    for key in keys:
        entry = catalog.get(key)
        if isinstance(entry, dict):
            return entry
    return None


def models_dev_candidates(catalog: dict, desc: ProviderDescriptor) -> list[Candidate]:
    """Candidates from a models.dev-shaped catalog for ``desc``. Unknown -> []."""
    entry = _lookup_provider(catalog, desc)
    if entry is None:
        return []
    models = entry.get("models")
    if not isinstance(models, dict):
        return []
    out: list[Candidate] = []
    for model_id, model_entry in models.items():
        if not isinstance(model_entry, dict):
            continue
        try:
            out.append(make_candidate(model_entry, model_id=model_id))
        except Exception as exc:  # noqa: BLE001 - one bad entry must not kill the list
            logger.debug("model-aliases: skipping bad models.dev entry %s: %s", model_id, exc)
    return out


def _first_str(item: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _item_context(item: dict) -> int | None:
    for key in ("context_length", "context_window", "max_input_tokens"):
        value = item.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def parse_models_endpoint(payload: object) -> list[tuple[str, int | None]]:
    """Normalize a live /models payload into ``[(id, context), ...]``.

    Accepts a JSON list, ``{"data": [...]}`` or ``{"models": [...]}``; items
    are strings or dicts with ``id``/``name``/``model``. Malformed -> [].
    """
    if isinstance(payload, dict):
        items = payload.get("data")
        if not isinstance(items, list):
            items = payload.get("models")
        if not isinstance(items, list):
            return []
        default_ctx = payload.get("context_length")
        if not isinstance(default_ctx, int) or isinstance(default_ctx, bool):
            default_ctx = None
    elif isinstance(payload, list):
        items = payload
        default_ctx = None
    else:
        return []

    seen: set[str] = set()
    rows: list[tuple[str, int | None]] = []
    for item in items:
        if isinstance(item, str) and item:
            model_id = item
            ctx = default_ctx
        elif isinstance(item, dict):
            model_id = _first_str(item, ("id", "name", "model"))
            if model_id is None:
                continue
            ctx = _item_context(item)
            if ctx is None:
                ctx = default_ctx
        else:
            continue
        if model_id in seen:
            continue
        seen.add(model_id)
        rows.append((model_id, ctx))
    return rows


def fetch_live_ids(desc: ProviderDescriptor, *, timeout: float = 3.0) -> list[tuple[str, int | None]] | None:
    """Fetch and parse the provider's live /models endpoint. None on any failure."""
    if desc.models_url:
        url = desc.models_url
    elif desc.base_url:
        url = desc.base_url.rstrip("/") + "/models"
    else:
        return None
    headers: dict[str, str] = {}
    if desc.api_key_env:
        token = os.environ.get(desc.api_key_env)
        if token:
            headers["Authorization"] = f"Bearer {token}"
    try:
        payload = _fetch_json(url, headers=headers, timeout=timeout)
        return parse_models_endpoint(payload)
    except Exception as exc:  # noqa: BLE001 - network layer may raise anything
        logger.debug("model-aliases: live models fetch failed for %s: %s", desc.name, exc)
        return None


def live_candidates(rows: list[tuple[str, int | None]]) -> list[Candidate]:
    """Thin Candidates from live rows; all other metadata stays unknown."""
    return [Candidate(id=model_id, context=ctx) for model_id, ctx in rows]


def merge_candidates(
    models_dev_list: list[Candidate],
    live_list: list[tuple[str, int | None]],
) -> list[Candidate]:
    """Union by id; models.dev entries win (full metadata), order preserved."""
    merged: list[Candidate] = []
    seen: set[str] = set()
    for cand in models_dev_list:
        if cand.id in seen:
            continue
        seen.add(cand.id)
        merged.append(cand)
    for row in live_list:
        model_id = row[0]
        if model_id in seen:
            continue
        seen.add(model_id)
        merged.extend(live_candidates([row]))
    return merged

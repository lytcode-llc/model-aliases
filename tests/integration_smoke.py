"""Real-loader round-trip smoke test for the model-aliases v2 plugin.

Runs against the installed Hermes (hermes_cli / providers / hermes_constants),
using an isolated HERMES_HOME: a seeded last-known-good cache + a dead-port
custom_providers entry, so no network and no real provider is needed.

Usage (Hermes' own interpreter; no pytest required):

    MODEL_ALIASES_SMOKE_HOME=$(mktemp -d ...) ~/.hermes/hermes-agent/venv/bin/python tests/integration_smoke.py

Asserts the load-bearing behaviors of the flat design:
  1. the wrapped profile registers UNDER the real provider name,
  2. normalize_model_id resolves an alias (the agent-init / aux / fallback path),
  3. concrete ids pass through (None -> Hermes generic normalization),
  4. fetch_models returns real ids + alias ids,
  5. strict mode fails the load loudly when an alias matches nothing,
  6. the custom:<name> profile alias resolves for custom_providers entries.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERMES_REPO = Path.home() / ".hermes" / "hermes-agent"
PKG = "hermes_model_aliases_smoke"


def _ensure_hermes_importable() -> None:
    if HERMES_REPO.is_dir() and str(HERMES_REPO) not in sys.path:
        sys.path.insert(0, str(HERMES_REPO))
    import providers  # noqa: F401  (ImportError here = Hermes not installed)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _load_plugin() -> None:
    spec = importlib.util.spec_from_file_location(
        PKG, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    assert spec and spec.loader, "cannot build plugin spec"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = mod
    spec.loader.exec_module(mod)


def main() -> int:
    # HERMES_HOME must be set before ANY hermes module import.
    home = Path(os.environ.get("MODEL_ALIASES_SMOKE_HOME") or tempfile.mkdtemp(prefix="model-aliases-smoke-"))
    os.environ["HERMES_HOME"] = str(home)
    _ensure_hermes_importable()

    # Isolated config: one custom_providers entry (dead port -> live fetch fails
    # fast) referenced via model.provider; generous TTL so the seeded cache wins.
    _write(
        home / "config.yaml",
        "model_aliases:\n"
        "  refresh_seconds: 86400\n"
        "  glm-latest:\n"
        "    match: ['glm-*']\n"
        "model:\n"
        "  provider: local\n"
        "custom_providers:\n"
        "  - name: local\n"
        "    api: http://127.0.0.1:9/v1\n"
        "    key_env: LOCAL_KEY\n",
    )
    _write(
        home / "cache" / "model-aliases" / "local.json",
        json.dumps(
            {
                "version": 1,
                "updated_at": time.time(),
                "resolved": {"glm-latest": "glm-4.7"},
                "history": [],
                "real_ids": ["glm-4.6", "glm-4.7"],
            }
        ),
    )

    # Mirror production order: bundled discovery runs FIRST (the generic
    # 'custom' profile claims bare-name aliases there), then our user plugin
    # loads last and its alias claims win — exactly like Hermes' home-layer
    # scan runs after bundled discovery.
    from providers import list_providers  # noqa: F401
    list_providers()  # force bundled discovery now

    _load_plugin()

    from providers import get_provider_profile

    # Registered UNDER the real provider name (the wrap mechanism).
    profile = get_provider_profile("local")
    assert profile is not None, "wrapped profile 'local' was not registered"
    assert profile.base_url == "http://127.0.0.1:9/v1", f"base_url not copied: {profile.base_url!r}"

    resolved = profile.normalize_model_id("glm-latest")
    assert resolved == "glm-4.7", f"alias did not resolve via seeded cache: {resolved!r}"
    assert profile.normalize_model_id("glm-4.7") is None, "concrete id must pass through untouched"
    assert profile.normalize_model_id("GLM-LATEST") == "glm-4.7", "case-insensitive alias lookup failed"

    rows = profile.fetch_models()
    assert rows[:2] == ["glm-4.6", "glm-4.7"], f"real ids first: {rows!r}"
    assert "glm-latest" in rows, f"alias ids must be in picker rows: {rows!r}"

    # The custom:<name> alias resolves for custom_providers entries.
    via_alias = get_provider_profile("custom:local")
    assert via_alias is profile, "custom:local profile alias must resolve to the same profile"

    # Strict mode: an alias that matches nothing must fail registration loudly.
    time.sleep(0.05)  # beat config mtime+size caching
    _write(
        home / "config.yaml",
        "model_aliases:\n"
        "  strict: true\n"
        "  ghost:\n"
        "    match: ['no-such-model-*']\n"
        "model:\n"
        "  provider: local\n"
        "custom_providers:\n"
        "  - name: local\n"
        "    api: http://127.0.0.1:9/v1\n",
    )
    register_all = importlib.import_module(f"{PKG}.model_aliases.plugin").register_all
    try:
        register_all()
    except RuntimeError as e:
        assert "ghost" in str(e), f"strict failure must name the alias: {e}"
    else:
        raise AssertionError("strict mode must fail the load for an unresolvable alias")

    print("integration smoke: OK (wrap, normalize, passthrough, picker, custom alias, strict)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Real-loader round-trip smoke test for the model-aliases plugin.

Runs against the installed Hermes (hermes_cli / providers / hermes_constants),
using an isolated HERMES_HOME: a seeded last-known-good cache + a dead-port
custom target, so no network and no real provider is needed.

Usage (Hermes' own interpreter; no pytest required):

    HERMES_HOME=$(mktemp -d) ~/.hermes/hermes-agent/venv/bin/python tests/integration_smoke.py
    # or plain python3 if the hermes source dir is importable

Asserts the four load-bearing behaviors:
  1. the plugin registers a profile through the real registration seam,
  2. normalize_model_id resolves an alias (the agent-init / aux / fallback path),
  3. concrete ids pass through (None -> Hermes generic normalization),
  4. strict mode fails the load loudly when an alias cannot resolve.
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


def _ensure_hermes_importable() -> None:
    # Always prepend the hermes-agent repo root: the venv's editable install
    # maps the `providers` package only, while Hermes' own top-level modules
    # (hermes_yaml, utils, hermes_constants, ...) resolve from the repo root.
    if HERMES_REPO.is_dir() and str(HERMES_REPO) not in sys.path:
        sys.path.insert(0, str(HERMES_REPO))
    import providers  # noqa: F401  (ImportError here = Hermes not installed)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def main() -> int:
    # HERMES_HOME must be set before ANY hermes module import (homes can bind at import).
    home = Path(os.environ.get("MODEL_ALIASES_SMOKE_HOME") or tempfile.mkdtemp(prefix="model-aliases-smoke-"))
    os.environ["HERMES_HOME"] = str(home)

    _ensure_hermes_importable()

    # Isolated config: one custom target (dead port -> live fetch fails fast),
    # generous TTL so the seeded cache is trusted.
    _write(
        home / "config.yaml",
        "model_aliases:\n"
        "  refresh_seconds: 86400\n"
        "  targets:\n"
        "    smoke:\n"
        "      provider: custom:local\n"
        "      aliases:\n"
        "        stable:\n"
        "          match: ['*']\n"
        "providers:\n"
        "  local:\n"
        "    api: http://127.0.0.1:9/v1\n"
        "    transport: chat_completions\n",
    )
    _write(
        home / "cache" / "model-aliases" / "alias-smoke.json",
        json.dumps(
            {"version": 1, "updated_at": time.time(), "resolved": {"stable": "glm-4.7"}, "history": []}
        ),
    )

    # Load the plugin exactly the way Hermes does: spec_from_file_location on the
    # root __init__.py with the directory as package root (registration at import).
    spec = importlib.util.spec_from_file_location(
        "hermes_model_aliases_smoke", ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    assert spec and spec.loader, "cannot build plugin spec"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    from providers import get_provider_profile

    profile = get_provider_profile("alias-smoke")
    assert profile is not None, "profile alias-smoke was not registered"
    assert profile.base_url == "http://127.0.0.1:9/v1", f"base_url not copied: {profile.base_url!r}"

    resolved = profile.normalize_model_id("stable")
    assert resolved == "glm-4.7", f"alias did not resolve via seeded cache: {resolved!r}"
    assert profile.normalize_model_id("glm-4.7") is None, "concrete id must pass through untouched"
    assert profile.normalize_model_id("STABLE") == "glm-4.7", "case-insensitive alias lookup failed"

    rows = profile.fetch_models()
    assert rows == ["stable"], f"picker catalog must list alias ids: {rows!r}"

    assert profile.live_context_length("stable") is None, "unknown target metadata must stay None"

    # Strict mode: a never-resolvable alias must fail registration loudly.
    time.sleep(0.05)  # beat config mtime+size caching
    _write(
        home / "config.yaml",
        "model_aliases:\n"
        "  strict: true\n"
        "  targets:\n"
        "    ghosty:\n"
        "      provider: custom:local\n"
        "      aliases:\n"
        "        ghost:\n"
        "          match: ['no-such-model-*']\n"
        "providers:\n"
        "  local:\n"
        "    api: http://127.0.0.1:9/v1\n",
    )
    register_all = importlib.import_module(
        "hermes_model_aliases_smoke.model_aliases.plugin"
    ).register_all
    try:
        register_all()
    except RuntimeError as e:
        assert "ghost" in str(e), f"strict failure must name the alias: {e}"
    else:
        raise AssertionError("strict mode must fail the load for an unresolvable alias")
    assert get_provider_profile("alias-ghosty") is None, "strict failure must register nothing"

    print("integration smoke: OK (register, normalize, passthrough, picker, strict)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

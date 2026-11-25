"""Real-loader round-trip smoke test for the model-aliases v3 plugin.

Runs against the installed Hermes (hermes_cli / providers / hermes_constants),
using an isolated HERMES_HOME: a seeded last-known-good cache + a dead-port
custom_providers entry joined to a provider-alias group by name, so no network
and no real provider is needed.

Usage (Hermes' own interpreter; no pytest required):

    MODEL_ALIASES_SMOKE_HOME=$(mktemp -d ...) ~/.hermes/hermes-agent/venv/bin/python tests/integration_smoke.py

Asserts the load-bearing behaviors of the v3 design:
  1. the group profile registers under the developer's chosen name (name join),
  2. normalize_model_id resolves an alias (the agent-init / aux / fallback path),
  3. concrete ids pass through (None -> Hermes generic normalization),
  4. fetch_models returns real ids + alias ids,
  5. the request-path seam (normalize_model_for_provider) resolves via the
     neutral group name AND the three early-return families are pinned,
  6. the naming guard rejects a group name that would early-return,
  7. strict mode fails the load loudly when an alias matches nothing.
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


def _write_config(home: Path, body: str) -> None:
    _write(home / "config.yaml", body)


def main() -> int:
    # HERMES_HOME must be set before ANY hermes module import.
    home = Path(os.environ.get("MODEL_ALIASES_SMOKE_HOME") or tempfile.mkdtemp(prefix="model-aliases-smoke-"))
    os.environ["HERMES_HOME"] = str(home)
    _ensure_hermes_importable()

    # Isolated config: one provider-alias group joined to a custom_providers
    # entry BY NAME (group key == entry name); dead port -> live fetch fails
    # fast; generous TTL so the seeded cache wins.
    _write_config(
        home,
        "model_aliases:\n"
        "  provider_alias_to_many_model_aliases:\n"
        "    local-alias:\n"
        "      provider: local-alias\n"
        "      glm-latest:\n"
        "        match: ['glm-*']\n"
        "model:\n"
        "  provider: local-alias\n"
        "custom_providers:\n"
        "  - name: local-alias\n"
        "    api: http://127.0.0.1:9/v1\n"
        "    key_env: LOCAL_KEY\n",
    )
    _write(
        home / "cache" / "model-aliases" / "local-alias.json",
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

    # Mirror production order: bundled discovery runs FIRST, then our user
    # plugin loads last and its alias claims win.
    from providers import list_providers  # noqa: F401
    list_providers()

    _load_plugin()

    from providers import get_provider_profile

    # 1. Registered under the developer's chosen name (the name join).
    profile = get_provider_profile("local-alias")
    assert profile is not None, "group profile 'local-alias' was not registered"
    assert profile.base_url == "http://127.0.0.1:9/v1", f"base_url not copied: {profile.base_url!r}"
    assert "custom:local-alias" in profile.aliases, f"custom:local claim missing: {profile.aliases!r}"

    # 2/3. normalize resolves the alias; concrete ids pass through.
    assert profile.normalize_model_id("glm-latest") == "glm-4.7"
    assert profile.normalize_model_id("GLM-LATEST") == "glm-4.7"
    assert profile.normalize_model_id("glm-4.7") is None

    # 4. Picker: real ids + alias ids.
    rows = profile.fetch_models()
    # A GROUP wrap lists ONLY its alias table (the real catalog belongs to the
    # target's own row); the canonical picker path calls fetch_models directly.
    assert rows == ["glm-latest"], f"group picker rows must be alias-only: {rows!r}"

    _assert_picker_floor(profile)

    # 5. The request-path seam: generic normalization dispatches through the
    # group name (the direct-profile calls above would mask an early-return
    # family regression).
    from hermes_cli.model_normalize import normalize_model_for_provider

    assert normalize_model_for_provider("glm-latest", "local-alias") == "glm-4.7"
    # Pin the three early-return families (README's table).
    assert normalize_model_for_provider("glm-latest", "opencode-go") == "glm-latest"
    assert normalize_model_for_provider("glm-latest", "anthropic") == "glm-latest"  # no dots to convert
    assert normalize_model_for_provider("glm-latest", "openrouter") != "glm-latest"  # vendor-prefixed

    # 6. The naming guard rejects a group name that would early-return.
    time.sleep(0.05)
    _write_config(
        home,
        "model_aliases:\n"
        "  provider_alias_to_many_model_aliases:\n"
        "    opencode-go-fast:\n"
        "      provider: opencode-go\n"
        "      glm-latest:\n"
        "        match: ['glm-*']\n"
        "model:\n"
        "  provider: local-alias\n"
        "custom_providers:\n"
        "  - name: local-alias\n"
        "    api: http://127.0.0.1:9/v1\n",
    )
    register_all = importlib.import_module(f"{PKG}.model_aliases.plugin").register_all
    try:
        register_all()
    except Exception as e:
        assert "opencode-go-fast" in str(e) and "family" in str(e), f"guard must name the alias and the family: {e}"
    else:
        raise AssertionError("naming guard must fail the load for an early-return group name")

    # 6b. The canonicalization branch of the guard: names Hermes rewrites
    # (normalize_provider lowercases + alias table, e.g. local -> custom)
    # never reach a profile registered under that name.
    time.sleep(0.05)
    _write_config(
        home,
        "model_aliases:\n"
        "  provider_alias_to_many_model_aliases:\n"
        "    local:\n"
        "      provider: local\n"
        "      glm-latest:\n"
        "        match: ['glm-*']\n"
        "model:\n"
        "  provider: local-alias\n"
        "custom_providers:\n"
        "  - name: local-alias\n"
        "    api: http://127.0.0.1:9/v1\n",
    )
    try:
        register_all()
    except Exception as e:
        assert "local" in str(e) and "canonicaliz" in str(e), f"canonicalization guard must fire: {e}"
    else:
        raise AssertionError("canonicalization guard must fail the load for a rewritten group name")

    # 7. Strict mode: an alias that matches nothing must fail registration loudly.
    time.sleep(0.05)
    _write_config(
        home,
        "model_aliases:\n"
        "  strict: true\n"
        "  provider_alias_to_many_model_aliases:\n"
        "    local-alias:\n"
        "      provider: local-alias\n"
        "      ghost:\n"
        "        match: ['no-such-model-*']\n"
        "model:\n"
        "  provider: local-alias\n"
        "custom_providers:\n"
        "  - name: local-alias\n"
        "    api: http://127.0.0.1:9/v1\n",
    )
    try:
        register_all()
    except RuntimeError as e:
        assert "ghost" in str(e), f"strict failure must name the alias: {e}"
    else:
        raise AssertionError("strict mode must fail the load for an unresolvable alias")

    print("integration smoke: OK (name-join, normalize, passthrough, picker, seam, guard, strict)")
    return 0



def _assert_picker_floor(profile) -> None:
    """Regression: /model's curated floor must list the aliases for the group
    provider (curated['ocg-alias'] was None when static/fallback were empty)."""
    assert profile.static_models == ("glm-latest",), profile.static_models
    assert profile.fallback_models == ("glm-latest",), profile.fallback_models
    from hermes_cli.model_switch_providers import _build_curated_lists

    curated = _build_curated_lists(profile.name, profile.base_url, "glm-latest")
    assert "glm-latest" in (curated.get(profile.name) or []), (
        f"curated floor for {profile.name!r} missing the alias: {curated.get(profile.name)}"
    )


if __name__ == "__main__":
    sys.exit(main())

# model-aliases — floating model aliases for Hermes

A Hermes **model-provider plugin** that ends manual model-id updates. You write a
stable alias once (`fast`, `stable`, …); the plugin resolves it to the **newest
matching model** your provider offers — every startup, automatically.

The design ports the semantics of
[`opencode-model-aliases`](https://www.npmjs.com/package/opencode-model-aliases)
(match / exclude / capability filters / `latest` selection, strict & tolerant
policies) to Hermes' provider seam.

## How it works

Hermes normalizes every (provider, model) pair through one seam —
`normalize_model_for_provider()`, consulted at agent init, `/model` switches,
auxiliary tasks (`auxiliary.<task>`), and fallback providers. It asks the active
provider profile's `normalize_model_id()` first. This plugin registers a profile
per configured target whose `normalize_model_id()` maps alias → newest resolved
concrete id:

1. You configure `model: { provider: alias-oc, default: fast }` **once**.
2. At each startup, the plugin resolves `fast` against the models.dev catalog
   and the target's live `/models` endpoint.
3. The agent runs against the newest `glm-*` (or whatever your globs match).
   When the provider ships a newer version, the next startup picks it up.
4. `/model` shows the alias ids (they never go stale), and per-model metadata
   (context window, tool support, vision) is attached to the alias from the
   resolved model's catalog entry.

The same alias works in every place a model id is accepted:

```yaml
model:
  provider: alias-oc
  default: fast            # floats forever

auxiliary:
  summarizer:
    provider: alias-oc     # aux calls normalize through the same seam
    model: stable

fallback_providers:
  - provider: alias-oc
    model: fast
```

## Install

```bash
git clone <this-repo> ~/.hermes/plugins/model-providers/model-aliases
# or: ln -s /path/to/model-aliases ~/.hermes/plugins/model-providers/model-aliases
```

Restart Hermes. Discovery is lazy — the profile appears on first provider
lookup; verify with:

```bash
hermes doctor        # profile listed under Provider Connectivity with a /models probe
```

## Configuration

All config lives in `~/.hermes/config.yaml` (one file, native to Hermes):

```yaml
model_aliases:
  strict: false          # true: fail plugin load loudly if any alias cannot resolve
  debug: false           # log each resolution
  refresh_seconds: 3600  # how long a resolved state is trusted before re-fetching live /models

  targets:
    oc:                          # becomes profile name "alias-oc"
      provider: opencode-go      # bundled provider, or a providers: name / custom:<name>
      aliases:
        fast:
          match: ["glm-*"]             # glob vs raw model id
          exclude: ["*-preview"]       # never floats onto previews
          filter:
            minContext: 128000         # required context window (unknown = fails)
            minAge: 48h                # skip models released < 48h ago (h or d)
            capabilities:
              tools: true              # required tool support (unknown = fails)
              input: [text, image]     # every listed modality must be present
              output: [text]
          select: { strategy: latest } # only strategy supported
        stable:
          match: ["glm-4.7*"]          # a narrow glob is a natural freeze
```

### Match semantics

- Globs match the **raw model id** (`glm-*`) *and* the provider-prefixed form
  (`opencode-go/glm-*`), so OpenCode-plugin-style patterns port over verbatim.
- Aggregator ids containing slashes (`vendor/fast-1`) match raw patterns too.
- `exclude` always wins over `match`.

### Selection

`latest` ranks survivors by **newest `release_date` first** (undated ids sort
last), breaking ties with a natural version key (`glm-4.7` > `glm-4.6` >
`glm-4`) then id descending. Date-primary so a same-version `-preview` released
before its base release loses to the base; a preview of a genuinely newer
version still wins on its later date.

### Resolution sources

1. **models.dev** (same catalog OpenCode uses) — context, modalities,
   tool support, release dates. Its capability entries are cached next to the
   resolution state, so warm starts keep picker badges without re-fetching.
2. **Live `GET /models`** on the target — fetched when state is stale, an
   alias is unresolved, or on-demand from `/model`. Required for custom/local
   endpoints that are not on models.dev. Live fetches for multiple targets
   run concurrently at startup.

Because undated live-only ids sort behind dated models.dev entries, a brand-new
model that only appears on your endpoint loses to dated catalog models until
it is indexed (give such models a narrow `match` if this matters).

### Named custom provider keys used

From `providers.<name>`: `api` (or `base_url`/`url`), `key_env`, inline
`api_key` (served through the profile's credential hook), `transport` (wire
mode: `chat_completions` / `anthropic_messages` / `codex_responses`),
`catalog_provider` (models.dev id for your endpoint's models), `enabled:
false` (target refused at load), `key_cmd` (warned: unsupported).

### Failure policy

- **Tolerant (default):** network failure or no matches → keep the
  **last-known-good** mapping from `~/.hermes/cache/model-aliases/`, log a
  startup error only when an alias has never resolved. Drift (winner changed)
  is logged: `model-aliases: alias-oc/fast: glm-4.6 -> glm-4.7 (detected …)`.
- **Strict (`strict: true`):** any alias without a resolution fails the plugin
  load with a visible error instead of running stale.

Config errors (unknown keys, bad types) always log loudly and skip
registration.

## Alias stability vs. "latest"

If you want stability *inside* the floating regime, use `exclude` (skip
previews/canaries), `minAge` (skip day-1 releases) or a narrow `match`
(`glm-4.7*` floats across patches, not majors). There is deliberately **no
`pin` option** — a pinned alias is just a model id, which plain
`model.default` already does without this plugin.

## Limitations (v1)

- **OAuth-backed targets** (qwen-oauth, nous, minimax-oauth, …) are not
  supported: an alias profile only carries api_key-style credentials. You get
  a startup warning if you configure one.
- `key_cmd`-only named custom providers: resolution cannot fetch the live
  catalog (set `key_env` or `api_key` instead).
- Alias display names are not customizable; the picker shows the alias id.
- Config is read at plugin import — restart Hermes after editing
  `model_aliases`.

## Development

```bash
python3 -m pytest tests/ -q                 # unit tests (no Hermes required)
python3 tests/integration_smoke.py          # real-loader round-trip (uses Hermes venv)
```

Layout: `model_aliases/{config,resolver,catalog,state}.py` are pure and fully
unit-tested; `model_aliases/{plugin,profile}.py` contain the Hermes seams
(config loading, `ProviderProfile` construction, `register_provider`).

## Install for development

```bash
rm -rf ~/.hermes/plugins/model-providers/model-aliases
ln -s "$PWD" ~/.hermes/plugins/model-providers/model-aliases
```

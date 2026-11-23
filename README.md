# model-aliases

Floating model aliases for Hermes. You configure a stable id once (`fast`, `stable`),
and the plugin resolves it to the newest model your provider actually offers, every
time Hermes starts. When the provider ships a newer release, the alias follows it
without you editing anything.

The alias semantics (match / exclude / capability filters, `latest` selection,
strict and tolerant failure policies) are ported from the
[`opencode-model-aliases`](https://www.npmjs.com/package/opencode-model-aliases)
plugin for OpenCode.

## How it works

Hermes translates every model id through one seam,
`normalize_model_for_provider()` (`hermes_cli/model_normalize.py`), and asks the
active provider profile's `normalize_model_id()` first. This plugin registers one
provider profile per configured target (named `alias-<target key>`) whose
`normalize_model_id()` maps an alias to the newest matching concrete model id.

Concretely:

1. You write `model_aliases:` in `~/.hermes/config.yaml` and point `model.provider`
   at the alias profile.
2. At plugin load (import time), the plugin resolves every alias against the
   models.dev catalog and, when needed, the target's live `/models` endpoint.
3. `normalize_model_id("fast")` returns the resolved id at agent init, on `/model`
   switches, for auxiliary tasks (`auxiliary.<task>`), and for fallback providers.
4. `/model` lists the alias ids themselves, which never go stale, and the metadata
   attached to an alias (context window, capability flags) comes from the resolved
   model's models.dev entry.

Concrete model ids are left alone: `normalize_model_id()` returns `None` for
anything that is not an alias, so Hermes' generic normalization handles them
unchanged. If a profile hook ever raised, Hermes would fall back to its generic
rules rather than break the request.

## Requirements

- Hermes installed and importable (`hermes_cli`, `providers`, `hermes_constants`).
  The plugin is loaded by Hermes' own plugin loader.
- Python 3.11 (the interpreter in Hermes' virtualenv). The plugin uses only the
  standard library, so there are no third-party dependencies to install.
- Network access to `https://models.dev/api.json` and to the target provider's
  `/models` endpoint, for cold starts. A warm cache needs no network at all.
- Write access to `$HERMES_HOME/cache/model-aliases/` (`~/.hermes/cache/model-aliases/`
  by default) for the resolution cache.

## Install

```bash
git clone <this-repo> ~/.hermes/plugins/model-providers/model-aliases
```

or, for development, symlink the working copy:

```bash
ln -s "$PWD" ~/.hermes/plugins/model-providers/model-aliases
```

Hermes discovers user plugins under `$HERMES_HOME/plugins/`, and a
`model-providers/<name>/` directory is loaded as a model-provider plugin.
Registration happens at import of the repository root `__init__.py`, so the
profile exists as soon as the plugin is loaded. Restart Hermes after installing
or after editing the config: the `model_aliases:` section is read at import.

## First 5 minutes

**1. Add the config.** Edit `~/.hermes/config.yaml`:

```yaml
model_aliases:
  debug: true
  targets:
    oc:
      provider: opencode-go
      aliases:
        fast:
          match: ["glm-*"]
          exclude: ["*-preview"]
          filter:
            minContext: 128000
            capabilities:
              tools: true
        stable:
          match: ["glm-4.7*"]

model:
  provider: alias-oc
  default: fast
```

`model.provider: alias-oc` selects the alias profile (the profile name is
`alias-` plus the target key), and `model.default: fast` is the alias you want
to run.

**2. Restart Hermes.** The plugin reads the config at import.

**3. Check that the profile is live.**

```bash
hermes doctor
```

Doctor probes the alias profile like any other api-key provider: it requests the
target's `/models` endpoint with the key from the target's env var and prints a
row labelled `Aliases (opencode-go)`. The row only appears when the target
exposes a key env var (`key_env` on a named provider block, the provider's own
env var for a bundled target). Doctor reads env vars only, so a key supplied
inline as `api_key` is used at runtime but shows as not configured in doctor.

With `debug: true`, the plugin logs the resolution it performed:

```
model-aliases: oc resolved {'fast': 'glm-4.7', 'stable': 'glm-4.7'}
model-aliases: registered ['alias-oc']
```

**4. Use it.** The alias works everywhere a model id is accepted:

```yaml
model:
  provider: alias-oc
  default: fast            # floats forever

auxiliary:
  summarizer:
    provider: alias-oc     # auxiliary calls normalize through the same seam
    model: stable

fallback_providers:
  - provider: alias-oc
    model: fast
```

In a session, `/model` shows the alias ids (`fast`, `stable`) for the alias
profile, not the concrete ids behind them.

**5. When the provider ships a new release.** The next startup re-resolves and
the alias moves with it. When the winner changes, the plugin logs one drift line:

```
model-aliases: alias-oc/fast: glm-4.6 -> glm-4.7 (detected 2026-11-23T09:14:22)
```

The drift is also recorded in the cache file (see
[Inspecting drift and the cache](#inspecting-drift-and-the-cache)).

## Configuration reference

Everything lives under the `model_aliases:` key in `~/.hermes/config.yaml`.
Unknown keys are rejected with the offending path in the message, so a typo
fails loudly at startup instead of being ignored. Edit the file directly:
`hermes config set` does not know this section and warns about unrecognized
keys.

```yaml
model_aliases:
  strict: false          # true: fail the plugin load instead of degrading
  debug: false           # log every resolution
  refresh_seconds: 3600  # how long a cached resolution is trusted

  targets:
    oc:                          # key must match ^[a-z0-9][a-z0-9._-]*$
      provider: opencode-go      # bundled provider name, or a providers: block
      aliases:
        fast:
          match: ["glm-*"]             # required; glob against the model id
          exclude: ["*-preview"]       # optional; exclude always wins
          filter:
            minContext: 128000         # required context window
            minAge: 48h                # '<n>h' or '<n>d'
            capabilities:
              tools: true              # required tool support
              input: [text, image]     # every listed modality must be present
              output: [text]
          select:
            strategy: latest           # only 'latest' is supported
```

### Top-level keys

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `strict` | boolean | `false` | Turn any resolution failure into a plugin load failure. |
| `debug` | boolean | `false` | Log each resolution, and each warm-cache hit. |
| `refresh_seconds` | positive integer | `3600` | Age at which a cached resolution is considered stale. |

### `targets.<key>`

| Key | Type | Required | Meaning |
| --- | --- | --- | --- |
| `provider` | non-empty string | yes | The provider to alias. A bundled Hermes provider name, or the name of a block under `providers:` (with or without the `custom:` prefix). |
| `aliases` | mapping | yes | At least one alias. The keys are the alias ids you will use. |

Target keys must match `^[a-z0-9][a-z0-9._-]*$` (lowercase, no spaces). The key
becomes the profile name: target `oc` registers the profile `alias-oc`, which you
then use as `model.provider`.

### `targets.<key>.aliases.<alias>`

| Key | Type | Required | Meaning |
| --- | --- | --- | --- |
| `match` | string or list of strings | yes | Glob patterns the candidate id must match. |
| `exclude` | string or list of strings | no | Glob patterns that disqualify a candidate. Wins over `match`. |
| `filter.minContext` | positive integer | no | Minimum context window. |
| `filter.minAge` | `'<n>h'` or `'<n>d'` | no | Minimum age of the release. |
| `filter.capabilities.tools` | boolean | no | Required tool-calling support. |
| `filter.capabilities.input` | string or list of strings | no | Required input modalities. |
| `filter.capabilities.output` | string or list of strings | no | Required output modalities. |
| `select.strategy` | string | no | Must be `latest` if present. |

Pattern lists must not be empty, and every item must be a non-empty string.
`select` may be omitted; `latest` is the only strategy implemented.

### Provider-side keys

For a target that names a block under `providers:` (a custom or local endpoint),
the plugin reads these keys from that block:

| Key | Meaning |
| --- | --- |
| `api`, `base_url` or `url` | The endpoint. One of them is required for a named block. |
| `key_env` or `api_key_env` | Env var holding the bearer token. Used for the live `/models` fetch and as the profile's credential env var. |
| `api_key` | Inline key, served through the profile's credential hook. Not visible to `hermes doctor`, which reads env vars only. |
| `transport` or `api_mode` | Wire protocol: `chat_completions` (default), `anthropic_messages`, `codex_responses`, `bedrock_converse`. Hermes' own `openai_chat` is accepted as the equivalent of `chat_completions`. |
| `catalog_provider` | The models.dev provider id to read this endpoint's models from. |
| `enabled: false` | The target is refused at load with an error. |
| `key_cmd` | Not supported. The plugin warns and cannot fetch the live catalog for this target. |

```yaml
providers:
  local:
    api: http://127.0.0.1:8000/v1
    transport: chat_completions
    key_env: LOCAL_API_KEY
    catalog_provider: ollama

model_aliases:
  targets:
    local:
      provider: custom:local
      aliases:
        fast:
          match: ["llama*"]
```

Notes on named blocks:

- The block must live in the `providers:` mapping. Hermes' legacy list-form
  `custom_providers:` is not read by this plugin.
- The plugin reads `providers:` first and only falls back to `custom_providers:`
  when `providers:` is absent or empty.
- A bare name that matches a `providers:` block uses that block; otherwise the
  name is looked up in the bundled provider registry.
- A target that resolves to another alias profile is refused (self-reference).

## Matching and selection

**Matching.** A candidate matches when its raw model id (`glm-4.7`) or its
provider-prefixed id (the configured provider string plus the id, for example
`opencode-go/glm-4.7`) matches any `match` pattern, and no `exclude` pattern
matches either form. Patterns are shell globs
(`fnmatch` syntax): `*`, `?`, `[seq]`. Aggregator ids that contain a slash
(`vendor/fast-1`) match raw patterns too. An empty `match` never matches.

**Filtering.** After matching, a candidate must pass every configured filter:

| Filter | Passes when | Unknown metadata |
| --- | --- | --- |
| `minContext` | `context >= minContext` | Fails (unknown context cannot be verified) |
| `capabilities.tools` | `tool_call == the requested value` | Fails |
| `capabilities.input` / `output` | every requested modality is present | Fails |
| `minAge` | released on or before the cutoff | Passes (no evidence of recency) |

Boundaries are inclusive: a model with exactly `minContext` tokens passes, and a
model released exactly at the `minAge` cutoff passes. `minAge` is compared by
release date, so `48h` excludes models released today or yesterday.

**Selection.** Survivors are ranked by newest `release_date` first (undated ids
sort last), then by a natural version key descending (`glm-4.7` > `glm-4.6` >
`glm-4`), then by id descending. Date is primary so a same-version `-preview`
released before its base release loses to the base, while a preview of a
genuinely newer version still wins on its later date.

**Alias independence.** Every alias is resolved on its own against the same
candidate pool. An alias that matches nothing resolves to nothing and does not
affect the others.

## Resolution, cache and drift

**Candidate sources.**

1. **models.dev** (`https://models.dev/api.json`) is the primary catalog. It
   supplies release dates, context windows, tool support and modalities. One
   fetch per process, shared by all targets; a failed fetch is retried after 60
   seconds instead of on every target.
2. **The target's live `/models` endpoint** (`<base_url>/models`, or the
   provider's own `models_url`) is fetched when the cache is stale, when an alias
   is still unresolved, or when the picker forces a refresh. It is required for
   endpoints that are not on models.dev. Live fetches for different targets run
   concurrently at startup.

Both sources are merged by id, and models.dev entries win, because they carry the
full metadata. Live-only ids contribute a thin candidate (id plus context window,
if the endpoint reports one). Because undated ids sort behind dated ones, a brand
new model that only appears on your endpoint loses to dated catalog models until
it is indexed; give such a model a narrow `match` if that matters.

**Startup budget.** Hermes allows 10 seconds per plugin load (configurable with
`plugins.load_timeout_seconds`). A warm cache short-circuits with zero network
calls; otherwise the plugin does one models.dev fetch plus at most one live fetch
per target.

**Warm cache.** A cached resolution is reused without any network call when both
hold: it is younger than `refresh_seconds`, and it resolves every configured
alias. Otherwise the plugin re-resolves.

**Last-known-good.** If a re-resolution fails (network error, endpoint down), the
previous mapping is kept and used. An alias that has never resolved is reported
as an error instead.

**Drift.** When a re-resolution picks a different model for an alias, the change
is logged and recorded in the cache history. The same alias can drift at most
once per resolution, and the history keeps the 20 most recent events.

## Failure policy

- **Tolerant (default).** A target that cannot be described, or an alias that
  cannot be resolved, is logged as an error. The other targets still register,
  and a previously resolved alias keeps its last-known-good value.
- **Strict (`strict: true`).** Any of those failures raises an error during
  plugin load. Nothing is registered, so the alias provider does not exist at
  all, and Hermes reports the failed import.
- **Config errors** (unknown key, wrong type, bad glob) are always logged and
  skip registration entirely, in both modes. The message names the exact key
  path, for example `model-aliases: invalid config: targets.oc.aliases.fast.filter.minContext: expected a positive integer`.

## Inspecting drift and the cache

The plugin writes one JSON file per profile under
`$HERMES_HOME/cache/model-aliases/` (`~/.hermes/cache/model-aliases/` by default),
named after the profile, for example `alias-oc.json`:

```json
{
  "version": 1,
  "updated_at": 1764000000.0,
  "resolved": {
    "fast": "glm-4.7",
    "stable": "glm-4.7"
  },
  "history": [
    {"alias": "fast", "from": "glm-4.6", "to": "glm-4.7", "at": 1763990000.0}
  ],
  "capabilities": {
    "fast": {"id": "glm-4.7", "release_date": "2026-02-10", "limit": {"context": 200000}}
  }
}
```

- `resolved` is the current alias to model id mapping.
- `history` is the drift log, oldest first, capped at 20 entries.
- `capabilities` caches the models.dev entry of each resolved model, so warm
  starts keep picker badges and routing metadata without a network call.
- `updated_at` is the epoch timestamp of the last resolution and drives the
  `refresh_seconds` check.
- A file with an unexpected `version` or malformed JSON is treated as "no cache",
  so the next start resolves from scratch.

To see the drift lines, read the Hermes logs (`hermes logs`, i.e.
`~/.hermes/logs/agent.log`) and look for the `model-aliases` logger:

```
model-aliases: alias-oc/fast: glm-4.6 -> glm-4.7 (detected 2026-11-23T09:14:22)
```

With the default `plugins.isolation: in_process` the plugin logs into the Hermes
log at INFO and above, so `debug: true` output shows up there. Under
`plugins.isolation: host` the plugin runs in the plugin host, which forwards
WARNING and above only, so the debug lines are not shown.

To force a re-resolution, delete the cache file (or lower `refresh_seconds` and
wait for it to expire). The `/model` picker also forces a live refresh, and a
failed refresh never wipes a good snapshot.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `model-aliases: target 'oc': alias(es) 'fast' resolved to no model (check match globs against the catalog)` | No candidate matched `match`, or every candidate was excluded or filtered out. | Turn on `debug: true` and check the resolved map. Widen `match`, drop an over-strict `exclude`, or relax `filter` (unknown context or tool support fails a requirement). |
| The alias resolves but the agent reports an unknown model | The resolved id is not served by the target, for example a catalog entry the endpoint does not implement. | Narrow `match` to the ids the endpoint really serves, or point `catalog_provider` at the right models.dev entry. |
| The alias points at an old model after the provider released a new one | The cached resolution is younger than `refresh_seconds`. | Lower `refresh_seconds`, delete `~/.hermes/cache/model-aliases/alias-<key>.json`, or open the `/model` picker, which forces a live refresh. |
| `model-aliases: alias-oc: target 'qwen-oauth' uses auth_type='oauth'; an alias profile only carries api_key-style credentials, so inference may fail without an env key` | The target authenticates with OAuth. | Point the alias at a target that uses an api key, or declare the endpoint as a `providers:` block with `key_env` (or `api_key`) and alias that block instead. |
| `model-aliases: alias-oc: providers.local uses key_cmd, which an alias profile cannot resolve; set key_env or api_key` | The named provider block fetches its key with a command. | Set `key_env` (preferred) or `api_key` on that block instead. |
| Plugin does not load and Hermes reports an import error | `strict: true` and something failed: an unknown provider, a disabled target, or an unresolvable alias. | Read the message, it names the target and the alias. Fix the cause, or set `strict: false` to degrade instead. |
| `model-aliases: invalid config: <path>: <message>` | A typo or a wrong type in the `model_aliases:` section. | Fix the key path named in the message. Nothing is registered until it is valid. |
| `hermes doctor` does not list the alias profile | The target exposes no key env var (`key_env` unset, or the bundled provider has no non-URL env var). | Set `key_env` on the provider block. Doctor reads env vars only, so an inline `api_key` never shows as configured there. |
| `unknown target provider 'x' (not bundled, not under providers:)` | The provider name is neither a bundled provider nor a block under `providers:`. | Add the block under `providers:` (mapping form, with an endpoint) and reference it as `custom:<name>`. |
| `target 'x' resolves to a model-aliases profile (self-reference)` | The target points at an alias profile. | Point it at the real provider. |
| The new alias is ignored | The config is read at plugin import only. | Restart Hermes after editing `model_aliases:`. |

## Limitations

- There is deliberately no `pin` option. A pinned alias is just a model id, and
  `model.default` already covers that without this plugin.
- OAuth-backed targets are not supported: an alias profile carries api-key-style
  credentials only. You get a startup warning when you configure one.
- `key_cmd`-only provider blocks cannot resolve, because the alias profile
  cannot run a command to fetch the key.
- Alias display names are not customizable. The picker shows the alias id.
- Alias namespaces are per target. The same alias name under two targets is
  two independent aliases with two independent resolutions.
- Live-only model ids carry no release date, so they sort behind dated catalog
  models.
- The config is read at plugin import. Restart Hermes after changing it.

## Development

```bash
python3 -m pytest tests/ -q                                   # 104 unit tests, no Hermes needed
HERMES_HOME=$(mktemp -d) ~/.hermes/hermes-agent/venv/bin/python tests/integration_smoke.py
```

The smoke test loads the plugin exactly the way Hermes does (real loader, real
provider registry, isolated `HERMES_HOME`, no network) and asserts registration,
alias resolution, pass-through of concrete ids, the picker catalog and the
strict-mode failure.

Layout:

- `model_aliases/config.py` parses the `model_aliases:` section into typed
  dataclasses and rejects unknown keys.
- `model_aliases/resolver.py` matches, filters and selects. Pure.
- `model_aliases/catalog.py` sources candidates from models.dev and live
  `/models`.
- `model_aliases/state.py` owns the on-disk cache and drift history.
- `model_aliases/plugin.py` is the Hermes seam: config loading, target
  description, resolution, registration.
- `model_aliases/profile.py` builds the `ProviderProfile` that serves the aliases.

## Contributing

Bugs and pull requests are welcome.

- **Reporting a bug.** Include your Hermes version, the relevant part of your
  `model_aliases:` section (redact keys and URLs), the plugin log lines
  (`hermes logs`, logger `model-aliases`), and the resolved map from the cache
  file if it exists. A failing `match` glob against a known catalog is the most
  useful thing to include.
- **Submitting a pull request.** Branch from `main`, keep the change focused, and
  make sure both test commands pass before opening it. New behavior needs a
  unit test; changes to the Hermes seam need the smoke test updated too.
- **Code standards.** `config.py`, `resolver.py` and `state.py` stay pure: no
  network, no filesystem, no Hermes imports. `catalog.py` owns all network access
  (models.dev and live `/models`) behind the `_fetch_json` seam so tests can
  stub it. The Hermes seams stay in `plugin.py` and `profile.py`. Keep
  `normalize_model_id()` free of I/O, it is on the hot path. Use type hints and
  a module docstring that states the contract, keep error messages actionable
  (they name the offending key path), and follow the existing frozen-dataclass
  and pure-function style.

## License

[MIT](LICENSE) — Copyright (c) 2026 Paul Chason.

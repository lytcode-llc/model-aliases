# model-aliases

Floating model aliases for Hermes.

You declare a stable model id once (`glm-latest`) and the plugin resolves it to the
newest matching model your provider actually offers, every time Hermes starts. When
the provider ships a newer release, the alias follows it. No config edits.

The alias semantics (match / exclude / capability filters, `latest` selection, strict
and tolerant failure policies) are ported from the
[`opencode-model-aliases`](https://www.npmjs.com/package/opencode-model-aliases)
plugin for OpenCode.

## Contents

- [What it is](#what-it-is)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Install](#install)
- [Configuration reference](#configuration-reference)
- [First 5 minutes](#first-5-minutes)
- [Support matrix](#support-matrix)
- [Troubleshooting](#troubleshooting)
- [Cache and drift](#cache-and-drift)
- [Failure policy](#failure-policy)
- [Limitations](#limitations)
- [Development](#development)
- [Contributing](#contributing)
- [License](#license)

## What it is

- One flat, global alias table in `~/.hermes/config.yaml`, under `model_aliases:`.
- Aliases are not bound to a provider. The same slug resolves against each provider's
  own catalog, so it means "the newest matching model this provider serves".
- The plugin wraps the providers your config already references, so an alias id works
  wherever a (provider, model) pair appears: main model, auxiliary tasks, fallbacks,
  the `/model` picker, delegation.
- Standard library only. No third-party dependencies to install.

There is no `targets:` block, no `alias-*` profile, and no provider prefix in the
schema. One table, declared once, used everywhere.

## How it works

### 1. Provider wrapping

Hermes translates every model id through one seam, `normalize_model_for_provider()`
(`hermes_cli/model_normalize.py`), and asks the active provider profile's
`normalize_model_id()` first. This plugin makes the real provider answer that call.

At import (Hermes' loader contract: `register_provider()` at import time), the plugin
scans the config for every referenced provider:

- `model.provider`
- `auxiliary.<task>.provider`
- `fallback_providers[].provider`
- `delegation.provider`
- `smart_model_routing.cheap_model.provider`
- every entry of `custom_providers:` (list form) and `providers:` (dict form)

For each one, it registers a profile **under the real provider name**. The profile is
a deep copy of the original with three methods shadowed on the instance:

| Method | What the wrapper does |
| --- | --- |
| `normalize_model_id()` | Maps an alias to the resolved concrete id. Pure dict lookup, no I/O. Returns `None` for real ids, so Hermes' generic normalization handles them unchanged. |
| `fetch_models()` | Returns the provider's real model ids plus the alias ids, for the `/model` picker. Forces a live re-resolution first, and a failed refresh keeps the current snapshot. |
| `live_context_length()` | Returns the context window of the resolved target model. |

Because the profile keeps the real provider name, credential pools, `auth_type`, env
var ladders and registry sync keep working unchanged. Hermes sees the same provider it
saw before, with an alias table attached.

### 2. Per-provider resolution

Every provider is resolved on its own, against its own catalog:

1. **models.dev** (`https://models.dev/api.json`) is the primary catalog. It supplies
   release dates, context windows, tool support and modalities. One fetch per process,
   shared by all providers; a failed fetch is retried after 60 seconds.
2. **The provider's live `/models` endpoint** (`<base_url>/models`, or the provider's
   own `models_url`) is fetched when the cache is stale, when an alias is still
   unresolved, or when the picker forces a refresh. It is required for endpoints that
   are not on models.dev. The request carries the provider's `Authorization: Bearer`
   header when the provider exposes a key env var, and a descriptive `User-Agent`
   (models.dev and several provider endpoints reject the stock Python-urllib agent).
   The live fetch times out after 3 seconds at startup, and after 8 seconds when the
   picker forces a refresh.

Both sources merge by id, and models.dev entries win, because they carry the full
metadata. Live-only ids contribute a thin candidate (id plus context window, if the
endpoint reports one).

Resolution is per provider, so the same slug can resolve to different ids on different
providers: `glm-latest` resolves against whatever catalog the provider in play
actually serves.

### 3. Matching

A candidate matches when any of these forms matches any `match` pattern, and no
`exclude` pattern matches any of them:

| Form | Example |
| --- | --- |
| The raw model id | `glm-4.7` |
| Every `/`-suffix segment of it | `google/gemini-3-flash` also matches `gemini-*` |
| The provider-prefixed form, when a provider is in play | `opencode-go/glm-4.7` |

So `match: ["glm-*"]` catches `glm-4.7`, `z-ai/glm-4.7` and `opencode-go/glm-4.7`
alike, on every provider. A provider-prefixed pattern (`opencode-go/glm-*`) matches
the provider-prefixed form, so it keeps working for that provider while the bare
form would have matched it too. Patterns are shell globs (`fnmatch` syntax): `*`,
`?`, `[seq]`. An empty `match` never matches.

### 4. Filtering and selection

After matching, a candidate must pass every configured filter:

| Filter | Passes when | Unknown metadata |
| --- | --- | --- |
| `filter.minContext` | `context >= minContext` | Fails (an unknown context cannot be verified) |
| `filter.capabilities.tools` | `tool_call == the requested value` | Fails |
| `filter.capabilities.input` / `output` | every requested modality is present | Fails |
| `filter.minAge` | released on or before the cutoff | Passes (no evidence of recency) |

Boundaries are inclusive: a model with exactly `minContext` tokens passes, and a model
released exactly at the `minAge` cutoff passes.

**Selection.** Survivors are ranked by newest `release_date` first (undated ids sort
last), then by a natural version key descending (`glm-4.7` > `glm-4.6` > `glm-4`), then
by id descending. Date is primary so a same-version `-preview` released before its
base release loses to the base, while a preview of a genuinely newer version still
wins on its later date.

**Alias independence.** Every alias is resolved on its own against the same candidate
pool. An alias that matches nothing resolves to nothing and does not affect the
others.

## Requirements

- Hermes installed and importable (`hermes_cli`, `providers`, `hermes_constants`). The
  plugin is loaded by Hermes' own plugin loader.
- Python 3.11 (the interpreter in Hermes' virtualenv). The plugin uses only the
  standard library, so there are no third-party dependencies to install.
- Network access to `https://models.dev/api.json` and to each provider's `/models`
  endpoint, for cold starts. A warm cache needs no network at all.
- Write access to `$HERMES_HOME/cache/model-aliases/` (`~/.hermes/cache/model-aliases/`
  by default) for the resolution cache.

## Install

```bash
mkdir -p ~/.hermes/plugins/model-providers
git clone <this-repo> ~/.hermes/plugins/model-providers/model-aliases
```

or, for development, symlink the working copy (run this **from the repo directory**,
`$PWD` must be the repo, not your home folder):

```bash
mkdir -p ~/.hermes/plugins/model-providers
ln -s "$PWD" ~/.hermes/plugins/model-providers/model-aliases
```

Hermes discovers user plugins under `$HERMES_HOME/plugins/`, and a
`model-providers/<name>/` directory is loaded as a model-provider plugin.
Registration happens at import of the repository root `__init__.py`, so the wrapped
providers exist as soon as the plugin is loaded. Restart Hermes after installing or
after editing the config: the `model_aliases:` section is read at import.

## Configuration reference

Everything lives under the `model_aliases:` key in `~/.hermes/config.yaml`. Edit the
file directly: `hermes config set` does not know this section and warns about
unrecognized keys.

```yaml
model_aliases:
  wrap_all: false       # true: also wrap providers the config does not reference
  strict: false         # true: fail the plugin load instead of degrading
  debug: false          # log every resolution
  refresh_seconds: 3600 # how long a cached resolution is trusted

  glm-latest:
    match: ["glm-*"]            # required; glob against the model id
    exclude: ["*-preview"]      # optional; exclude always wins
    filter:
      minContext: 128000        # required context window
      minAge: 48h               # '<n>h' or '<n>d'
      capabilities:
        tools: true             # required tool support
        input: [text, image]    # every listed modality must be present
        output: [text]
    select:
      strategy: latest          # only 'latest' is supported
```

A ready-to-paste block is in [`examples/hermes-config.yaml`](examples/hermes-config.yaml).

### Top-level keys

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `wrap_all` | boolean | `false` | Also wrap every registered provider, not only the referenced ones. |
| `strict` | boolean | `false` | Turn any resolution failure into a plugin load failure. |
| `debug` | boolean | `false` | Log each resolution and the list of registered providers. |
| `refresh_seconds` | positive integer | `3600` | Age at which a cached resolution is considered stale. |

Every other top-level key is an alias slug. Unknown keys inside an alias spec are
rejected with the offending key path in the message, so a typo is logged at load and
skips registration instead of being ignored.

### `<alias>`

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

Pattern lists must not be empty, and every item must be a non-empty string. `select`
may be omitted; `latest` is the only strategy implemented.

Alias lookup is case-insensitive: `normalize_model_id("GLM-LATEST")` resolves the same
as `glm-latest`.

### `wrap_all`

`false` (default): wrap only the providers the config references. That keeps the
number of live catalog fetches small and the startup inside Hermes' 10 second plugin
load budget (`plugins.load_timeout_seconds`).

`true`: wrap every registered provider too, including ones the config never mentions.

Both modes resolve eagerly at import. There is no lazy path: `normalize_model_id()`
has to stay free of I/O, so the resolution happens once, at load, behind one shared
models.dev fetch, with the per-provider fetches running concurrently. The result is
held in memory for the life of the process.

### Custom providers

Entries in `custom_providers:` (list form) or `providers:` (dict form) are wrapped
like bundled providers. The plugin reads these keys from the block:

| Key | Meaning |
| --- | --- |
| `name` | The provider name. Required in the list form. |
| `api`, `base_url` or `url` | The endpoint. One of them is required. |
| `key_env` or `api_key_env` | Env var holding the bearer token. Used for the live `/models` fetch and as the profile's credential env var. |
| `transport` or `api_mode` | Wire protocol. `chat_completions` when unset. |
| `catalog_provider` | The models.dev provider id to read this endpoint's models from. |

A custom entry claims two profile names: `custom:<name>` and the bare `<name>`. The
bare name matters because Hermes' generic `custom` profile declares bare-name aliases
(`ollama`, `local`, `vllm`) that would otherwise intercept the lookup.

The plugin reads only the keys above, to build the wrapped profile and to fetch the
live catalog. Every other key of a provider block stays Hermes' business.

```yaml
providers:
  local:
    api: http://127.0.0.1:8000/v1
    transport: chat_completions
    key_env: LOCAL_API_KEY
    catalog_provider: ollama

model_aliases:
  fast:
    match: ["llama*"]

model:
  provider: local
  default: fast
```

## First 5 minutes

**1. Add the config.** Edit `~/.hermes/config.yaml`:

```yaml
model_aliases:
  debug: true
  glm-latest:
    match: ["glm-*"]
    exclude: ["*-preview"]
    filter:
      minContext: 128000
      capabilities:
        tools: true

model:
  provider: opencode-go
  default: glm-latest
```

`model.provider` stays the real provider name; the plugin wraps that provider, it does
not replace it. `model.default` is the alias you want to run.

**2. Restart Hermes.** The config is read at plugin import.

**3. Check the resolution.** With `debug: true` the plugin logs what it resolved and
what it registered:

```
model-aliases: opencode-go resolved {'glm-latest': 'glm-5.3-flash'}
model-aliases: registered ['opencode-go']
```

**4. Use it.** The alias works anywhere a model id is accepted:

```yaml
model:
  provider: opencode-go
  default: glm-latest

auxiliary:
  summarizer:
    provider: opencode-go
    model: glm-latest

fallback_providers:
  - provider: opencode-go
    model: glm-latest
```

In a session, `/model` shows the provider's real model ids and the alias ids.

`opencode-go` is one of the provider families whose generic normalization returns before
it asks the provider profile, so on that family the alias is translated in `/model` but
`model.default` keeps the literal id. See
[Provider families that ask the profile later](#provider-families-that-ask-the-profile-later).

**5. When the provider ships a new release.** The next startup re-resolves and the
alias moves with it. When the winner changes, the plugin logs one drift line:

```
model-aliases: opencode-go/glm-latest: glm-5.2 -> glm-5.3-flash (detected 2026-11-24T09:14:22)
```

The drift is also recorded in the cache file, see [Cache and drift](#cache-and-drift).

## Support matrix

Where an alias id can be used, verified against the Hermes call sites:

| Config surface | Resolves | Notes |
| --- | --- | --- |
| `model.default` | yes | Normalized at agent init, see the provider families below. |
| `auxiliary.<task>.model`, including `vision` | yes | The auxiliary builder normalizes the model for the task's provider. |
| `fallback_providers[].model` | yes | Normalized per fallback entry. |
| `/model` picker | yes | The picker lists real ids plus alias ids. |
| `delegation.model` | yes | Delegated children normalize the same way. |
| `custom_providers[].model` | yes | Custom entries are wrapped like bundled providers, so their default model id is normalized too. |
| `x_search.model` | no | The x_search tool calls xAI directly with the literal id, outside the provider profile seam. Use a concrete xAI model id there. |

### Provider families that ask the profile later

The plugin always answers on the provider profile: on every provider it wraps,
`normalize_model_id()` returns the resolved id and `fetch_models()` lists the real ids
plus the alias ids.

Hermes' generic normalization, `normalize_model_for_provider()`, asks that profile for
most providers, but a few families return before they do:

| Family | What `normalize_model_for_provider()` returns |
| --- | --- |
| `openrouter`, `nous`, `ai-gateway`, `kilocode` | The vendor-prefixed id (`z-ai/glm-latest`). |
| `opencode-go`, `opencode-zen` | The name unchanged. (A custom provider whose name starts with a family slug — e.g. `opencode-go-bridge` — early-returns the same way.) |
| `anthropic` | The name with dots replaced by hyphens. |

On those providers the alias id is still translated when you pick it in `/model`, because
the picker calls the profile directly. The paths that go through the generic
normalization (`model.default`, the auxiliary tasks, the fallbacks) need the **neutral
alias name** instead of the real provider name — every wrap also answers to
`alias-<provider>`, which dodges all three early-return lists:

```yaml
model:
  provider: alias-opencode-go   # NOT opencode-go — early-return family
  default: glm-latest           # resolves on the request path

auxiliary.anything:
  provider: alias-opencode-go
  model: glm-latest
```

On families that do ask the profile (gemini, groq, bedrock, custom endpoints, …) the
real provider name works directly.

### Live example

Against a real `opencode-go` catalog, a table of seven aliases resolves like this:

```
model-aliases: opencode-go resolved {'glm-latest': 'glm-5.3-flash', 'glm-flash-latest': 'glm-5.3-flash', 'kimi-code-latest': 'kimi-k2.7-code', 'mimo-pro-latest': 'mimo-v2.6-pro', 'mimo-flash-latest': 'mimo-v2.6-flash', 'minimax-latest': 'minimax-m3.1-flash-free', 'minimax-flash-latest': 'minimax-m3.1-flash'}
```

The `/model` picker for that provider shows the 50 real ids it serves plus the alias
ids on top.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `model-aliases: provider 'opencode-go': alias(es) 'glm-latest' matched no models (check match globs against the catalog)` | No candidate matched `match`, or every candidate was excluded or filtered out. | Turn on `debug: true` and check the resolved map. Widen `match`, drop an over-strict `exclude`, or relax `filter` (unknown context or tool support fails a requirement). |
| `model-aliases: alias 'glm-latest' matched no models on 'opencode-go'` | The alias is known but resolved to nothing on this provider. Logged once per (provider, alias) pair, the first time the alias is looked up there. | Same as above: the glob matches nothing this provider serves. |
| The alias resolves but the agent reports an unknown model | The resolved id is not served by the provider, for example a catalog entry the endpoint does not implement. | Narrow `match` to the ids the endpoint really serves, or point `catalog_provider` at the right models.dev entry. |
| The alias points at an old model after the provider released a new one | The cached resolution is younger than `refresh_seconds`. | Lower `refresh_seconds`, delete `~/.hermes/cache/model-aliases/opencode-go.json`, or open the `/model` picker, which forces a live refresh. |
| Plugin does not load and Hermes reports an import error | `strict: true` and something failed: an unknown provider, or an unresolvable alias. | Read the message, it names the provider and the alias. Fix the cause, or set `strict: false` to degrade instead. |
| `model-aliases: invalid config: <path>: <message>` | A typo or a wrong type in the `model_aliases:` section. | Fix the key path named in the message. Nothing is registered until it is valid. |
| `unknown provider 'x' (not bundled, not under custom_providers/providers:)` | The provider name is neither a bundled provider nor a block under `custom_providers:` or `providers:`. | Add the block with an endpoint and reference it by that name. |
| The provider receives the alias id itself (`glm-latest`) instead of the resolved model | The provider family returns before Hermes asks the provider profile (aggregators, `opencode-*`, `anthropic`). | Use the neutral `alias-<provider>` name in that block (`provider: alias-opencode-go`), or pick the alias in `/model`. See [Provider families that ask the profile later](#provider-families-that-ask-the-profile-later). |
| The new alias is ignored | The config is read at plugin import only. | Restart Hermes after editing `model_aliases:`. |
| `hermes doctor` does not show the provider as configured | The provider exposes no key env var (`key_env` unset, or the bundled provider has no non-URL env var). | Set `key_env` on the provider block. The wrapper carries the provider's own credential env var into the wrapped profile, so nothing else changes. |

## Cache and drift

The plugin writes one JSON file per provider under `$HERMES_HOME/cache/model-aliases/`
(`~/.hermes/cache/model-aliases/` by default), named after the provider, for example
`opencode-go.json`:

```json
{
  "version": 1,
  "updated_at": 1764000000.0,
  "resolved": {
    "glm-latest": "glm-5.3-flash"
  },
  "history": [
    {"alias": "glm-latest", "from": "glm-5.2", "to": "glm-5.3-flash", "at": 1763990000.0}
  ],
  "capabilities": {
    "glm-latest": {"id": "glm-5.3-flash", "release_date": "2026-08-26", "limit": {"context": 1000000}}
  },
  "real_ids": ["glm-5.2", "glm-5.3-flash"]
}
```

- `resolved` is the current alias to model id mapping for that provider.
- `history` is the drift log, oldest first, capped at 20 entries.
- `capabilities` caches the models.dev entry of each resolved model, so warm starts keep
  picker badges and routing metadata without a network call.
- `real_ids` is the provider's real model id list, used to populate the picker.
- `updated_at` is the epoch timestamp of the last resolution and drives the
  `refresh_seconds` check.
- A file with an unexpected `version` or malformed JSON is treated as "no cache", so the
  next start resolves from scratch.

**Warm cache.** A cached resolution is reused without any network call when it is
younger than `refresh_seconds` and it resolves every configured alias. Otherwise the
plugin re-resolves.

**Last-known-good.** If a re-resolution fails (network error, endpoint down), the
previous mapping is kept and used. An alias that has never resolved is reported as an
error instead.

**Drift.** When a re-resolution picks a different model for an alias, the change is
logged and recorded in the cache history. The same alias can drift at most once per
resolution.

To force a re-resolution, delete the cache file, or lower `refresh_seconds` and wait for
it to expire. The `/model` picker also forces a live refresh, and a failed refresh never
wipes a good snapshot.

To see the drift lines, read the Hermes logs (`hermes logs`, i.e.
`~/.hermes/logs/agent.log`) and look for the `model-aliases` logger.

## Failure policy

- **Tolerant (default).** A provider that cannot be described, or an alias that cannot
  be resolved on it, is logged as an error. The other providers still register, and a
  previously resolved alias keeps its last-known-good value.
- **Strict (`strict: true`).** Any of those failures raises an error during plugin load.
  Nothing is registered, and Hermes reports the failed import.
- **Config errors** (unknown key, wrong type, empty pattern list) are always logged and
  skip registration entirely, in both modes. The message names the exact key path, for
  example `model-aliases: invalid config: model_aliases.glm-latest.filter.minContext: expected a positive integer`.

## Limitations

- There is deliberately no `pin` option. A pinned alias is just a model id, and
  `model.default` already covers that without this plugin. A pin would also freeze the
  one thing this plugin exists to keep fluid.
- `x_search.model` is not normalized, see [Support matrix](#support-matrix).
- On the provider families listed in
  [Provider families that ask the profile later](#provider-families-that-ask-the-profile-later),
  `model.default`, the auxiliary tasks and the fallbacks need the neutral
  `alias-<provider>` name (the real name keeps the literal alias id; `/model` resolves it).
- The neutral `alias-<provider>` name forfeits the family's real-id repairs: under
  `alias-anthropic` a dotted id (`claude-3.5-sonnet`) is no longer hyphenated, and under
  `alias-opencode-go` a vendor-prefixed id (`z-ai/glm-5.3-flash`) is no longer stripped.
  Use the real provider name for those concrete ids.
- A provider block that fetches its key with `key_cmd` gives this plugin no env var to
  use. Declare `key_env` on the block instead.
- Alias display names are not customizable. The picker shows the alias id.
- Live-only model ids carry no release date, so they sort behind dated catalog models.
- The config is read at plugin import. Restart Hermes after changing it.

## Development

```bash
python3 -m pytest tests/ -q                                   # 114 unit tests, no Hermes needed
MODEL_ALIASES_SMOKE_HOME=$(mktemp -d) ~/.hermes/hermes-agent/venv/bin/python tests/integration_smoke.py
```

The smoke test loads the plugin exactly the way Hermes does (real loader, real provider
registry, isolated `HERMES_HOME`, no network) and asserts that the wrapped profile
registers under the real provider name, that an alias resolves through it, that concrete
ids pass through untouched, that the picker catalog holds real ids plus alias ids, that a
`custom_providers` entry claims its `custom:<name>` alias, and that strict mode fails the
load loudly.

Layout:

- `model_aliases/config.py` parses the `model_aliases:` section into typed dataclasses and
  rejects unknown keys. Pure.
- `model_aliases/resolver.py` matches, filters and selects. Pure.
- `model_aliases/catalog.py` sources candidates from models.dev and live `/models`. Owns
  all network access, behind the `_fetch_json` seam so tests can stub it.
- `model_aliases/state.py` owns the on-disk cache and drift history. Pure.
- `model_aliases/plugin.py` is the Hermes seam: config loading, provider discovery,
  resolution, wrapping, registration.
- `model_aliases/profile.py` wraps a provider profile and builds the `ProviderProfile` of
  a custom entry.

## Contributing

Bugs and pull requests are welcome.

- **Reporting a bug.** Include your Hermes version, the relevant part of your
  `model_aliases:` section (redact keys and URLs), the plugin log lines (`hermes logs`,
  logger `model-aliases`), and the resolved map from the cache file if it exists. A
  failing `match` glob against a known catalog is the most useful thing to include.
- **Submitting a pull request.** Branch from `main`, keep the change focused, and make
  sure both test commands pass before opening it. New behavior needs a unit test; changes
  to the Hermes seam need the smoke test updated too.
- **Code standards.** `config.py`, `resolver.py` and `state.py` stay pure: no network, no
  filesystem, no Hermes imports. `catalog.py` owns all network access (models.dev and live
  `/models`) behind the `_fetch_json` seam so tests can stub it. The Hermes seams stay in
  `plugin.py` and `profile.py`. Keep `normalize_model_id()` free of I/O, it is on the hot
  path. Use type hints and a module docstring that states the contract, keep error
  messages actionable (they name the offending key path), and follow the existing
  frozen-dataclass and pure-function style.

## License

[MIT](LICENSE) - Copyright (c) 2026 Paul Chason.

# jsonhub

`jsonhub` is the command line interface for [JsonHub](https://jsonhub.cloud) —
what `gh` is to GitHub. It stores JSON documents ("entities"), validates them
against JSON Schemas ("definitions"), and this CLI drives that from a terminal
or a CI job.

It is a thin, opinionated layer over the official
[`jsonhub-sdk`](https://pypi.org/project/jsonhub-sdk/).

## Install

```bash
uv tool install jsonhub-cli      # or: pipx install jsonhub-cli
```

From a checkout:

```bash
uv sync
uv run jsonhub --help
```

## Log in

```bash
jsonhub auth login                # opens a browser: OAuth 2.0 + PKCE
jsonhub auth status
```

For CI, use a personal access token instead of a browser:

```bash
jsonhub token create ci-deploy --expires 90d --json | jq -r .token
echo "$JSONHUB_PAT" | jsonhub auth login --with-token
```

Or skip stored credentials entirely and set `JSONHUB_TOKEN`.

## Use

Reading needs no credentials — public entities and definitions are readable
anonymously.

```bash
jsonhub entity list --limit 20
jsonhub entity list --definition base-v1 --owned
jsonhub entity get my-slug
jsonhub entity get my-slug --data-only > document.json

jsonhub entity create --definition base-v1 --slug my-slug --data @document.json
jsonhub entity create --definition base-v1 --field name=ada --field count=3
jsonhub entity edit my-slug --field name=grace
jsonhub entity edit my-slug                 # opens $EDITOR on the document
jsonhub entity delete my-slug

jsonhub definition list
jsonhub definition get base-v1 --schema-only
jsonhub definition create --slug person-v1 --data @schema.json

jsonhub me                                  # quota usage
jsonhub token list
```

Entities and definitions can be addressed however you have them — a UUID, a
slug, an IRI (`/api/entities/<uuid>`), or a URL pasted from the web app.

### JSON payloads

`--data` takes inline JSON, `@file.json`, or `-` for stdin. `--field key=value`
sets one key at a time; values that parse as JSON keep their type, dots nest,
and fields override `--data`:

```bash
jsonhub entity create -d @base.json -f name=ada -f meta.tags='["x","y"]'
```

### Scripting

`--json` prints the API's own JSON on stdout and nothing else. Without it,
piped output is tab-separated with no header, so it composes with `cut` and
`awk`:

```bash
jsonhub entity list --limit 100 --json | jq -r '.[] | "\(.slug)\t\(.id)"'
jsonhub definition list | cut -f2
```

`--limit` counts resources, not requests: `--limit 500` pages until it has 500
items. Pass `--page N` to fetch exactly one page of `--limit` items.

Diagnostics, prompts and warnings always go to stderr, so stdout stays clean.

Exit codes:

| Code | Meaning |
| ---- | ------- |
| 0    | success |
| 1    | generic failure |
| 2    | bad input, or an ambiguous slug |
| 3    | not found |
| 4    | authentication required or rejected |
| 22   | the API rejected the payload (422) |

## Configuration

| Variable | Purpose |
| -------- | ------- |
| `JSONHUB_HOST` | default deployment, same as `--host` |
| `JSONHUB_TOKEN` | access token, overriding stored credentials |
| `JSONHUB_CONFIG_DIR` | where `config.json` lives (default `~/.config/jsonhub`) |

Credentials live in `~/.config/jsonhub/config.json`, written `0600`. Multiple
deployments are supported:

```bash
jsonhub config set-host jsonhub.internal --base-url https://api.jsonhub.internal
jsonhub auth login --host jsonhub.internal
jsonhub --host jsonhub.internal entity list
jsonhub config set-default jsonhub.internal
```

## Develop

```bash
uv sync
uv run pytest          # no network: the API is mocked with pytest-httpx
uv run ruff check .
uv run ruff format .
uv run mypy
```

Tests drive the CLI through `jsonhub_cli.main.run`, the same error boundary the
installed script uses, so exit codes and stderr messages are covered too.

## Licence

MIT.

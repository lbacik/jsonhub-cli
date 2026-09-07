# jsonhub-cli

`jsonhub`, the command line interface for JsonHub — `gh` for JsonHub. A thin
layer over the generated [`jsonhub-sdk`](https://pypi.org/project/jsonhub-sdk/).

## Commands

```bash
uv sync
uv run jsonhub --help
uv run pytest          # no network; the API is mocked with pytest-httpx
uv run ruff check .
uv run ruff format .
uv run mypy            # strict, and expected to stay clean
```

## Layout

Source is `src/jsonhub_cli/`, one module per concern:

| Module | Role |
| ------ | ---- |
| `main.py` | root Typer app; `run()` is the error boundary that turns exceptions into stderr lines and exit codes |
| `api.py` | builds SDK clients; `check()`/`payload()` convert SDK response unions into dicts or typed exceptions |
| `config.py` | `~/.config/jsonhub/config.json`, env-var layering, `0600` writes |
| `oauth.py` | authorization code + PKCE flow over a loopback redirect |
| `collection.py` | HAL collection fetch, working around an SDK defect (see below) |
| `hal.py`, `paging.py` | unwrap HAL envelopes; auto-paginate to a resource count |
| `refs.py` | resolve a UUID / slug / IRI / pasted URL to an id, and read HAL relations |
| `jsonarg.py` | `--data` / `--field` / `$EDITOR` payload input |
| `output.py` | tables for a TTY, TSV when piped, `--json` for the raw API body |
| `commands/` | one module per top-level noun; shared option types in `_shared.py` |

## Conventions that matter

**The CLI's internal currency is the API's own JSON.** Typed SDK models are
flattened with `to_dict()` at the boundary in `api.payload()`. That keeps
`--json` byte-comparable with the HTTP response and lets one set of renderers
handle both single resources and collection members. Keys stay camelCase.

**stdout is for results, stderr is for everything else.** Notes, prompts,
warnings and errors go through `output.err`. With `--json`, stdout carries
exactly one JSON document. When stdout is not a TTY, tables become
tab-separated with no header.

**Commands do not catch exceptions.** They raise from `jsonhub_cli.errors`;
`main.run()` renders them. Add a new failure mode by adding an exception class
with an `exit_code`, not a `try` block in a command.

**Accept must be exactly `application/hal+json`.** The API is content-negotiated
(API Platform): it answers JSON-LD by default and a bare array for
`application/json`, and only returns the paginated HAL envelope — the one the
generated collection models require — for that exact media type. Set in
`api._client_kwargs`.

**`--limit` counts resources, not requests.** `paging.collect` pages until it
has that many. The page size must stay constant across the traversal: page
numbers are offsets in units of the page size, so shrinking it for the final
request re-offsets the pages and returns duplicates.

## Known SDK/API mismatches

Worked around here; remove the workaround if the SDK is fixed upstream
([lbacik/jsonhub-sdk-python](https://github.com/lbacik/jsonhub-sdk-python)).

1. **Empty collections crash the generated models.** The API omits `_embedded`
   entirely when a collection has no members, but the collection models declare
   it required and read it with `d.pop("_embedded")`. Every empty result would
   raise `KeyError: '_embedded'`. Handled in `collection.fetch`.

2. **`private` leaks into every entity merge patch.**
   `EntityEntityUpdateJsonMergePatch.private` defaults to `False` rather than
   `UNSET`, so an untouched body serialises `"private": false` and would quietly
   publish a private entity on any edit. `commands/entity.py` starts from
   `private=UNSET`.

3. **`/api/users/me` reports quota, not identity.** There is no "who am I"
   endpoint, so `auth login`/`auth status` can only report whether the server
   accepts the token. `auth._verify` calls it through the raw httpx client so a
   quota payload that does not match the schema cannot break a login check.

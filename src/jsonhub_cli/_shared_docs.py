"""Long-form help text kept out of the code that uses it.

Rendered by Typer in Markdown mode, so fenced blocks stay verbatim instead of
being reflowed to the terminal width.
"""

EPILOG = """\
**Examples**

```
jsonhub auth login
jsonhub entity list --limit 5
jsonhub entity get my-slug --data-only
jsonhub entity create -D base-v1 -d @document.json
jsonhub definition list --owned --json | jq -r '.[].slug'
```

**Environment**

```
JSONHUB_HOST        default deployment, same as --host
JSONHUB_TOKEN       access token, overriding stored credentials
JSONHUB_CONFIG_DIR  where config.json lives (default ~/.config/jsonhub)
```
"""

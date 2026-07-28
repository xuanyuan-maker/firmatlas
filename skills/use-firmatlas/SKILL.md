---
name: use-firmatlas
description: Operate the FirmAtlas CLI to discover, search, inspect, and download IoT firmware. Use when a user asks to find firmware by vendor, source, region, device type, model, hardware revision, or version; inspect crawl or download history; authenticate a source; or download selected firmware.
---

# Use FirmAtlas

Run commands from the FirmAtlas repository. Prefer `uv run firmatlas` so the project environment is
used consistently.

## Follow the Workflow

### 1. Prepare the Catalog

Install dependencies and initialize the data directory:

```bash
uv sync --dev
uv run firmatlas --data-dir data init
uv run firmatlas --data-dir data sources
```

Treat `init` as idempotent. Use `sources` instead of guessing supported source keys.

### 2. Collect Metadata

Run one source at a time:

```bash
uv run firmatlas --data-dir data crawl <source-key>
uv run firmatlas --data-dir data runs --source <source-key>
```

`crawl` collects metadata only; it does not download firmware. Report incomplete runs and their
issues instead of treating partial results as complete.

For `ruijie-cn`, ask the user to obtain the token in their authenticated browser. Never request that
they paste it into chat or place it directly in shell history. Let the user save and check it with
hidden input:

```zsh
read -rs "FIRMATLAS_RUIJIE_TOKEN?Token: "; echo
uv run firmatlas --data-dir data auth ruijie-cn --save "$FIRMATLAS_RUIJIE_TOKEN"
unset FIRMATLAS_RUIJIE_TOKEN
uv run firmatlas --data-dir data auth ruijie-cn --check
```

### 3. Search and Inspect

Prefer JSON when another program or AI will consume the result:

```bash
uv run firmatlas --data-dir data list --model <model> --format json
uv run firmatlas --data-dir data list --source <source-key> --type <type> --format json
uv run firmatlas --data-dir data show <release-id> --format json
```

Combine filters when useful: `--vendor`, `--source`, `--region`, `--family`, `--type`, `--series`,
`--model`, `--hardware`, `--version`, `--visibility`, `--download-status`, and
`--verification-status`. Use `list --help` to confirm enum values. Use `--limit` and `--offset` for
pagination.

Accept only an unambiguous release ID prefix. Use `show` before downloading when a release contains
multiple artifacts.

### 4. Download Only on Request

Download only after the user selects or explicitly requests the firmware:

```bash
uv run firmatlas --data-dir data download <release-id-or-artifact-id>
uv run firmatlas --data-dir data downloads
```

A release ID works directly when it has one artifact. When it has several, select an Artifact ID
from `show`. Unambiguous prefixes are accepted. FirmAtlas streams the file, verifies available
official checksums, computes SHA-256, and archives it atomically.

Treat `pending:<file_id>` URLs from `ruijie-cn` as expected; download refreshes the temporary URL.
Report the final archive path, verification result, and SHA-256.

## Configure and Troubleshoot

- Put global options before the subcommand:
  `firmatlas --config <file> --data-dir <dir> --verbose <command>`.
- Inspect effective settings with
  `uv run firmatlas --config <file> --data-dir <dir> config`.
- Run `uv run firmatlas --help` or `uv run firmatlas <command> --help` before inventing syntax.
- Resolve an unknown source with `sources`.
- Resolve an ambiguous ID by supplying a longer prefix; use `show` to choose among artifacts.
- Inspect failed collections with `runs` and failed downloads with `downloads`.

If stale proxy variables break a source, remove them for that command only:

```bash
env -u all_proxy -u http_proxy -u https_proxy \
  -u ALL_PROXY -u HTTP_PROXY -u HTTPS_PROXY \
  uv run firmatlas --data-dir data crawl <source-key>
```

## Protect User Data

- Never commit or publish `data/`, firmware files, tokens, cookies, or vendor responses.
- Never expose authentication values in chat, logs, command output, or shell history.
- Keep metadata collection separate from firmware download.
- Do not delete catalog or archived firmware unless the user explicitly requests it.

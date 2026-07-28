---
name: firmatlas-development
description: Develop, review, test, and document FirmAtlas changes. Use for adapters, crawl and download workflows, domain models, repositories, CLI commands, fixtures, source registration, or other work in the FirmAtlas repository.
---

# FirmAtlas Development

## Prepare

1. Read `AGENTS.md` and the files relevant to the request.
2. Inspect `git status` and preserve unrelated user changes.
3. Explain the task, data flow, files to change, and small verification steps in Chinese.
4. Wait for confirmation before editing code or interfaces.

Keep implementation and documentation concise. Do not add dependencies or abstraction layers unless
the user explicitly approves them.

## Respect the Architecture

- Keep `domain` independent of `infra`.
- Put use cases and ports in `app`.
- Inject only `HttpFetcher` into adapters. Never access repositories, artifact stores, downloaders,
  or private HTTP clients from adapters.
- Use SQLAlchemy Core in `infra`; never expose SQLAlchemy objects, SQL, or SQLite errors upward.
- Keep domain entities and candidates as frozen dataclasses.

Follow this data flow:

`Adapter -> nested Candidate events -> crawl use case -> Repository -> flat domain entities`

## Implement Adapters

Use this package shape:

```text
src/firmatlas/adapters/<vendor>_<region>/
├── classification.py
├── <parser>.py
└── adapter.py
```

- Make classification and parsing pure functions using only the standard library.
- Set `source_key` to kebab-case and the package name to snake_case.
- Yield `DiscoveredProduct` or `SkippedCandidate` one at a time.
- Yield `DiscoveryCompleted` as the final discovery event.
- Stop API pagination when `page * page_size >= total`.
- Use `UNSPECIFIED_REVISION_SOURCE_KEY` when hardware revision data is absent.
- Validate all three identity levels before returning a refreshed artifact URL.

Register a new adapter in `src/firmatlas/app/registry.py`: import its class, seed its
`FirmwareSource`, and add its builder mapping.

## Test and Verify

Use sanitized fixtures under `tests/fixtures/<source-key>/`; never contact live vendor sites from
tests. Cover the normal path, boundaries, failure states, non-target skips, stable source keys, and
URL refresh when applicable.

Run the narrowest checks first, then expand:

```bash
uv run pytest tests/test_<topic>.py -q
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build
```

Report skipped checks and their reason.

## Protect Data and Commit

- Never commit firmware, `data/`, secrets, cookies, or unsanitized responses.
- Keep regions as separate sources and mark missing records `disappeared`; never hard-delete them.
- Stage only files belonging to the completed change.
- Commit each completed feature separately with a Chinese message in `topic: specific change` form.
- Finish by explaining the execution order, call relationships, error handling, and key concepts in
  Chinese.

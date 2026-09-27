# Contributing

Use Python 3.12+, `uv` and Node.js 24:

```bash
uv sync --locked
npm ci --prefix web
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run pytest
npm run typecheck --prefix web
npm test --prefix web
```

Changes to input handling, window targets or the protocol should also pass `uv run python scripts/e2e.py` in a desktop session; it only types into the isolated Kitty instance it starts.

Keep the scope focused on Linux + Kitty. The web app, relay and agent share one protocol version, so update all three together when messages change. Never include real credentials, relay addresses, personal paths, terminal content or screenshots of real sessions in commits or fixtures.

Open an issue before a large behavior or protocol change. Pull requests should explain the user-visible change, security implications, and verification performed.

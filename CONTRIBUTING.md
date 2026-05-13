# Contributing

Thanks for considering a contribution. Quick orientation:

## What's most welcome

- **New collectors.** A new RSS source registry is a YAML edit, not code. But if
  you want a whole new platform (Mastodon, Bluesky, Discord channels with custom
  filtering, etc.), open an issue first to discuss the contract.
- **Niche tuning.** The heat formula, cluster threshold, and topic-diversity
  gate are all tuned for the homelab/self-host/AI/virt audience. PRs that make
  these configurable per niche (instead of hardcoded constants) are great.
- **Eval cases.** More golden cases in `tests/golden/<agent>/` strengthen the
  regression net every model swap relies on.
- **Bug fixes.** Especially ones that include a golden test case that fails
  before the fix and passes after.

## What needs design discussion first

- New AI agents in the synthesis path.
- Schema changes (`schemas.py`).
- Anything that adds a hard dependency on a hosted service.
- Anything that changes how `channel.yaml` is interpreted.

## Setup

```bash
./setup.sh
.venv/bin/pip install -e ".[dev]"
pre-commit install                  # gitleaks + secret scanning
```

## Tests

```bash
.venv/bin/engine eval               # golden cases
.venv/bin/pytest                    # unit tests (when there are some)
```

## Code style

- Standard Python, no clever metaclass stuff.
- Type hints on public function signatures.
- Pydantic v2 schemas for any agent I/O.
- Prefer plain-text `{{var}}` substitution over Jinja in agent prompts —
  prompts should be `grep`-able.

## Hygiene before pushing

```bash
gitleaks detect --no-banner         # no secrets
git check-ignore .env channel.yaml  # both should print themselves
.venv/bin/engine eval               # green
```

## Commit messages

Short imperative-mood subject, then a paragraph or two explaining *why*. Co-author
trailers for AI-assisted commits encouraged.

## License

By contributing, you agree your contribution is licensed under the MIT License
(see `LICENSE`).

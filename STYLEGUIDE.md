# novelhog styleguide

One styleguide for the whole project. Later plans follow it and extend it here.

## Python

- Python 3.14, managed with `uv`; `ruff format` + `ruff check`; `mypy --strict`; `pytest`.
- `snake_case` for modules, functions and variables; `PascalCase` for classes.
- Tests are named `test_<unit>__<behaviour>`, e.g. `test_claim_batch__skips_claimed_novel`.
- No bare `except:`.
- Every module starts with a module docstring.
- Public functions are fully typed.
- Domain errors (`NotFound`, `Conflict`, `ValidationFailed`) are raised in `domain` and `repo`
  and mapped to HTTP responses only in `api`.
- Line length 100.

## TypeScript

- Strict mode; `eslint` + `prettier`.
- Components are `PascalCase.tsx`, hooks are `useX.ts`, tests are colocated as `*.test.tsx`.

## Layout

```
src/novelhog/{api,worker,domain,repo,scraper_adapter,storage,exporter,notifications,
              html_to_markdown,image_fetcher,images,chapter_store,vapid.py,ids.py,
              config.py,cli.py}
migrations/        (Alembic: env.py, versions/)
web/src/{routes,components,api,hooks}   web/public/{sw.js,manifest.webmanifest}
tests/{unit,integration,api,fixtures}
docker/{Dockerfile,Caddyfile}   compose.yaml   STYLEGUIDE.md   docs/superpowers/specs/
```

## Tests

- One test per BDD effect, grouped in classes named after the scenario, for example
  `class TestClaimBatch__OnClaimedNovel`.
- Fixtures live in `tests/conftest.py`.
- Integration tests use a real temporary database (SQLite by default, PostgreSQL via
  `NOVELHOG_TEST_DATABASE_URL`). They never mock `repo`.

## Import boundaries

Enforced by import-linter (`lint-imports`, contracts in `pyproject.toml`):
`api-is-pure`, `repo-is-standalone`, `scraper-is-isolated`.

## Quality gates

`scripts/check.sh` runs `ruff format --check`, `ruff check`, `mypy --strict src tests`,
`lint-imports` and `pytest`, and exits non-zero at the first failing gate.

## Commits

`type: summary`, where `type` is one of `feat`, `fix`, `test`, `docs`, `chore`, `refactor`.

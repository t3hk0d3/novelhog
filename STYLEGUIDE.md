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
- Error codes are `snake_case`.
- Wire timestamps are RFC 3339 UTC with milliseconds and `Z`.
- `repo` never logs: it returns reports and raises domain errors.
- No public `repo` method takes a `Connection`; transactions are composed inside `repo`.
- Line length 100.

## TypeScript

- Strict mode; `eslint` + `prettier`.
- Components are `PascalCase.tsx`, hooks are `useX.ts`, tests are colocated as `*.test.tsx`
  (`*.test.ts` for files without JSX).
- Query keys: `['novels']`, `['novel', id]`, `['chapters', novelId]`, `['exports', novelId]`,
  `['settings']`.

## Layout

```
src/novelhog/{api,worker,domain,repo,scraper_adapter,exporter,notifications,
              storage.py,html_to_markdown.py,image_fetcher.py,images.py,net.py,
              chapter_store.py,vapid.py,ids.py,config.py,log.py,cli.py}
src/novelhog/repo/migrations/   (Alembic: env.py, versions/)
web/src/{routes,components,api,hooks,theme}   web/public/{sw.js,manifest.webmanifest,icons}
tests/{unit,integration/{repo,worker,notifications},api,container,fixtures}
tools/             (repository tooling, e.g. import-linter contract types)
docker/{Dockerfile,Caddyfile}   compose.yaml   compose.test.yaml   STYLEGUIDE.md
docs/superpowers/specs/
```

## Tests

- Every test directory has an `__init__.py` (module names repeat across `unit`, `integration`
  and `api`).
- One test per BDD effect, grouped in classes named after the scenario, for example
  `class TestClaimBatch__OnClaimedNovel`.
- Fixtures live in `tests/conftest.py`.
- Integration tests use a real temporary database (SQLite by default, PostgreSQL via
  `NOVELHOG_TEST_DATABASE_URL`). They never mock `repo`.

## Import boundaries

Enforced by import-linter (`lint-imports`, contracts in `pyproject.toml`):
`api-is-pure`, `repo-is-standalone`, `scraper-is-isolated`. The custom `allowed_imports`
contract (`tools/importlinter_contracts.py`) checks direct imports only.

## Quality gates

`scripts/check.sh` runs `ruff format --check`, `ruff check`, `mypy --strict src tests tools`,
`lint-imports` and `pytest`, and exits non-zero at the first failing gate.

## Commits

`type: summary`, where `type` is one of `feat`, `fix`, `test`, `docs`, `chore`, `refactor`.

# Implementation Plan: Testing Resources Rationalization

**Branch**: `003-testing-resources-refactor` | **Date**: 2026-08-18 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/003-testing-resources-refactor/spec.md`

## Summary

Reorganize the kazoo testing framework so that `kazoo.testing` owns everything a harness run needs:

1. **Move framework resources** — the six `docker-compose.*.yml` overlays, `jaas/`, and `dockerfiles/` trees — from `kazoo/tests/integ/` into the `kazoo.testing` package, resolving the compose context via `importlib.resources.files("kazoo.testing")`.
2. **Delete** the fully commented-out `kazoo/tests/conftest.py` and its references (mypy override list).
3. **Split** `kazoo/testing/kazoo_ensemble.py` into `kazoo.testing.common` (all business logic, pytest/Docker-independent) and `kazoo.testing.fixtures` (thin, documented pytest fixtures + plugin hooks delegating to `common`).
4. **Clean** comments/docstrings in every file under `kazoo.testing` and `kazoo/tests`: remove Speckit plan references (US/FR/R/SC ids, "the plan", quickstart) and historical narrative ("used to X", "formerly", "legacy ... was removed"); document only current behavior.
5. **Unit-test** the extracted logic to **100% branch coverage of every pure function** in `kazoo.testing.common` via a new `kazoo/tests/unit/test_testing.py` that runs without Docker.

Behavioral parity is the cardinal rule: marker/skip decisions, fixture names, the `--zk-*` CLI surface, and integration-test outcomes must be byte-identical after the refactor (FR-011, SC-001).

## Technical Context

**Language/Version**: Python ≥ 3.9 (CPython and PyPy; `StrEnum` via `backports.strenum` on < 3.11, already used in the codebase).

**Primary Dependencies**: `attrs` (frozen data classes), `pytest`, `testcontainers>=4,<5` (`testcontainers.compose.DockerCompose`), `importlib.resources` (`resources.files`), `packaging` (`SpecifierSet`, `Version`). All already dependencies of the harness.

**Storage**: N/A (no persistent data). The relocated resources ship as package data — `setup.cfg` `include_package_data = true` plus `MANIFEST.in` `recursive-include kazoo *` already covers `kazoo/testing/`; verified for wheel and sdist (FR-004).

**Testing**: `pytest` for the suite; `pytest-cov` (branch mode) to prove 100% branch coverage of `common.py` pure functions (FR-010, SC-005); existing gates `flake8`, `black`, strict `mypy` per constitution V (SC-007).

**Target Platform**: Linux (CI) and macOS locally; `windows-latest` runs the integration suite against a WSL2-hosted dockerd, so `_daemon_mount_path` host→`/mnt/<drive>` translation for `ZK_WORK_DIR`/`ZK_COMPOSE_DIR` must keep working for the relocated context.

**Project Type**: library (`kazoo`) plus an internal pytest test harness (`kazoo.testing`) — this feature re-organizes the harness.

**Performance Goals**: The new unit suite must run in seconds with no Docker engine available (SC-005); no runtime/performance change is introduced to the integration suite.

**Constraints**:
- 100% branch coverage of every pure function in `kazoo.testing.common` (clarification Q1).
- Byte-identical marker registration, collection-time skip decisions, and integration outcomes vs. the current branch (FR-011).
- `kazoo_ensemble.py` and `kazoo/tests/conftest.py` removed; zero references that break imports, mypy, or the docs build (SC-006).
- Resources resolvable from an *installed* package (wheel + sdist), not a source-tree-relative path (FR-002, FR-004).
- Cleanup scope: every file under `kazoo.testing` and `kazoo/tests` — Python, YAML, Dockerfiles, entrypoint scripts, JAAS configs (clarification Q2).

**Scale/Scope**: ~7,500 lines of integration tests + ~1,080 lines of harness (1066-line `kazoo_ensemble.py` split, 13-line `__init__.py`); 6 compose files, 2 JAAS configs, 4 Dockerfile trees relocated; ~19 integ test modules + 13 existing unit files, plus one new unit module.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Gate result | Notes |
|-----------|-------------|-------|
| I. Higher-Level API & Recipes | PASS | Internal testing refactor; no recipe or client API changes. |
| II. Test-First (NON-NEGOTIABLE) | PASS | Sequencing below writes `test_testing.py` tests against the new `common.py` surface first (red), then extracts logic (green); the existing integration suite is the regression gate and must stay green at every checkpoint. |
| III. Integration Testing Against Real ZooKeeper | PASS | Harness still drives the real 3-node ensemble; SC-001 requires identical integ outcomes. (The constitution's `ZOOKEEPER_PATH`/`KazooTestCase` text is legacy — superseded by the docker-compose harness this feature consolidates.) |
| IV. Backward Compatibility & Semantic Versioning | PASS with justification | `kazoo.testing.kazoo_ensemble` is test-support API, not the stable client surface; the prior `KazooTestCase` removal set the precedent of recording test-API changes in `CHANGES.md`. This plan removes the module name and updates every in-repo reference (conftest, docs, mypy config, workflow comment) in the same change, and records the re-layout under the unreleased section of `CHANGES.md`. See Complexity Tracking. |
| V. Rigorous Quality Gates | PASS | `flake8`, `black`, strict `mypy` must pass on all new/modified modules (SC-007); commits follow the Angular convention. |

## Project Structure

### Documentation (this feature)

```text
specs/003-testing-resources-refactor/
├── plan.md              # This file (/speckit.plan command output)
├── research.md          # Phase 0 output (/speckit.plan command)
├── data-model.md        # Phase 1 output (/speckit.plan command)
├── quickstart.md        # Phase 1 output (/speckit.plan command)
├── contracts/           # Phase 1 output (/speckit.plan command)
│   └── modules.md       # kazoo.testing public surface + resource path invariants
└── tasks.md             # Phase 2 output (/speckit.tasks command - NOT created by /speckit.plan)
```

### Source Code (repository root)

```text
kazoo/
├── testing/                     # the self-contained testing framework package
│   ├── __init__.py              # updated docstring (no history); re-exports
│   ├── common.py                # ALL business logic (pytest/Docker-free where possible)
│   ├── fixtures.py              # thin fixtures + plugin hooks, documented, delegate to common
│   ├── docker-compose.base.yml
│   ├── docker-compose.auth-digest.yml
│   ├── docker-compose.auth-sasl-digest.yml
│   ├── docker-compose.auth-sasl-gssapi.yml
│   ├── docker-compose.auth-tls.yml
│   ├── docker-compose.features-capture.yml
│   ├── jaas/
│   │   ├── sasl-digest.conf
│   │   └── sasl-gssapi.conf
│   └── dockerfiles/
│       ├── capture/             # Dockerfile + capture-entrypoint.sh
│       ├── certgen/             # Dockerfile + entrypoint.sh
│       ├── kdc/                 # Dockerfile + root/entrypoint.sh
│       └── tls-secrets-agent/   # Dockerfile + entrypoint.sh
│
├── tests/
│   ├── conftest.py              # DELETED (was fully commented out)
│   ├── integ/                   # test cases only (no framework resources)
│   │   ├── conftest.py          # re-exports fixtures/hooks from kazoo.testing.fixtures
│   │   └── test_*.py            # 19 modules (unchanged apart from doc/comment cleanup)
│   └── unit/
│       ├── test_testing.py      # NEW: 100% branch coverage of common.py pure functions
│       └── test_*.py            # existing unit files
```

**Structure Decision**: Single project — the `kazoo` library keeps its existing layout; only `kazoo/testing/` (framework resources + split modules) and the test tree change. The relocated resources sit at the `kazoo.testing` package root so `resources.files("kazoo.testing")` is the compose context and the existing `build: ./dockerfiles/...` and `${ZK_COMPOSE_DIR}/jaas/...` relative references keep resolving from the compose file's own directory.

## Complexity Tracking

> Filled because Constitution Check has one justified deviation note (Principle IV — module-name removal).

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| `kazoo.testing.kazoo_ensemble` module name removed (Principle IV) | The user's directive is an explicit two-module split (`common.py` + `fixtures.py`); keeping the old name would mean a third file and contradict FR-006/SC-006. `kazoo.testing` is test-support API, not the stable client surface; the prior `KazooTestCase` removal set the precedent. | Keeping `kazoo_ensemble.py` as a re-export shim preserves the name but adds a file the user explicitly asked to eliminate, and every in-repo reference is updated in the same atomic change, so no import can break. |

## Phase 0: Research

Output: [research.md](research.md) — resolves the layout, module-split, packaging, and unit-test-strategy decisions.

## Phase 1: Design & Contracts

Output: [data-model.md](data-model.md), [contracts/modules.md](contracts/modules.md), [quickstart.md](quickstart.md).

## Constitution Check (post-design re-evaluation)

Re-checked after Phase 1: all gates still PASS; the Principle IV justification in Complexity Tracking stands (module is test-support API; CHANGES.md records the re-layout; no external import can break since all references are in-repo and updated atomically). Principle II sequencing is honored: new unit tests land against the `common.py` surface before the logic migration, and the integration suite remains the regression gate throughout.

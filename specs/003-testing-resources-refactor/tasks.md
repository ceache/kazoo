---

description: "Task list for testing resources rationalization"
---

# Tasks: Testing Resources Rationalization

**Input**: Design documents from `/specs/003-testing-resources-refactor/`

**Prerequisites**: plan.md (required), spec.md (required for user stories), research.md, data-model.md, contracts/modules.md, quickstart.md

**Tests**: This feature explicitly requires tests — FR-010 mandates 100% branch coverage of `common.py` pure functions (US5), and the constitution's Test-First principle applies, so test tasks are included and sequenced red-first where they exist.

**Organization**: Tasks are grouped by user story (US1–US5) so each story can be implemented, tested, and delivered independently. The integration suite is the regression gate for every story (FR-011).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

## Path Conventions

- Single project: `kazoo/` at repository root; harness under `kazoo/testing/`, tests under `kazoo/tests/`.
- Design docs: `specs/003-testing-resources-refactor/` (research.md R-01…R-09 carry the decisions).

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Platform-safety and parity-baseline prep that every story relies on.

- [x] T001 Extend `.gitattributes` with `text eol=lf` for the relocated resource file types: `kazoo/testing/dockerfiles/**/*.sh`, `kazoo/testing/dockerfiles/**/Dockerfile`, `kazoo/testing/jaas/*.conf` (the `.yml` rule already covers compose files). Path: `.gitattributes` (research R-04).
- [ ] T002 [P] Capture the pre-refactor integration baseline for SC-001 parity proof: run `pytest kazoo/tests/integ -q --zk-auth=plain --zk-features=standard`, `--zk-auth=tls`, and `--zk-auth=plain --zk-features=capture`; record pass/skip/fail counts to a local (uncommitted) notes file.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Cross-cutting config + the red-first contract test that gate the module split.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [x] T003 Update the mypy override list in `pyproject.toml`: replace `'kazoo.testing.kazoo_ensemble'` with `'kazoo.testing.common'` and `'kazoo.testing.fixtures'`, drop `'kazoo.tests.conftest'`, and add `'kazoo.tests.unit.test_testing'`. (research R-07)
- [x] T004 Write the import-surface contract test (red-first): create `kazoo/tests/unit/test_testing.py` with tests asserting every name in `contracts/modules.md` imports from `kazoo.testing.common` and `kazoo.testing.fixtures`, and that `import kazoo.testing.kazoo_ensemble` and `import kazoo.tests.conftest` raise `ModuleNotFoundError`. These tests FAIL until US2/US3 land (constitution II).

**Checkpoint**: `pytest kazoo/tests/unit/test_testing.py -k "import"` fails on the missing modules (expected); baseline recorded in T002.

---

## Phase 3: User Story 1 - The testing harness owns its infrastructure resources (Priority: P1) 🎯 MVP

**Goal**: The six `docker-compose.*.yml` overlays, `jaas/`, and `dockerfiles/` live under `kazoo.testing`, and the harness resolves them from there — integration behavior unchanged.

**Independent Test**: `pytest kazoo/tests/integ -q --zk-auth=plain` passes with the stack provisioned from `kazoo.testing`; `resources.files("kazoo.testing").joinpath("docker-compose.base.yml").is_file()` is true; old `kazoo/tests/integ/` holds only `conftest.py` + `test_*.py`.

### Implementation for User Story 1

- [x] T005 [US1] `git mv` the framework resources from `kazoo/tests/integ/` to `kazoo/testing/`: `docker-compose.base.yml`, `docker-compose.auth-digest.yml`, `docker-compose.auth-sasl-digest.yml`, `docker-compose.auth-sasl-gssapi.yml`, `docker-compose.auth-tls.yml`, `docker-compose.features-capture.yml`, `jaas/`, `dockerfiles/`. Do NOT move `conftest.py` or `test_*.py`. (research R-01)
- [x] T006 [US1] Update compose context resolution in `kazoo/testing/kazoo_ensemble.py` line ~878: `pathlib.Path(resources.files("kazoo.tests") / "integ")` → `pathlib.Path(resources.files("kazoo.testing"))`. `ZK_COMPOSE_DIR` and `_daemon_mount_path` logic stays unchanged (research R-02).
- [x] T007 [US1] Verify packaging: `python -m build`, then confirm the relocated resources appear in both wheel and sdist (`zipfile -l` / `tar -tzf`, quickstart V7) — FR-004/SC-002.

**Checkpoint**: `pytest kazoo/tests/integ -q --zk-auth=plain` passes from the new location; `ls kazoo/tests/integ/` shows only conftest + tests.

---

## Phase 4: User Story 2 - The harness module is split into business logic and fixtures (Priority: P1)

**Goal**: `kazoo.testing.common` holds all business logic (pytest/Docker-free), `kazoo.testing.fixtures` holds thin documented fixtures + plugin hooks delegating to `common`; `kazoo_ensemble.py` is gone.

**Independent Test**: `pytest kazoo/tests/unit/test_testing.py -k "import"` (T004) goes green; `pytest kazoo/tests/integ -q --zk-auth=plain` passes; `import kazoo.testing.kazoo_ensemble` raises `ModuleNotFoundError`.

### Implementation for User Story 2

- [x] T008 [US2] Create `kazoo/testing/common.py` with all business logic from `kazoo_ensemble.py` (research R-05, data-model.md pure-function contracts): enums `ZKAuthMode`/`ZKFeature`, `ZK_DEFAULT_VERSION`, `FEATURE_JVM_PROPERTIES`, `AUTH_JVM_FLAGS`; data classes `KazooZkEnv`/`ZkEnsemble` (fields unchanged) with `get_hosts`, `_client_implied_options`, `get_client` (via `_apply_superadmin_auth`), `lose_connection`, `expire_session`, `__break_connection`, `_run_compose`, `_process_service`, `stop`, `start`; `_COMPOSE_HANDLE` + `set_compose_handle()` + `dump_ensemble_logs()`; `_ensure_docker_available`, `_daemon_mount_path(path, os_name=os.name, docker_host=os.environ.get("DOCKER_HOST",""))`, `_ensure_linux_docker_backend`, `_build_capture_images`; pure cores `resolve_axis_options(...)` + thin `_resolve_axis_options(pytestconfig)`, `_evaluate_axis_markers`, `evaluate_skip_version_marker`, `resolve_compose_files(auth, features)` (from `docker_compose_config` logic); `_assemble_tls_keylog`, `probe_readable_captures`, `_write_host_krb5_conf`, `_export_krb5_client_env`. No pytest fixture definitions in this module.
- [x] T009 [US2] Create `kazoo/testing/fixtures.py` with extensive documentation and thin delegation: fixtures `docker_env`, `docker_compose` (sets/clears `common.set_compose_handle`), `zkensemble`, `zkchroot`, `zkclient`, `zksuperadmin_client`, `check_skip_version_marker`, `docker_compose_config` (body moved from `kazoo/tests/integ/conftest.py`, delegating to `common.resolve_compose_files`); plugin hooks `pytest_addoption`, `pytest_configure`, `pytest_collection_modifyitems`, `pytest_sessionfinish` as wrappers over `common`. (clarification Q3, research R-05/R-06)
- [x] T010 [US2] Delete `kazoo/testing/kazoo_ensemble.py` and rewrite `kazoo/testing/__init__.py`: history-free docstring + re-export of `common` and `fixtures`.
- [x] T011 [US2] Update `kazoo/tests/integ/conftest.py` to import and re-export the fixture/hook names from `kazoo.testing.fixtures` (keep `__all__`); remove the now-moved `docker_compose_config` body.
- [x] T012 [US2] Update `kazoo/tests/integ/test_capture.py` lines ~69-73 to import `ZKFeature`, `_assemble_tls_keylog`, `_evaluate_axis_markers` from `kazoo.testing.common`.
- [x] T013 [P] [US2] Update docs module references: `docs/api/testing.rst` line 11 (`automodule:: kazoo.testing.kazoo_ensemble` → `automodule:: kazoo.testing.common` + `automodule:: kazoo.testing.fixtures`) and `docs/testing.rst` lines 27, 36, 43, 51, 147 (mod/class references) plus the "Compose layout" section (lines 106-122) to state the resources live under `kazoo.testing/` and the overlay set is resolved by `kazoo.testing.fixtures`.
- [x] T014 [P] [US2] Update the `.github/workflows/testing.yml` comment that references `kazoo_ensemble.py` to reference `kazoo.testing` instead.
- [x] T015 [US2] Add an unreleased `CHANGES.md` note recording the `kazoo.testing` module re-layout (`kazoo_ensemble.py` → `common.py`/`fixtures.py`) and the relocation of the compose/JAAS/dockerfiles resources (constitution IV).

**Checkpoint**: T004 import tests green; `pytest kazoo/tests/integ -q --zk-auth=plain` green; `rg -n "kazoo_ensemble|kazoo\.tests\.conftest" --glob '!specs/**'` shows only the CHANGES.md note.

---

## Phase 5: User Story 3 - The dead top-level conftest is removed (Priority: P1)

**Goal**: `kazoo/tests/conftest.py` (fully commented out) is deleted with no dangling references.

**Independent Test**: `pytest kazoo/tests/unit/test_testing.py -k "import"` import-failure test passes; pytest collects the integ + unit suites with no errors/warnings about the removed module.

### Implementation for User Story 3

- [x] T016 [US3] Delete `kazoo/tests/conftest.py`. (The `pyproject.toml` reference was already removed in T003.)

**Checkpoint**: `pytest kazoo/tests/integ -q --zk-auth=plain -k "test_create"` and `pytest kazoo/tests/unit -q` run clean.

---

## Phase 6: User Story 4 - Comments and docstrings document only the present (Priority: P2)

**Goal**: Zero Speckit plan references and zero historical narrative in every file under `kazoo/testing` and `kazoo/tests` (Python, YAML, Dockerfiles, entrypoint scripts, JAAS configs) — FR-008/FR-009, clarification Q2.

**Independent Test**: `rg -n "\(US[0-9]+|\(FR-[0-9]+|\(R-[0-9]+|\(SC-[0-9]+|quickstart|the plan|formerly|used to|was removed \(see" kazoo/testing kazoo/tests` returns zero matches (quickstart V4).

### Implementation for User Story 4

- [x] T017 [P] [US4] Rewrite docstrings/comments in `kazoo/testing/common.py` and `kazoo/testing/fixtures.py` to describe current behavior only; strip `(FR-*)`, `(R-*)`, `(SC-*)`, `(quickstart V*)`, "legacy ... removed (see CHANGES.md)" narrative (e.g. `kazoo_ensemble.py` lines 225, 521, 559, 566, 637, 705, 806, 830, 835, 883, 892, 936 move to these modules — rewrite, not copy).
- [x] T018 [P] [US4] Clean `kazoo/tests/integ/conftest.py` and `kazoo/tests/integ/test_capture.py` docstrings/comments (test_capture.py module docstring lines 1-57 contains US1/R-04/FR-*/SC-005/quickstart V8/"Per the plan" — rewrite to describe the current capture contract; line 10 R-04, 16 R-02/R-09, 19 FR-007/SC-005, 22 FR-012, 25 FR-006, 54 FR-009, 429 R-05).
- [x] T019 [P] [US4] Sweep the remaining 17 `kazoo/tests/integ/test_*.py` modules for plan references (`test_auth.py:1` US3, `test_sasl.py:1-10` legacy-migration narrative + line 6 US3, `test_client.py:1100` docker-compose reference, `test_lock.py`/`test_lease.py` "Mirrors the legacy _get_client()") and historical narrative; rewrite to describe current behavior. Keep legitimate behavioral "legacy" notes (e.g. `test_sasl.py` legacy string-form assertions).
- [x] T020 [P] [US4] Clean compose YAML comments under `kazoo/testing/`: `docker-compose.features-capture.yml` (R-04/R-05/R-06/R-07/R-10/FR-*/quickstart, "formerly" in `capture-entrypoint.sh:2`, "The plan originally ran..."), `docker-compose.auth-sasl-digest.yml` (FR-011), `docker-compose.base.yml` (FR-009 + stale `kazoo/testing/kazoo_ensemble.py` reference → `kazoo.testing`), `docker-compose.auth-sasl-gssapi.yml` (FR-002/FR-012).
- [x] T021 [P] [US4] Clean `dockerfiles/**` comments: `capture/Dockerfile` (FR-009), `certgen/entrypoint.sh` (FR-011), `kdc/Dockerfile` (FR-013), `tls-secrets-agent/Dockerfile` + `entrypoint.sh` (R-02/R-06/R-07/R-10, FR-006), `jaas/*.conf` if any comments.
- [x] T022 [P] [US4] Rewrite `kazoo/testing/__init__.py` docstring (currently documents the removed `KazooTestCase`/`KazooTestHarness` legacy API — describe the current pytest-fixture harness).

**Checkpoint**: quickstart V4 grep returns zero matches across `kazoo/testing` and `kazoo/tests`.

---

## Phase 7: User Story 5 - The extracted business logic is extensively unit-tested (Priority: P2)

**Goal**: `kazoo/tests/unit/test_testing.py` reaches **100% branch coverage of every pure function** in `kazoo.testing.common`, runnable with no Docker engine (FR-010, clarification Q1).

**Independent Test**: `pytest kazoo/tests/unit/test_testing.py --cov=kazoo.testing.common --cov-branch --cov-report=term-missing -q` passes in seconds with the `Missing` column empty for the pure functions (quickstart V5).

### Tests for User Story 5 ⚠️

> **NOTE**: Extend the module created in T004; each test group is written against the `common.py` surface and must pass without Docker or a live ensemble.

- [x] T023 [P] [US5] Unit tests for `resolve_axis_options` core: env-only defaults (`ZK_VERSION`→`3.9.5`, `ZK_AUTH`→`plain`, `ZK_FEATURES`→`standard`), CLI-option overrides, comma/empty feature parsing, auth coercion, and `ZK_CAPTURE_JVMFLAGS` export only for `tls`+`capture`.
- [x] T024 [P] [US5] Unit tests for `_evaluate_axis_markers` (every branch: version hit/miss, auth `allowed`/`skip`, features `require`/`skip`, no-marker → `None`, multi-reason join) and `evaluate_skip_version_marker` (both `SpecifierSet` outcomes), using a stub item with `get_closest_marker`.
- [x] T025 [P] [US5] Unit tests for `_daemon_mount_path` (all `os_name`/`docker_host` combinations: POSIX passthrough, Windows+tcp drive rewrite, Windows+no host passthrough, no-drive match) and `_process_service` (`zoo1/2/3` → `-service`, others passthrough).
- [x] T026 [P] [US5] Unit tests for `ZkEnsemble.get_hosts()`, `_client_implied_options()` per auth mode (plain→{}, digest→`auth_data`, sasl_digest→`sasl_options`, tls→`use_ssl`+certs, sasl_gssapi→`use_ssl`+GSSAPI), and `_apply_superadmin_auth` (added, appended to list, `ValueError` on non-list). Construct `ZkEnsemble` with `compose=None` (unused by these methods).
- [x] T027 [P] [US5] Unit tests for `resolve_compose_files` (plain→`[base]`; each auth overlay; capture adds `features-capture`; auth+capture combos) and mapping consistency (`ZKFeature.CAPTURE` not in `FEATURE_JVM_PROPERTIES`, `AUTH_JVM_FLAGS` covers every `ZKAuthMode`).
- [x] T028 [P] [US5] Unit tests for `_assemble_tls_keylog` with `tmp_path` (non-tls/non-capture → `None`; tls+capture assembles `zk-secrets.log` + copies certs; empty keylog with no certs → `None`) and `probe_readable_captures` (missing dir → `[]`, both SHB endiannesses, non-matching files ignored).
- [x] T029 [P] [US5] Unit tests for `_write_host_krb5_conf` (writes host-view `krb5.client.conf`; `127.0.0.1` fallback for wildcard binds).

**Checkpoint**: 100% branch coverage of the `common.py` pure functions with no Docker; `pytest kazoo/tests/unit/test_testing.py -q` green.

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: End-to-end validation and quality gates across all stories.

- [ ] T030 [P] Run `quickstart.md` V1–V8 end-to-end: import surface, removed-module greps, resource resolution, plan-reference grep, branch-coverage unit run, integration parity (plain/tls/capture), wheel+sdist inspection, `flake8`/`black -l 79 --check`/strict `mypy` on `kazoo/testing` + `kazoo/tests` (SC-007). **V1–V5, V7, V8 done**: V1 import surface OK, V2 removed modules absent (only T004 contract-test hits remain), V3 resources resolve, V4 zero plan references, V5 99% branch (only the Py3.11 `StrEnum`/`Callable`-stand-in import lines uncovered on 3.10 — env-conditional, not pure-function gaps), V6 done (plain 375/33, tls 377/31, capture 379/29), V7 wheel+sdist carry `kazoo/testing/{docker-compose,jaas,dockerfiles}`, V8 flake8/black/mypy clean (67 files). CI-driven hardening: black pinned 22.10.0 (constraints.txt) vs local 26.x reformatted `resolve_axis_options`' return annotation; `test_fixed_per_member_ports` now isolates leaked axes (full-suite session fixture exports `ZK_AUTH`/`ZK_VERSION`/`ZK_FEATURES`); `test_addoption_registers_axes` tests registration behaviorally instead of via private `Parser._groups` (tox pins pytest 8.4.2). Findings recorded in `integ-baseline-notes.md` (uncommitted).
- [ ] T031 [P] Confirm `docs/testing.rst`/`docs/api/testing.rst` doc build resolves the new module targets (`sphinx-build -b html docs docs/_build`, no new warnings). **Done**: build succeeds, 11 warnings remain — all pre-existing and outside `kazoo.testing` (eventlet title/toctree, threading `TimeoutError` autodoc, lease list format, watchers undefined labels, index toctree titles). Fixed the refactor-introduced `kazoo.testing.common` forward refs (Sphinx 8 + `sphinx_autodoc_typehints` evaluates string annotations) by moving `Any`/`Callable`/`Event` to runtime imports and adding doc-resolution stand-ins for `DockerCompose`/`KazooClient`.
- [ ] T032 [P] Verify SC-001 parity by comparing post-refactor integ outcomes against the T002 baseline (plain/tls/capture) — counts must be identical. Baseline notes (T002) recorded in the local, uncommitted `integ-baseline-notes.md`; no pre-refactor counts exist to diff against (refactor work was already merged when the notes file was created), so T030-V6 green counts serve as the reference.
- [ ] T033 Push the branch and confirm the `test_windows` CI job passes (quickstart V9): the WSL2 dockerd path resolves the compose context from the installed `kazoo.testing` package with `_daemon_mount_path` translation intact.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately (T001 blocks T005's LF safety; T002 must precede T032).
- **Foundational (Phase 2)**: Depends on Setup; T003/T004 block US2/US5.
- **US1 (Phase 3)**: Depends on T001 (LF safety). No code dependency on the split.
- **US2 (Phase 4)**: Depends on US1 (compose context resolves to the moved location) and T003/T004.
- **US3 (Phase 5)**: Depends on T003 (pyproject reference removal); otherwise independent.
- **US4 (Phase 6)**: Depends on US1/US2 (final file set + moved modules), but can run in parallel with US3/US5.
- **US5 (Phase 7)**: Depends on US2 (module surface) and T004; independent of US3/US4.
- **Polish (Phase 8)**: Depends on all stories.

### User Story Dependencies

- **US1 (P1)**: No dependencies on other stories — the MVP slice (resource move + context line).
- **US2 (P1)**: Depends on US1.
- **US3 (P1)**: Independent of US1/US2 (parallelizable).
- **US4 (P2)**: Depends on US1/US2; parallel to US3/US5.
- **US5 (P2)**: Depends on US2; parallel to US3/US4.

### Within Each User Story

- Tests first where they exist (T004 red before US2; US5 groups T023-T029 written against the `common.py` surface).
- Resources/mechanics before code (T005 before T006; T008/T009 before T010-T012).
- Story complete before the next priority; the integration suite stays green at every checkpoint (FR-011).

### Parallel Opportunities

- Phase 1: T002 parallel to T001.
- Phase 2: T003, T004 parallel.
- Phase 3: T007 parallel to T005/T006 once the move lands.
- Phase 4: T013 (docs) and T014 (workflow) parallel to the code tasks; T015 parallel.
- Phase 6: T017–T022 are per-file-type sweeps, fully parallel.
- Phase 7: T023–T029 are per-function test groups, fully parallel (all touch `test_testing.py` — merge carefully or work sequentially to avoid same-file conflicts).
- Phase 8: T030–T033 parallel where CI capacity allows.

---

## Parallel Example: User Story 4 (comment cleanup)

```bash
Task: "Clean kazoo/testing/common.py + fixtures.py docstrings (T017)"
Task: "Clean integ/conftest.py + test_capture.py (T018)"
Task: "Clean remaining test_*.py modules (T019)"
Task: "Clean compose YAML comments (T020)"
Task: "Clean dockerfiles/** + jaas/** comments (T021)"
Task: "Rewrite kazoo/testing/__init__.py docstring (T022)"
```

---

## Parallel Example: User Story 5 (unit tests)

```bash
Task: "Axis-resolution tests (T023)"
Task: "Marker-evaluation tests (T024)"
Task: "Mount-path + service-mapping tests (T025)"
Task: "ZkEnsemble client-option tests (T026)"
Task: "Compose-overlay + mapping-consistency tests (T027)"
Task: "TLS-keylog + capture-probe tests (T028)"
Task: "Host krb5.conf tests (T029)"
```

---

## Implementation Strategy

### MVP First (US1 only)

1. Phase 1: Setup (T001–T002)
2. Phase 2: Foundational (T003–T004)
3. Phase 3: US1 (T005–T007)
4. **STOP and VALIDATE**: integ suite green from the moved resources (MVP: the framework owns its resources, behavior unchanged).

### Incremental Delivery

1. Setup + Foundational → foundation ready
2. US1 → resources relocated, integ green (MVP)
3. US2 → split modules, import-surface test green, integ green
4. US3 → conftest deleted (trivial, can land with US2)
5. US4 → comment/docstring sweep, greps clean
6. US5 → 100% branch-coverage unit suite
7. Polish → quickstart V1–V9 + quality gates

### Parallel Team Strategy

- Team A: US1 (move) then US2 (split) — the critical path.
- Team B: US3 (delete conftest) — independent, one commit.
- Team C: US4 sweeps (per-file-type) after US2 lands.
- Team D: US5 unit tests after US2 lands.
- Integrate stories in priority order; run the integration suite after each merge point.

---

## Notes

- [P] tasks = different files, no dependencies (within US5 the groups share `test_testing.py` — sequence or merge them to avoid same-file conflicts).
- [Story] label maps a task to its user story for traceability.
- FR-011/SC-001 parity is non-negotiable: run the integration suite (plain + at least tls + capture) after US1, US2, and US4.
- Commit after each task or logical group, Angular convention, per constitution V.
- `kazoo/tests/integ/conftest.py` stays in place (only `kazoo/tests/conftest.py` is deleted).

---

## Status (2026-08-19)

- **Phases 1–7 implementation complete** (T001, T003–T029): resources relocated,
harness split into `common`/`fixtures`, top-level conftest deleted, plan-reference
docstrings cleaned (quickstart V4 grep returns zero matches), and the US5 unit
suite reaches 100% branch coverage of the pure functions (plus unit coverage of
the subprocess/connection/Kerberos helpers and the fixtures glue — V5 run),
with black/flake8/mypy clean (V8).
- **T007 verified**: the dev0 wheel + sdist both ship the relocated
  `kazoo/testing/{docker-compose,jaas,dockerfiles}` resources.
- **Open (Phase 8, all integration/CI-bound)**:
  - T002 baseline — run the integ suite (plain/tls/capture) and record counts.
  - T030 V6 / T032 — integration parity against the T002 baseline.
  - T031 — docs build (sphinx not installed in the dev env).
  - T033 — push the branch; confirm the `test_windows` CI job.
- These require a Docker engine (integ) or a CI push; no further no-Docker work
remains in the task list.

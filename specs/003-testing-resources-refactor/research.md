# Research: Testing Resources Rationalization

**Feature**: [spec.md](spec.md) | **Date**: 2026-08-18

This file records the Phase 0 research decisions for the testing-framework refactor. Every decision below was validated against the current codebase (compose files, harness module, packaging config, docs) rather than assumed.

## R-01: Destination of the relocated resources — `kazoo.testing` package root

- **Decision**: Move the six `docker-compose.*.yml` overlays, `jaas/`, and `dockerfiles/` from `kazoo/tests/integ/` directly into the `kazoo/testing/` package directory (the `kazoo.testing` package root).
- **Rationale**:
  - The harness already resolves its compose context with `importlib.resources.files("kazoo.tests") / "integ"` (`kazoo_ensemble.py`). After the move this becomes `resources.files("kazoo.testing")`, which is exactly the package root — no subdirectory traversal needed.
  - Docker Compose resolves `build: ./dockerfiles/capture` (and the other three builds) relative to the compose file's own directory, and `docker-compose.auth-sasl-gssapi.yml` references `${ZK_COMPOSE_DIR}/jaas/sasl-gssapi.conf`. Because compose files, `jaas/`, and `dockerfiles/` move **together**, every relative reference resolves unchanged from the new context directory (`zkensemble`/`docker_compose` fixtures set `ZK_COMPOSE_DIR = _daemon_mount_path(context)` already).
  - Matches the user's phrasing ("moved to kazoo/testing") and the spec (FR-001/FR-002).
- **Alternatives considered**:
  - `kazoo/testing/compose/` (or `resources/`) subdirectory — rejected: would require editing every `build: ./dockerfiles/...` reference and every `${ZK_COMPOSE_DIR}/jaas/...` mount, and adds nesting with no benefit since the compose context is a single directory.
  - Leave resources under `kazoo/tests/integ/` — rejected: that is the status quo the user explicitly wants changed.

## R-02: Compose context + path translation after the move

- **Decision**: `docker_compose` fixture sets `context = str(resources.files("kazoo.testing"))`; `ZK_COMPOSE_DIR` continues to be `_daemon_mount_path(context)` so the daemon-translated path (Windows → `/mnt/d/...`) still points at the relocated `jaas/` mounts.
- **Rationale**: The Windows-remote behavior (WSL2 dockerd, `DOCKER_HOST=tcp://...`) must keep working (FR-011/SC-001); nothing in the translation logic is location-specific, only the source directory changes.
- **Alternatives considered**: Hardcoding a source-tree path — rejected (FR-002 requires installed-package resolution).

## R-03: Packaging of the relocated resources

- **Decision**: No packaging changes required beyond verification. `setup.cfg` `include_package_data = true` + `MANIFEST.in` `recursive-include kazoo *` already bundle everything under `kazoo/`, so the moved YAML/JAAS/Dockerfile trees ship in both wheel and sdist. Add explicit validation to `quickstart.md` (build wheel + sdist, inspect contents).
- **Rationale**: The files are moving within `kazoo/`, so existing packaging already covers them (FR-004).
- **Alternatives considered**: Adding per-directory `package_data` entries — rejected as redundant with the `recursive-include kazoo *` manifest rule.

## R-04: Line-ending safety for the moved resources

- **Decision**: Extend `.gitattributes` with `text eol=lf` for the shell/Dockerfile/conf files under `kazoo/testing/` (`*.sh`, `Dockerfile`, `*.conf`) — the `.yml` rule (`*.yml text eol=lf`) already covers the compose files, and `docker-compose.base.yml` header also becomes stale (references `kazoo/testing/kazoo_ensemble.py`; cleanup per FR-008).
- **Rationale**: The `dockerfiles/**/entrypoint.sh` and Dockerfiles must stay LF so container entrypoints and builds behave identically on every checkout platform (consistency with the existing `.github` eol rules; protects FR-004/FR-011).
- **Alternatives considered**: Relying on git's autocrlf defaults — rejected; the repo already standardizes eol for YAML/scripts explicitly.

## R-05: `kazoo_ensemble.py` split — what lands where

- **Decision**:
  - `kazoo/testing/common.py` (business logic, no pytest fixture definitions):
    - Data/enums: `ZKAuthMode`, `ZKFeature`, `ZK_DEFAULT_VERSION`, `FEATURE_JVM_PROPERTIES`, `AUTH_JVM_FLAGS`.
    - Data classes: `KazooZkEnv`, `ZkEnsemble` (with `get_hosts`, `_client_implied_options`, `get_client` + extracted `_apply_superadmin_auth`, `lose_connection`, `expire_session`, `__break_connection`, `_run_compose`, `_process_service`, `stop`, `start`).
    - Compose/docker helpers: `_ensure_docker_available`, `_daemon_mount_path`, `_ensure_linux_docker_backend`, `_build_capture_images`, `dump_ensemble_logs` + `_COMPOSE_HANDLE` global + `set_compose_handle()` setter.
    - Axis logic: pure core `resolve_axis_options(...)` (options + environ in → axes + env map out) and thin `_resolve_axis_options(pytestconfig)` wrapper that reads the config/env and applies env updates; `_evaluate_axis_markers`; `evaluate_skip_version_marker(condition, active_version)`; `resolve_compose_files(auth, features)`.
    - Capture/Kerberos: `_assemble_tls_keylog`, `_export_krb5_client_env` + extracted pure `_write_host_krb5_conf`, `probe_readable_captures(workdir)` (extracted from `pytest_sessionfinish`).
  - `kazoo/testing/fixtures.py` (pytest glue, thin + documented):
    - Fixtures: `docker_env`, `docker_compose`, `zkensemble`, `zkchroot`, `zkclient`, `zksuperadmin_client`, `check_skip_version_marker`, `docker_compose_config` (delegates to `resolve_compose_files`).
    - Hooks (per clarification Q3): `pytest_addoption`, `pytest_configure`, `pytest_collection_modifyitems`, `pytest_sessionfinish` — thin wrappers over `common`.
  - `kazoo/testing/kazoo_ensemble.py` deleted; `kazoo/testing/__init__.py` re-exports the new modules with a history-free docstring.
- **Rationale**: Keeps `common.py` importable and unit-testable without pytest/Docker (US2, FR-006/FR-010); the `pytest_sessionfinish` capture-probing logic and the `docker_compose_config` overlay-selection logic move into `common` as pure functions so they are branch-testable.
- **Alternatives considered**: Leaving `pytest_*` hooks in `common.py` — rejected by clarification Q3; a separate `plugin.py` — rejected (clarification Q3 chose `fixtures.py`).

## R-06: `_COMPOSE_HANDLE` cross-module sharing

- **Decision**: Keep `_COMPOSE_HANDLE` and `dump_ensemble_logs()` in `common.py`; `fixtures.py`'s `docker_compose` fixture calls `common.set_compose_handle(compose)` on setup and `common.set_compose_handle(None)` on teardown.
- **Rationale**: The global is inherently shared mutable state between the fixture lifecycle and the log dump; placing it with the log-dump logic keeps fixtures thin and the handle access centralized.
- **Alternatives considered**: Moving the handle into `fixtures.py` — rejected: `dump_ensemble_logs` is business logic in `common`, and `test_capture`/unit tests import the dump logic from `common`.

## R-07: Import-surface changes for existing test modules

- **Decision**:
  - `kazoo/tests/integ/conftest.py` re-exports fixtures/hooks from `kazoo.testing.fixtures` (and now also re-exports `docker_compose_config`, whose body moves to `fixtures.py`).
  - `kazoo/tests/integ/test_capture.py` imports `ZKFeature`, `_assemble_tls_keylog`, `_evaluate_axis_markers` from `kazoo.testing.common`.
  - `pyproject.toml` mypy override list: replace `'kazoo.testing.kazoo_ensemble'` with `'kazoo.testing.common'`, `'kazoo.testing.fixtures'`, `'kazoo.tests.unit.test_testing'`; drop `'kazoo.tests.conftest'`.
  - `docs/testing.rst`/`docs/api/testing.rst` `:mod:`/`automodule` targets → `kazoo.testing.common` + `kazoo.testing.fixtures`.
  - `.github/workflows/testing.yml` comment referencing `kazoo_ensemble.py` → `kazoo.testing`.
  - `CHANGES.md`: unreleased note recording the module re-layout and resource move.
- **Rationale**: FR-007 (public names preserved), SC-006 (no dangling references), Principle IV recording.
- **Alternatives considered**: Keeping a `kazoo_ensemble.py` re-export shim — rejected per spec SC-006 (see plan Complexity Tracking).

## R-08: Unit-test strategy for 100% branch coverage of `common.py` pure functions

- **Decision**: `kazoo/tests/unit/test_testing.py` exercises the pure functions with dependency injection and synthetic inputs:
  - `resolve_axis_options` core: env-only defaults, CLI-option overrides, feature-string parsing (comma list, empty → `standard`), auth coercion, and the `ZK_CAPTURE_JVMFLAGS` export only for `tls`+`capture`.
  - `_evaluate_axis_markers`: synthetic items via a lightweight stub exposing `get_closest_marker`; all branches — version specifier hit/miss, auth `allowed`/`skip`, features `require`/`skip`, no-marker, multi-reason join.
  - `evaluate_skip_version_marker`: `SpecifierSet` membership both ways.
  - `_daemon_mount_path`: `os_name`/`DOCKER_HOST` injected via default params — POSIX passthrough, Windows+tcp drive rewrite (`/mnt/<drive>`), Windows+no-host passthrough, Windows+non-tcp passthrough, no-drive match.
  - `_process_service`: `zoo1/2/3` → `-service`, any other name passthrough.
  - `ZkEnsemble.get_hosts` and `_client_implied_options` per auth (plain/digest/sasl_digest/tls/sasl_gssapi) with `compose=None` (unused by these methods).
  - `_apply_superadmin_auth`: no existing `auth_data` → added; list → appended; non-list → `ValueError`.
  - `resolve_compose_files`: every auth × {capture, no-capture} combination; plain → base only.
  - `_assemble_tls_keylog`: non-tls/non-capture → `None`; tls+capture assembles keylog + copies certs; empty keylog with no certs → `None`.
  - `probe_readable_captures`: missing dir → `[]`; magic-detection both SHB endiannesses; non-matching files ignored.
  - `_write_host_krb5_conf`: writes host-view config with `127.0.0.1` fallback for wildcard bind.
  - Mapping consistency: `ZKFeature.CAPTURE` absent from `FEATURE_JVM_PROPERTIES`; `AUTH_JVM_FLAGS` covers every `ZKAuthMode`.
  - Coverage measured with `pytest --cov=kazoo.testing.common --cov-branch --cov-report=term-missing` in the quickstart.
- **Rationale**: These are exactly the "pure functions" the clarification Q1 target applies to; Docker/subprocess-bound helpers (`_ensure_docker_available`, `_ensure_linux_docker_backend`, `_build_capture_images`, `_export_krb5_client_env` kinit/ccache part) are outside the pure set and keep their non-Docker branches covered where feasible.
- **Alternatives considered**: Integration-level verification only — rejected (US5 requires fast, Docker-free regression catching).

## R-09: Cleanup mechanics (plan-level note)

- **Decision**: The FR-008/FR-009 cleanup is a mechanical sweep applied to every file under `kazoo.testing` and `kazoo/tests` (Python docstrings/comments, YAML comments, Dockerfile comments, `entrypoint.sh` comments, JAAS `#` comments), excluding `specs/` and `docs/` (out of the declared scope) — plus the `.github/workflows/testing.yml` *comment-only* reference to the removed module name.
- **Rationale**: FR-008/FR-009 scope is explicit (per clarification Q2); `docs/` updates are a separate companion requirement (SC-006) limited to the module-name references, not a full doc rewrite.
- **Alternatives considered**: Extending cleanup to `docs/` and `specs/` — rejected; out of scope and would bloat the change.

## Validation notes

All decisions above were confirmed against the actual files: compose build contexts and `${ZK_COMPOSE_DIR}/jaas/...` mounts resolve relative to the compose context directory (R-01/R-02); `MANIFEST.in` `recursive-include kazoo *` covers the moved trees (R-03); `test_capture.py` and `conftest.py` are the only test-tree importers of `kazoo.testing.kazoo_ensemble` (R-07); `docs/testing.rst` and `docs/api/testing.rst` hold the module references (R-07).

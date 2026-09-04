# Feature Specification: Testing Resources Rationalization

**Feature Branch**: `003-testing-resources-refactor`

**Created**: 2026-08-18

**Status**: Draft

**Input**: User description: "I am to rationalize the testing resources management. I think the jaas, dockerfiles and all the docker-compose-* files in kazoo/tests/integ should be moved to kazoo/testing as they are integral parts of the testing framework, not part of the test data. kazoo/tests/conftest.py seems completely useless and should be removed. Further more, I would like all the comments and docstrings in all the tests and kazoo.testing module to a) only document what is, not what was (no more "it used to X, but now we Y"). and b) not have any Speckit plan references (e.g. the "US1" in "Integration self-check tests for the capture axis (US1)."). One more thing, I would like the kazoo/testing/kazoo_ensemble.py module to be broken down in two. I want all business logic functions to move to a common.py module, while the fixtures are moved to kazoo/testing/fixtures.py where it would be minimal code (Calling out to common.py) decorators and lots of documentation. The new kazoo/testing/common module should have extensive unit testing in kazoo/tests/unit/test_testing.py"

## Clarifications

### Session 2026-08-18

- Q: How thorough must the new unit tests for `kazoo.testing.common` be for this feature to count as done? → A: 100% branch coverage of every pure function in `common.py` (axis resolution, marker evaluation, mount paths, service mapping, client options, keylog assembly).
- Q: Does the comment/docstring cleanup requirement extend to the resource files that move into `kazoo.testing` (compose overlays, Dockerfiles, entrypoint scripts, JAAS configs)? → A: Yes — clean all plan references and historical narrative from every file under `kazoo.testing` and `kazoo/tests`, not just Python source.
- Q: Where should the pytest plugin hooks (`pytest_addoption`, `pytest_configure`, `pytest_collection_modifyitems`, `pytest_sessionfinish`) live in the new module layout? → A: In `fixtures.py` as thin wrappers delegating the real logic (marker evaluation, axis resolution, capture probing) to `common.py`.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The testing harness owns its infrastructure resources (Priority: P1)

A Kazoo developer who needs to understand how the integration harness provisions a ZooKeeper ensemble expects every resource the harness depends on — the docker-compose stack overlays, the JAAS server configs, and the sidecar Dockerfiles (KDC, certgen, TLS-secrets agent, capture) — to live inside `kazoo.testing`, the testing framework package, alongside the code that consumes them. The `kazoo/tests/integ` directory then contains only the test cases themselves. Nothing about the way a test session is launched, an axis is selected, or a stack is provisioned changes.

**Why this priority**: This is the core reorganization. Moving the resources is the prerequisite for everything else; it defines the new boundary between "framework" and "test data" and makes `kazoo.testing` self-contained. Without it, none of the downstream cleanup has a home.

**Independent Test**: Run the integration suite (plain axis) from a clean checkout. It must provision the full ensemble exactly as before — same compose files resolved, same JAAS configs mounted, same sidecar images built, same temp-dir artifact layout — and produce identical pass/skip/fail results, with the resources now resolved from `kazoo.testing`.

**Acceptance Scenarios**:

1. **Given** the current repository layout, **When** the resources (`jaas/`, `dockerfiles/`, `docker-compose-*.yml`) are relocated into `kazoo.testing`, **Then** the harness resolves its compose context and every relative resource reference from the new location.
2. **Given** the relocated resources, **When** the integration suite runs under any auth/feature axis (digest, sasl_digest, sasl_gssapi, tls; capture), **Then** the stack provisions identically to before the move (same images, mounts, interpolation variables).
3. **Given** a fresh install of the package (wheel and sdist), **When** a developer runs the integration suite against the installed distribution, **Then** the relocated resources are present and resolvable.

---

### User Story 2 - The harness module is split into business logic and fixtures (Priority: P1)

A Kazoo maintainer who wants to reason about the harness wants a clear separation: one module (`kazoo.testing.common`) holds all business logic — the axis enums and their JVM flag mappings, the ensemble/client connection logic, the Docker availability and mount-path translation helpers, the TLS-keylog and Kerberos environment assembly, the marker-evaluation and axis-resolution functions — and another module (`kazoo.testing.fixtures`) holds the pytest fixtures and plugin hooks as thin, well-documented wrappers that delegate to `common`. The fixtures module is minimal code plus documentation, so the logic is testable without pytest, Docker, or a live ZooKeeper.

**Why this priority**: The business logic is currently entangled with pytest fixtures in a single 1000+ line module, which makes the logic untestable outside a live harness. Splitting it is what unlocks the extensive unit-testing story (US5) and makes the harness maintainable.

**Independent Test**: Import `kazoo.testing.common` in an environment with neither pytest hooks exercised nor Docker present, and call the pure functions (e.g. axis resolution, mount-path translation, service mapping, marker evaluation against synthetic items). All must work. The `kazoo.testing.fixtures` module must re-export the same public fixture/hook names the integration `conftest.py` currently imports.

**Acceptance Scenarios**:

1. **Given** the current `kazoo_ensemble.py`, **When** its business logic is extracted into `kazoo.testing.common` and its fixtures into `kazoo.testing.fixtures`, **Then** no business logic lives in the fixtures module (only delegation, decorators, and documentation).
2. **Given** the split modules, **When** `kazoo.tests.integ.conftest` re-exports the ensemble fixtures and pytest hooks, **Then** the integration suite discovers and behaves identically to today.
3. **Given** `kazoo.testing.common`, **When** it is imported without a running Docker engine, **Then** no import-time or call-time behavior requires pytest or Docker except where inherently Docker-bound (and those functions degrade or raise clearly).

---

### User Story 3 - The dead top-level conftest is removed (Priority: P1)

A developer who opens `kazoo/tests/conftest.py` sees a file that contains only commented-out code and no active plugin behavior. Removing it simplifies the tree and eliminates a misleading artifact without changing any test run.

**Why this priority**: It is a trivial, safe deletion that reduces confusion; the file contributes nothing. It is grouped with the top priority because it is a pure deletion with no dependencies.

**Independent Test**: Delete `kazoo/tests/conftest.py`, update any configuration that references it, and run a representative slice of the integration and unit suites. Pytest must not warn about a missing plugin module and all tests run unchanged.

**Acceptance Scenarios**:

1. **Given** the commented-out `kazoo/tests/conftest.py`, **When** it is deleted and any references to it (e.g. in tooling configuration) are removed, **Then** pytest runs with no errors or warnings about the removed module.
2. **Given** the deletion, **When** the integration and unit suites run, **Then** collection, markers, and outcomes are unchanged from before.

---

### User Story 4 - Comments and docstrings document only the present (Priority: P2)

A Kazoo contributor reading the test code and the testing framework wants documentation that states what each piece *is* and *does today*. It must not narrate history ("it used to X, but now we Y", "formerly", "migrated from the legacy ...") and must not reference external planning artifacts (plan IDs such as US1/FR-007/R-04/SC-005, "the plan", quickstart references). Clean, present-tense documentation reduces confusion and keeps the codebase self-explanatory.

**Why this priority**: Lower priority than the structural changes because it is a sweeping, mechanical cleanup, but it is a hard requirement of this feature: the current tree is full of such references.

**Independent Test**: Grep the `kazoo/tests` and `kazoo.testing` source for plan-reference patterns and historical-narrative phrases; the grep must come back empty. Spot-read modified docstrings to confirm they describe current behavior.

**Acceptance Scenarios**:

1. **Given** the current tests and testing framework, **When** a search is run for Speckit plan references (e.g. `US\d+`, `FR-\d+`, `R-\d+`, `SC-\d+`, `quickstart`, "the plan") across **every file** under `kazoo/tests` and `kazoo.testing` — Python, YAML, Dockerfiles, entrypoint scripts, and JAAS configs — **Then** zero matches remain.
2. **Given** the current tests and testing framework, **When** docstrings/comments are reviewed for historical narrative (e.g. "used to", "formerly", "legacy ... removed", "was removed (see CHANGES.md)"), **Then** they describe only current behavior.
3. **Given** the cleanup, **When** a documentation reader inspects any public function/fixture/module docstring, **Then** it explains what the thing is and how it behaves now, without external plan references.

---

### User Story 5 - The extracted business logic is extensively unit-tested (Priority: P2)

A Kazoo maintainer wants the harness's business logic to be covered by fast, deterministic unit tests that need no Docker daemon, no pytest plugins, and no live ZooKeeper. `kazoo/tests/unit/test_testing.py` exercises the pure logic extracted into `kazoo.testing.common`: axis resolution (CLI/env defaults), marker evaluation and skip decisions, JVM flag mappings, mount-path translation for remote daemons, member-service name mapping, client-option derivation per auth axis, and TLS-keylog assembly.

**Why this priority**: The unit-test suite is the payoff of the US2 split; it makes the harness logic verifiable in isolation and guards regressions cheaply in CI.

**Independent Test**: Run `pytest kazoo/tests/unit/test_testing.py` in an environment with no Docker engine available; it must pass in seconds, exercising the pure business logic with synthetic inputs.

**Acceptance Scenarios**:

1. **Given** the extracted `kazoo.testing.common`, **When** the unit suite runs without Docker, **Then** every pure function and branch is exercised by `kazoo/tests/unit/test_testing.py` and passes.
2. **Given** a code change to a business-logic function, **When** the unit suite runs, **Then** regressions are caught by the new tests rather than requiring a live ensemble run.

---

### Edge Cases

- Relocated compose files use relative bind-mount sources (`./jaas/...`) interpolated through `${ZK_COMPOSE_DIR}`: the new resource location must be translated to a daemon-visible mount path exactly as today (including on Windows-remote `DOCKER_HOST` setups).
- The compose context must keep resolving to the on-disk location of the *installed* package (via importlib.resources), not a hardcoded path tied to a source checkout.
- Marker/skip decisions must be byte-identical before and after the module split and comment cleanup — no behavioral drift in `pytest_collection_modifyitems` or the autouse version fixture.
- The `KazooZkEnv`/`ZkEnsemble` frozen dataclasses are public-ish API used by integration tests; their construction and field names must not change.
- `kazoo.tests.conftest` is referenced by tooling configuration (e.g. mypy override list); removing the module must not leave dangling references.
- Docs (`docs/testing.rst`, `docs/api/testing.rst`) reference `kazoo.testing.kazoo_ensemble`; they must be updated to the new module layout so doc builds keep resolving.
- A plan-reference/history grep must not be confused by legitimate technical terms that merely look similar (e.g. a test for real "legacy" SASL string-form behavior): such legitimate content is kept, while *narrative* references are removed.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The testing framework resources that are not test data — the JAAS configs, the sidecar Dockerfiles, and every docker-compose overlay currently under `kazoo/tests/integ/` — MUST live under `kazoo.testing`, co-located with the harness code that consumes them.
- **FR-002**: The harness MUST resolve its compose context and all relative resource references (`./jaas/...`, `dockerfiles/capture`, `dockerfiles/certgen`, `dockerfiles/kdc`, `dockerfiles/tls-secrets-agent`) from the new location, via the installed package (not a source-tree-relative path).
- **FR-003**: After the move, the integration suite MUST provision stacks identically: same compose overlay resolution, same JAAS mounts, same sidecar image builds, same interpolation variables, and identical pass/skip/fail outcomes on every auth/feature axis.
- **FR-004**: The relocated resources MUST remain included in both wheel and sdist package data so installed-distribution test runs resolve them.
- **FR-005**: `kazoo/tests/conftest.py` MUST be deleted, along with every reference to it in tooling/configuration, without affecting pytest collection, markers, or plugin behavior.
- **FR-006**: `kazoo/testing/kazoo_ensemble.py` MUST be split into exactly two modules: `kazoo.testing.common` holding all business logic (axes and flag mappings, ensemble/client logic, Docker/mount helpers, keylog and Kerberos assembly, marker evaluation, axis resolution), and `kazoo.testing.fixtures` holding the pytest fixtures and plugin hooks as minimal delegation wrappers plus documentation.
- **FR-007**: The public surface re-exported through the integration `conftest.py` (fixtures `docker_env`, `docker_compose`, `zkensemble`, `zkchroot`, `zkclient`, `zksuperadmin_client`, `check_skip_version_marker`, and the `pytest_*` hooks) MUST remain importable under the same names from the new modules.
- **FR-008**: Comments and docstrings in **every file** under `kazoo/tests/` and `kazoo.testing` (Python, YAML, Dockerfiles, entrypoint scripts, JAAS configs) MUST contain no Speckit plan references (e.g. `US\d+`, `FR-\d+`, `R-\d+`, `SC-\d+`, "the plan", quickstart references).
- **FR-009**: Comments and docstrings in `kazoo/tests/` and `kazoo.testing` MUST describe current behavior only — no historical narrative ("it used to X but now we Y", "formerly", "migrated from the legacy ...") — except where a *behavioral* compatibility note is itself the subject (e.g. a test asserting a legacy string-form is still accepted).
- **FR-010**: `kazoo/tests/unit/test_testing.py` MUST provide **100% branch coverage of every pure function** in `kazoo.testing.common`, runnable without a Docker engine or live ZooKeeper, covering axis resolution, marker evaluation/skip reasons, JVM flag mappings, mount-path translation, member-service name mapping, per-axis client-option derivation, and TLS-keylog assembly.
- **FR-011**: The refactor MUST NOT change any test outcome, skip decision, marker registration, or client-visible harness behavior relative to the current branch baseline.

### Key Entities

- **Testing resource**: The non-test artifacts owned by the framework — the docker-compose overlays (`docker-compose.base.yml`, `docker-compose.auth-*.yml`, `docker-compose.features-capture.yml`), the JAAS server configs, and the sidecar Dockerfile trees — currently in `kazoo/tests/integ/`, to be owned by `kazoo.testing`.
- **Business-logic module (`kazoo.testing.common`)**: The extracted harness logic (axes, flag mappings, ensemble/client helpers, Docker/mount/keylog/Kerberos helpers, marker evaluation, axis resolution), independent of pytest fixtures and Docker at import time.
- **Fixtures module (`kazoo.testing.fixtures`)**: The pytest fixtures and plugin hooks as minimal, documented wrappers delegating to `common`.
- **Harness unit suite (`kazoo/tests/unit/test_testing.py`)**: The new fast, Docker-free unit tests for `common`.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of the integration suite's pass/skip/fail outcomes on the plain axis are identical before and after the refactor; the same holds on at least one representative auth axis (e.g. tls) and the capture axis.
- **SC-002**: 100% of the relocated resources resolve from an installed `kazoo.testing` package (verified from both a built wheel and an sdist) when running the integration suite.
- **SC-003**: A pattern search for plan references across **every file** in `kazoo/tests/` and `kazoo.testing` (Python, YAML, Dockerfiles, entrypoint scripts, JAAS configs) returns zero matches.
- **SC-004**: A review of changed docstrings/comments finds no historical narrative describing how things "used to" work, only current behavior.
- **SC-005**: `pytest kazoo/tests/unit/test_testing.py` passes in an environment with no Docker engine available, in under a few seconds, and achieves 100% branch coverage of every pure function in `common.py`.
- **SC-006**: `kazoo_ensemble.py` and `kazoo/tests/conftest.py` no longer exist, and no code, configuration, or documentation references the removed modules in a way that breaks imports, mypy, or the docs build.
- **SC-007**: `flake8`, `black`, and strict `mypy` pass on the new and modified modules per the project's quality gates.

## Assumptions

- `kazoo/tests/integ/conftest.py` stays in place (it is the active re-export/conftest for the integration suite); only `kazoo/tests/conftest.py` (fully commented out) is removed.
- The pytest plugin hooks (`pytest_addoption`, `pytest_configure`, `pytest_collection_modifyitems`, `pytest_sessionfinish`) live in `kazoo.testing.fixtures` as thin wrappers over the business logic (marker evaluation, axis resolution, capture probing) in `kazoo.testing.common` (per the session clarification).
- The integration test *cases* stay in `kazoo/tests/integ/`; only the framework-owned resources move. The `--zk-*` CLI surface and fixture names are unchanged.
- Documentation (`docs/testing.rst`, `docs/api/testing.rst`) and tooling configuration (mypy override list) are updated as companion changes so nothing references `kazoo.testing.kazoo_ensemble` or `kazoo.tests.conftest`.
- "Extensive" unit coverage means **100% branch coverage of every pure function** in `common.py` (per the session clarification); Docker-bound helpers are outside the pure-function set and are exercised via synthetic/mocked inputs where deterministic, with their non-Docker code paths fully covered.
- The comment/docstring cleanup applies to **every file** under `kazoo.testing` and `kazoo/tests` — Python, YAML, Dockerfiles, entrypoint scripts, and JAAS configs (per the session clarification), including the resources relocated in US1.
- Legitimate technical references to "legacy" behavior (e.g. tests asserting that a legacy SASL string form is still accepted, or the `skip_if_zk_version` legacy marker's continued operation) remain, because they document current behavior; only narrative/historical and plan-referencing prose is removed.

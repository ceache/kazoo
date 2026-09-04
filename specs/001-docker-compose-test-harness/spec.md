# Feature Specification: Docker-Compose Test Harness

**Feature Branch**: `001-docker-compose-test-harness`

**Created**: 2026-08-14

**Status**: Draft

**Input**: User description: "modernize the kazoo.testing harness to use docker-compose instead of hand rolling zookeeper configurations. The harness will now run 'official Zookeeper images' from hub.docker.com/_/zookeeper, leverage the interfaces the Zookeeper folks implemented in those images and relieve us from managing JVM flags and zookeeper configurations as much as possible. A lot of hand rolled code will then go away and the testing will be truly multiplatform (as long as the platform provides a docker-compose compatible cli). Look at PYTEST_INTEG.md for the general plan and COMPOSE-STRATEGY.md for how to lay out the compose files. I have started to implement this on this branch, the tests under kazoo/tests/integ/ already use a docker-compose based solution. One thing to consider for authentication: Zookeeper supports TLS server validation with GSSAPI client authentication happening in that secure tunnel."

## Clarifications

### Session 2026-08-14

- Q: How many ZooKeeper server nodes should the compose harness provision, and must that count be configurable per run? → A: Fixed 3-node ensemble (participating servers); not configurable in this feature.
- Q: Should this feature migrate the CI workflows to the compose-based path, and what test matrix should CI run given the current CI tests Python 3.8–3.14 and pypy across ZooKeeper 3.6.4–3.9.1 while the new harness supports only 3.7–3.9? → A: Migrate CI to the compose path with a tiered matrix: all supported Python versions × ZK 3.7/3.8/3.9, plus auth and feature axes on the latest Python target; ZooKeeper 3.6 is retired from CI.
- Q: For the SASL GSSAPI test flavor, where should the Kerberos KDC — the trust root for GSSAPI tests — come from? → A: In-repo KDC Dockerfile build based on Alpine Linux; principals and config versioned with the repo, no third-party image trust.
- Q: Which KDC setup should the in-repo Alpine KDC be modeled on? → A: The harness's KDC image lives in-repo at `kazoo/tests/integ/dockerfiles/kdc/` (Alpine-based: KDC daemon setup plus keytab generation for configured service principals).

### Session 2026-08-14 (plan refinement)

- Q: Should the compose orchestration driver be switched from pytest-docker to the testcontainers-python package? → A: Yes — adopt **testcontainers-python** (`testcontainers.compose.DockerCompose`) as the orchestration driver (FR-014). pytest-docker is dropped.
- Q: testcontainers 4.x requires Python >= 3.9.2, but the current support matrix still includes Python 3.8 (EOL). How to reconcile? → A: Adopt testcontainers 4.x and **drop Python 3.8 from the test support matrix** (setup.cfg classifiers, CI tiered matrix); the harness requires Python >= 3.9. The drop is recorded as part of the documented breaking change (FR-010 / CHANGES.md).
- Q: Compose layout — per-auth single files or the layered overlay approach from COMPOSE-STRATEGY.md? → A: Adopt the layered **base + auth overlay** layout, now viable because `DockerCompose` accepts a list of compose files.

---

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Run integration tests against a docker-compose-managed ZooKeeper cluster (Priority: P1)

A Kazoo developer (or CI runner) invokes the integration test suite. The testing harness provisions a ZooKeeper cluster from the official ZooKeeper container images (hub.docker.com/_/zookeeper) using a docker-compose compatible CLI, health-checks the cluster, runs the tests, and tears the cluster down — all without the developer installing a ZooKeeper binary or managing JVM flags and configuration files by hand.

**Why this priority**: This is the core of the modernization. Every other benefit (multiplatform, less maintenance, fewer configuration-flake failures) flows from replacing hand-rolled process management with the official image and docker-compose. Without this, nothing else in the feature exists.

**Independent Test**: Run the integration suite on a machine with only Python and a docker-compose compatible CLI installed (no ZooKeeper binary, no Java toolchain). The suite must provision the cluster from the official image, pass, and tear down cleanly.

**Acceptance Scenarios**:

1. **Given** a developer machine with a docker-compose compatible CLI but no local ZooKeeper install, **When** the developer runs the integration test suite, **Then** the harness provisions the cluster from the official images, waits until it is healthy, runs the tests, and removes the cluster afterwards.
2. **Given** a test session, **When** the harness starts, **Then** exactly one cluster is provisioned for the whole session and shared by all tests, instead of one cluster per test.
3. **Given** a failing test, **When** it fails, **Then** the harness surfaces the relevant cluster logs to aid diagnosis.

---

### User Story 2 - Run the suite across versions, auth schemes, and feature sets (Priority: P1)

A test author selects the ZooKeeper version, authentication scheme, and feature set for a run (via CLI flags or environment variables). The harness materializes a matching cluster (base compose definition plus auth and feature overlays), and tests that declare incompatible environment requirements are skipped with a clear reason rather than failing.

**Why this priority**: The three testing axes — ZooKeeper version, authentication scheme, and feature flags — are the organizing principle of the plan documents (PYTEST_INTEG.md, COMPOSE-STRATEGY.md). They enable CI coverage of the full matrix.

**Independent Test**: Run the same test file against two different versions and two different auth schemes; incompatible tests must be skipped while compatible tests pass.

**Acceptance Scenarios**:

1. **Given** a run configured for ZooKeeper 3.8 with SASL digest auth, **When** a test declares it requires ZooKeeper 3.9 or later, **Then** the test is skipped with a clear reason instead of failing.
2. **Given** a run configured for plain (no) auth, **When** a test declares it requires GSSAPI, **Then** the test is skipped with a clear reason.
3. **Given** a run with the TTL feature enabled, **When** a test requires the TTL feature, **Then** it runs; **When** a test is incompatible with the TTL feature, **Then** it is skipped.

---

### User Story 3 - Exercise the security matrix via the official image interfaces (Priority: P2)

A security-focused test author verifies authentication and authorization behavior (digest, SASL digest, SASL GSSAPI, TLS) against a cluster configured entirely through the official image's public interfaces (JAAS configuration, keystores/truststores, extra server config, JVM flags). The combined mode — TLS server validation with GSSAPI client authentication inside the secure tunnel — is part of the design.

**Why this priority**: Authentication and authorization correctness is a core requirement of the project. The current hand-rolled TLS certificate generation and JAAS wiring is precisely the fragile code this feature retires.

**Independent Test**: Run the SASL and TLS integration tests against a compose-provisioned cluster; valid authentication handshakes must succeed and negative tests must reject wrong credentials.

**Acceptance Scenarios**:

1. **Given** a cluster configured for digest auth, **When** a client authenticates with the correct credentials, **Then** the session is authorized; **When** a client presents wrong credentials, **Then** authentication fails.
2. **Given** a cluster configured for SASL GSSAPI, **When** a client authenticates via Kerberos, **Then** the session is established.
3. **Given** a TLS cluster, **When** a client connects with a valid client certificate and validates the server, **Then** the session is established; **When** the certificate is invalid, **Then** the connection is rejected.
4. **Given** the combined TLS + GSSAPI mode (a supported configuration of this feature), **When** a client authenticates, **Then** GSSAPI authentication happens inside the TLS-validated tunnel.

---

### User Story 4 - Truly multiplatform testing (Priority: P2)

Anyone with a docker-compose compatible CLI runs the same suite on Linux, macOS, or Windows. Platform-specific pain (Java installs, classpath globbing, path separators, per-OS ZooKeeper setup scripts) disappears because the ZooKeeper runtime lives in the official container.

**Why this priority**: "Truly multiplatform" is an explicit goal. It removes the Windows sanity-test special-casing and per-OS Java/ZooKeeper installation requirements.

**Independent Test**: Run the integration suite on Linux, macOS, and Windows hosts that each have a docker-compose compatible CLI; all three platforms pass with no platform-specific harness code.

**Acceptance Scenarios**:

1. **Given** a Windows machine with a docker-compose compatible CLI, **When** the integration suite runs, **Then** it provisions the cluster and passes without a local Java or ZooKeeper installation.
2. **Given** a macOS machine, **When** the suite runs, **Then** it behaves identically to Linux with no platform-specific code paths in the harness.

---

### User Story 5 - Retire the hand-rolled ZooKeeper management code (Priority: P3)

The legacy harness components that download binaries, resolve classpaths, generate `zoo.cfg` / log4j files, launch Java processes, and generate TLS certificates in-process are removed. All tests that still use the legacy harness — including the remaining legacy `KazooTestCase`/`KazooTestHarness`-based tests under `kazoo/tests/` — are migrated to the new fixtures, and the legacy public API is removed outright as a documented breaking change.

**Why this priority**: Removing the hand-rolled code is an explicit goal ("a lot of hand rolled code will then go away"). It is lower priority than P1/P2 because it is the cleanup that follows the new harness working.

**Independent Test**: Inspect the repository — no legacy process-management or certificate-generation code remains — and confirm the full integration suite still passes with equal coverage.

**Acceptance Scenarios**:

1. **Given** the modernized harness, **When** the test suite runs, **Then** no code that launches Java processes or generates ZooKeeper config files directly is invoked.
2. **Given** the legacy harness removal, **When** the full integration suite runs, **Then** all previously covered behaviors remain covered.

---

### Edge Cases

- docker-compose CLI or Docker daemon unavailable at run time → the harness fails fast, before running tests, with a clear, actionable message.
- First run requires pulling images → the startup and health-check budget accounts for image-pull time.
- Host port conflicts → the cluster binds ephemeral host ports.
- A test crashes mid-session → the cluster is still torn down at session end and logs are retained.
- Failure-injection tests stop/start individual nodes → the harness re-establishes quorum/health before proceeding.
- Auth sidecars (KDC, cert generator, JAAS writer) not ready → the ZooKeeper service waits for sidecar readiness before starting.
- Stale containers or state from a previous interrupted run → the harness cleans up or namespaces its resources so a new session starts clean.
- Healthcheck tooling differs across image versions → the harness relies only on commands guaranteed present in the official image.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The testing harness MUST provision ZooKeeper clusters from the official ZooKeeper container image published on Docker Hub, without requiring a locally installed ZooKeeper binary.
- **FR-002**: The harness MUST configure clusters exclusively through the official image's public interfaces (environment variables for server id, server list, extra configuration, and JVM flags), never by writing configuration files or launching Java directly.
- **FR-003**: The harness MUST allow the ZooKeeper version to be selected per run and MUST support the 3.7, 3.8, and 3.9 series.
- **FR-004**: The harness MUST allow the authentication scheme to be selected per run, covering plain, digest, SASL digest, SASL GSSAPI, and TLS.
- **FR-005**: The harness MUST allow server feature toggles to be selected per run (standard, TTL, read-only, reconfig).
- **FR-006**: The harness MUST start the cluster once per test session, health-check it before running any tests, and tear it down at session end.
- **FR-007**: The harness MUST provide each test with an isolated namespace on the shared cluster and MUST clean it up during teardown.
- **FR-008**: Tests MUST be able to declare required environment constraints (version range, auth schemes, required and forbidden features), and the harness MUST skip incompatible tests with an actionable reason.
- **FR-009**: The harness MUST let tests stop and restart individual cluster nodes to support failure-injection and recovery scenarios.
- **FR-010**: The legacy harness code that downloads ZooKeeper binaries, resolves classpaths, generates configuration and log files, launches Java processes, and generates TLS certificates in-process MUST be removed. ALL tests currently using the legacy `KazooTestCase` / `KazooTestHarness` API MUST be migrated to the new fixtures with no loss of coverage, and the legacy public API MUST be removed outright, recorded as a documented breaking change in the project's changelog.
- **FR-011**: The harness MUST run identically on Linux, macOS, and Windows given only a docker-compose compatible CLI, with no platform-specific code paths.
- **FR-012**: For SASL GSSAPI, the harness MUST support GSSAPI client authentication inside a TLS-validated connection (TLS server validation with GSSAPI client auth in the tunnel) as a supported configuration of the auth matrix.
- **FR-013**: All credentials, keytabs, and certificates used by the harness MUST be throwaway test values and MUST NOT be committed, logged, or exposed in test output.
- **FR-014**: The harness's runtime test dependencies (including the docker-compose orchestration driver) MUST be declared so a fresh environment installs everything needed for the test suite.
- **FR-015**: On test failure, the harness MUST surface the relevant cluster logs to aid diagnosis.
- **FR-016**: The harness MUST provision a fixed 3-node ZooKeeper ensemble (three participating servers) for every test session; the node count is not configurable per run in this feature.
- **FR-017**: The CI workflows MUST be migrated to the compose-based path using a tiered matrix: all supported Python versions (3.9 and later, per the plan-refinement clarification — 3.8 is dropped) and pypy run against ZooKeeper 3.7, 3.8, and 3.9, with the authentication and feature axes exercised on the latest Python target; ZooKeeper 3.6 is retired from the CI test matrix, and CI no longer installs ZooKeeper binaries, Java, or apt-installed Kerberos packages on the runner.
- **FR-018**: For the SASL GSSAPI flavor, the Kerberos KDC MUST be built from an in-repo Dockerfile based on Alpine Linux living in `kazoo/tests/integ/dockerfiles/kdc/` — KDC daemon setup plus keytab generation for the configured service principals — with principals and configuration versioned in the repository, so the GSSAPI trust root is reproducible and auditable without depending on a third-party image.

### Key Entities *(include if feature involves data)*

- **Test Run Configuration**: The resolved triple (ZooKeeper version, auth scheme, feature set) selected for a run via CLI flags or environment variables; drives compose interpolation and test skipping.
- **ZooKeeper Ensemble**: The fixed set of three cluster nodes provisioned via docker-compose for a session; exposes client endpoints to tests and supports per-node stop/start.
- **Compose Stack**: The layered compose definition (base definition + auth overlay + feature overlays) that materializes a given configuration from the official image.
- **Test Namespace (Chroot)**: A unique per-test path within the ensemble that isolates test data and is removed at teardown.
- **Test Constraint Marker**: A declarative annotation on a test expressing required version/auth/features; evaluated by the harness to skip incompatible tests.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of the existing integration test suite runs against a compose-provisioned cluster with no local ZooKeeper binary or Java required on the host.
- **SC-002**: Every supported ZooKeeper version (3.7, 3.8, 3.9) can be selected with a single parameter change and requires no test-code changes.
- **SC-003**: Zero hand-rolled process management remains in the harness: no generated ZooKeeper configuration files, no direct Java launches, and no in-process TLS certificate generation.
- **SC-004**: The identical test invocation passes on Linux, macOS, and Windows hosts that have a docker-compose compatible CLI, with no platform-specific harness branches.
- **SC-005**: 100% of environment-incompatible tests are skipped (never fail) with actionable reasons.
- **SC-006**: 100% of per-test namespaces are cleaned up after each test, leaving no residual data between runs.
- **SC-007**: Continuous integration no longer downloads or installs ZooKeeper binaries; container provisioning is the sole runtime path, using a tiered matrix (all supported Python versions 3.9+ × ZK 3.7/3.8/3.9, with auth and feature axes on the latest Python target).

## Assumptions

- The official ZooKeeper image (hub.docker.com/_/zookeeper) is the runtime for all tests; its environment-variable interfaces (server id, server list, extra config, JVM flags) and 4-letter-word-based healthcheck support remain available across the 3.7–3.9 series.
- "Multiplatform" means any operating system with a docker-compose compatible CLI; it does not imply support on hosts without a container runtime.
- Test isolation via per-test namespaces on a single shared ensemble is preferred over per-test clusters, amortizing startup cost once per session.
- All authentication credentials are throwaway test values (e.g. `super`/`super_secret`); no real secrets exist in the harness.
- The Kerberos KDC used for GSSAPI tests is built in-repo from an Alpine-based Dockerfile (per FR-018) at `kazoo/tests/integ/dockerfiles/kdc/`; no third-party KDC image is used as the auth trust root. Its entrypoint writes `krb5.conf`, creates the principal database, adds the configured service principals (SPNS), exports their keytabs to the KDC data volume, and starts `krb5kdc`.
- The compose orchestration driver is **testcontainers-python** (`testcontainers.compose.DockerCompose`, version 4.x), which drives the modern `docker compose` (v2) CLI with multi-file overlay support and healthcheck-driven readiness (`up --wait`). Because testcontainers 4.x requires Python >= 3.9.2, the harness and its test environment target Python 3.9+; Python 3.8 is removed from the support matrix as a consequence.
- Migrating continuous integration to the compose-based path is in scope, as CI is the primary consumer of the multiplatform benefit. The CI matrix is tiered per PYTEST_INTEG.md: all supported Python versions (3.9+ and pypy) × ZK 3.7/3.8/3.9, with auth and feature axes on the latest Python target; ZooKeeper 3.6 is retired from the CI test matrix as a consequence, and Python 3.8 is dropped from the support matrix because the testcontainers 4.x driver requires Python >= 3.9.
- All remaining legacy tests under `kazoo/tests/` that use `KazooTestCase` / `KazooTestHarness` are migrated to the new fixtures as part of this feature, and the removal of the legacy public API is treated as a documented breaking change per the project's backward-compatibility policy.
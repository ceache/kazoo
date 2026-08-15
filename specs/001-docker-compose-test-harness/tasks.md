---

description: "Task list for the docker-compose test harness implementation"
---

# Tasks: Docker-Compose Test Harness

**Input**: Design documents from `/specs/001-docker-compose-test-harness/`

**Prerequisites**: plan.md (required), spec.md (required for user stories), research.md, data-model.md, contracts/

**Tests**: The feature's user stories define runnable validation scenarios (quickstart.md V1–V10) and the spec's "Independent Test" per story. Dedicated test-authoring tasks are included only where the spec requires them (negative auth tests in US3). All other phases use verification tasks against the existing integration suite.

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

## Path Conventions

- **Single project**: `kazoo/`, `setup.cfg`, `.github/` at repository root
- Harness code: `kazoo/testing/` · compose/KDC artifacts: `kazoo/tests/integ/` · CI: `.github/workflows/testing.yml`

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Declare the new driver and adjust packaging so every subsequent phase can import it.

- [x] T001 Update `setup.cfg` `[options.extras_require] test`: add `testcontainers>=4,<5` (orchestration driver, requires Python >= 3.9 per R-01/R-10); remove `pyjks` and `pyOpenSSL` from the `test` extra; remove the `Programming Language :: Python :: 3.8` classifier (Python 3.8 dropped from the support matrix per plan-refinement clarification; test env requires 3.9+). **Companion change (required so the still-present legacy suite keeps importing)**: convert the top-level `import jks` / `import OpenSSL` in `kazoo/testing/common.py` (lines 37–38) into lazy imports inside `perform_ssl_certs_generation()` that raise a clear `ImportError` when those modules are absent — `kazoo/testing/__init__.py` → `harness.py` → `common.py`, so the module-level imports would otherwise break `import kazoo.testing` on a fresh 3.9+ env before Phase 7 deletes `common.py`. The cert-gen path has no active callers (only commented-out references in `kazoo/tests/integ/test_client.py`). Leave `pyOpenSSL` in the `typing` extra untouched (reassessed in T040).

**Checkpoint**: `pip install -e '.[test]'` resolves `testcontainers` (and `docker`, `python-dotenv`, `requests`, `wrapt` transitively) and drops `pyjks`/`pyOpenSSL`.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Swap the orchestration driver from pytest-docker to testcontainers `DockerCompose` and stand up the plain-flavor session cluster. **MUST complete before ANY user story.**

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [x] T002 Refactor `kazoo/tests/integ/docker-compose.yml` → `kazoo/tests/integ/docker-compose.base.yml`: replace the hardcoded `SERVER_JVMFLAGS` (superDigest + extendedTypes) with `${ZK_FEATURES_JVMFLAGS} ${ZK_AUTH_JVMFLAGS}` interpolation (keep `-Dzookeeper.root.logger=INFO,CONSOLE,ROLLINGFILE`); keep ephemeral `0:2181` ports, tmpfs `/data`+`/datalog`, `${ZK_WORK_DIR}/logs` mounts, 4LW whitelist, and `srvr` healthcheck; tune healthcheck per R-11 (interval ~3s, timeout ~3s, retries 15–20); delete the commented-out `kdc`/`dev` blocks (KDC moves to the sasl_gssapi overlay in US3)
- [x] T003 Rework `docker_compose_config` in `kazoo/tests/integ/conftest.py` to return a `compose_files` list = `["docker-compose.base.yml"]` + (`["docker-compose.auth-<auth>.yml"]` when auth != `plain`) and export `ZK_VERSION`/`ZK_AUTH`/`ZK_FEATURES`/`ZK_FEATURES_JVMFLAGS`/`ZK_AUTH_JVMFLAGS`/`COMPOSE_PROJECT_NAME`; delete the `docker_compose_file` fixture (pytest-docker-specific)
- [x] T004 Extend `_resolve_axis_options`/`docker_env` in `kazoo/testing/kazoo_ensemble.py`: compute and export `ZK_AUTH_JVMFLAGS` (`-Dzookeeper.DigestAuthenticationProvider.superDigest="super:D/InIHSb7yEEbrWz8b9l71RjZJU="` for `digest`, empty otherwise) and a session-unique `COMPOSE_PROJECT_NAME=kazoo-<uuid8>`; add a session-scoped `docker_compose` fixture wrapping `testcontainers.compose.DockerCompose(context=kazoo/tests/integ, compose_file_name=config["compose_files"])` with `start()` (`docker compose up --wait`, healthcheck-driven readiness) at setup and `stop()` (`down --volumes`) at teardown
- [x] T005 Rewrite `ZkEnsemble` and the `zkensemble` fixture in `kazoo/testing/kazoo_ensemble.py`: replace the `docker_ip`/`docker_services.port_for(...)` dependencies with `compose.get_service_host("zooN", 2181)` / `compose.get_service_port("zooN", 2181)`; change the `docker_services: Services` field to `compose: DockerCompose`; implement `_run_compose(*args)` using `compose.compose_command_property` and route `stop("zooN")`/`start("zooN")` through it (per contracts/fixtures.md)
- [x] T006 Add the log-dump hook (FR-015) in `kazoo/tests/conftest.py` (or `kazoo_ensemble.py`): on test failure (`pytest_exception_interact`/`pytest_sessionfinish`), print `compose.get_logs("zoo1", "zoo2", "zoo3", ...)` stdout/stderr to the report
- [x] T007 Add a failure-fast guard in `kazoo/testing/kazoo_ensemble.py`: before the session cluster starts, detect a missing/unusable `docker compose` CLI or daemon and fail with a clear, actionable message (edge case in spec.md)

**Checkpoint**: `pytest kazoo/tests/integ/test_client.py -k "test_create" -v` brings up the plain 3-node ensemble from `zookeeper:3.9.4` via testcontainers, waits for health, runs, and tears down cleanly (quickstart V1).

---

## Phase 3: User Story 1 - Run integration tests against a docker-compose-managed ZooKeeper cluster (Priority: P1) 🎯 MVP

**Goal**: The existing integration suite runs against the official-image cluster on a host with only Python + a docker-compose CLI; one cluster per session; clean teardown; cluster logs surfaced on failure.

**Independent Test**: `pytest kazoo/tests/integ -q` on a Docker-only host (no ZooKeeper binary, no Java) — cluster provisions, healthchecks pass, tests pass, `docker ps` shows no leftover harness containers after the run (quickstart V1, V7).

### Implementation for User Story 1

- [x] T008 [US1] Verify the 6 already-migrated files (`kazoo/tests/integ/test_client.py`, `test_connection.py`, `test_watchers.py`, `test_cache.py`, `test_counter.py`, `test_barrier.py`) pass on the testcontainers driver under plain auth; fix any fixture-usage drift from the `docker_services` removal
- [x] T009 [US1] Guarantee teardown on failure: ensure `compose.stop()` (`down --volumes`) runs at session end even when tests crash mid-session; confirm no stale containers/volumes remain (spec edge case)
- [x] T010 [P] [US1] Add a session-level readiness assertion in `kazoo/testing/kazoo_ensemble.py` that `compose.get_container("zoo1").Health == "healthy"` (belt-and-suspenders beyond `up --wait`) before any test runs (FR-006)

**Checkpoint**: User Story 1 is fully functional and testable independently — the baseline plain-flavor run is green end-to-end.

---

## Phase 4: User Story 2 - Run the suite across versions, auth schemes, and feature sets (Priority: P1)

**Goal**: Version/auth/feature axes selectable per run via CLI/env; incompatible tests skipped with actionable reasons.

**Independent Test**: `pytest kazoo/tests/integ --zk-version=3.8.3 --zk-auth=digest -q` and `--zk-features=ttl,reconfig -q` — same test file, no test-code edits; version- or auth-incompatible tests are **skipped** with a clear reason, never failed (quickstart V2, V3, V4).

### Implementation for User Story 2

- [x] T011 [US2] Create `kazoo/tests/integ/docker-compose.auth-digest.yml` as a minimal overlay (digest is configured purely through `ZK_AUTH_JVMFLAGS` interpolation in the base file; the overlay documents the flavor selection and is the file `docker_compose_config` resolves for `--zk-auth=digest`)
- [x] T012 [P] [US2] Validate the version axis (SC-002): run V2 with `--zk-version=3.8.3` and `3.7.2`, confirming `zookeeper:${ZK_VERSION}` interpolation in `docker-compose.base.yml` selects the tag; fix any base-file incompatibilities across 3.7–3.9
- [x] T013 [US2] Wire the feature axis into the compose path: confirm `ZK_FEATURES_JVMFLAGS` (from `FEATURE_JVM_PROPERTIES` in `kazoo/testing/kazoo_ensemble.py`: ttl/readonly/reconfig JVM props) renders into `SERVER_JVMFLAGS` via the base file interpolation (FR-005)
- [x] T014 [P] [US2] Implement the richer skip-marker surface from contracts/markers.md in `kazoo/testing/kazoo_ensemble.py`: `@pytest.mark.zk_version("<3.8")`, `@pytest.mark.zk_auth("digest", "tls")`, `@pytest.mark.zk_features(require=[...], skip=[...])`; register them in `pytest_configure` alongside the existing `skip_if_zk_version`
- [x] T015 [US2] Implement collection-time skip evaluation (`pytest_collection_modifyitems`) against the active `Test Run Configuration` in `kazoo/testing/kazoo_ensemble.py`, producing actionable skip reasons (FR-008, SC-005); keep the existing per-test `check_skip_version_marker` autouse fixture working
- [x] T016 [US2] Add auth-axis skip integration: tests declaring `zk_auth("sasl_gssapi")` (or requiring GSSAPI) are skipped on plain/digest runs; verify against the `test_sasl.py`/auth scenarios from PYTEST_INTEG.md

**Checkpoint**: User Stories 1 AND 2 both work independently — axes selectable, skipping correct.

---

## Phase 5: User Story 3 - Exercise the security matrix via the official image interfaces (Priority: P2)

**Goal**: digest, SASL digest, SASL GSSAPI, and TLS flavors via official-image interfaces; combined TLS-validated tunnel with GSSAPI client auth (FR-012); positive + negative auth tests.

**Independent Test**: Run each flavor (`--zk-auth=digest|sasl_digest|tls|sasl_gssapi`); valid handshakes succeed, wrong credentials are rejected, and the GSSAPI run authenticates inside the TLS-validated tunnel (quickstart V3; spec User Story 3).

### Implementation for User Story 3

- [x] T017 [US3] Create `kazoo/tests/integ/docker-compose.auth-sasl-digest.yml`: JAAS `DigestLoginModule` server config mounted at `/conf/jaas.conf`, `JVMFLAGS=-Djava.security.auth.login.config=/conf/jaas.conf`, `ZOO_CFG_EXTRA` with `authProvider.1=org.apache.zookeeper.server.auth.SASLAuthenticationProvider` + `enforce.auth.enabled=true` + `enforce.auth.schemes=sasl` (per R-03; `requireClientAuthScheme` is legacy and never enforced in ZK 3.7+, use `enforce.auth.*`/`sessionRequireClientSASLAuth` instead — verified empirically on 3.9.4: bad DIGEST-MD5 creds now raise `SessionClosedRequireSaslError`)
- [x] T018 [P] [US3] Create the certgen sidecar under `kazoo/tests/integ/dockerfiles/certgen/` (Dockerfile + entrypoint script on `eclipse-temurin:17-jdk-jammy`): generate throwaway CA + server PKCS12 keystore/truststore + client PEM certs into `${ZK_WORK_DIR}/certs` bind mount; healthcheck = keystore files present; no host keytool/openssl (R-05, FR-013). Verified: entrypoint must keep the container alive after generating certs (`while :; sleep`) for `depends_on: condition: service_healthy` to succeed — an exiting container (even rc 0) never becomes healthy.
- [x] T019 [US3] Create `kazoo/tests/integ/docker-compose.auth-tls.yml`: certgen service with `depends_on: condition: service_healthy`, Netty `serverCnxnFactory`, ephemeral `0:<secureClientPort>`, `ssl.keyStore/trustStore` + `ssl.clientAuth=need` + `authProvider.1=X509AuthenticationProvider`, `ZOO_CFG_EXTRA` (R-03/R-09)
- [x] T020 [P] [US3] Create `kazoo/tests/integ/dockerfiles/kdc/Dockerfile` (Alpine port of `tmp/kdc/Dockerfile`): `FROM alpine`, `apk add krb5 krb5-server`, copy `root/` to `/`, `VOLUME /kdc-data`, `ENV SPNS`/`REALM`, `EXPOSE 1088`, `ENTRYPOINT ["/entrypoint.sh"]` (FR-018)
- [x] T021 [US3] Write `kazoo/tests/integ/dockerfiles/kdc/root/entrypoint.sh` in POSIX `sh` (Alpine has no bash): port of `tmp/kdc/root/entrypoint.sh` — write `krb5.conf` to `/kdc-data`, `kdb5_util create -s`, add SPNS principals (`client server/zoo1 server/zoo2 server/zoo3`) plus `zookeeper/127.0.0.1` + `zookeeper/localhost` (the SPNs the kazoo GSSAPI client requests), and export keytabs to `/kdc-data/keytabs` with `/`→`#` filenames, `chmod go+r`, start `krb5kdc -n` (R-04)
- [x] T022 [US3] Create `kazoo/tests/integ/docker-compose.auth-sasl-gssapi.yml`: KDC service (build `dockerfiles/kdc`, bind-mount `${ZK_WORK_DIR}` at `/kdc-data`) with a keytab-presence healthcheck + `depends_on: condition: service_healthy` on zoo nodes, JAAS `Krb5LoginModule` server config, and the TLS transport from the tls overlay (FR-012 combined mode; R-03/R-04/R-09); SASL enforcement via `enforce.auth.enabled` + `enforce.auth.schemes=sasl` (see T017). Verified end-to-end on 3.9.4 (see R-06 for the TCP/ccache/loopback-host client requirements).
- [x] T023 [US3] Extend `ZkEnsemble.get_client()` auth mapping in `kazoo/testing/kazoo_ensemble.py` per contracts/client-connection.md: `tls` → `use_ssl=True` + `certfile`/`keyfile`/`ca` from `${ZK_WORK_DIR}/certs`; `sasl_gssapi` → `use_ssl=True` (TLS tunnel) + `sasl_options={"mechanism": "GSSAPI"}` + `KRB5_CONFIG`/`KRB5_CLIENT_KTNAME`/`KRB5CCNAME` pointing at `${ZK_WORK_DIR}` (R-06); the `zkensemble` fixture normalizes the wildcard bind host to `127.0.0.1` and `_export_krb5_client_env` provisions a fresh per-run FILE ccache via `kinit -c`
- [x] T024 [US3] Add auth integration tests in `kazoo/tests/integ/test_auth.py` (or extend `test_sasl.py`): positive (valid credentials authenticate) and negative (wrong credentials rejected) cases for digest, sasl_digest, tls, and sasl_gssapi, gated by the US2 markers so they skip on incompatible runs. Verified: digest/sasl_digest/tls/sasl_gssapi each pass their 2 tests on 3.9.4. Also fixed `conftest.py` overlay-name mapping (`sasl_digest` → `docker-compose.auth-sasl-digest.yml`, underscore→hyphen) and `connection.py` to surface `SessionClosedRequireSaslError` as AUTH_FAILED (see T017/kazoo fix note).

**Checkpoint**: All security flavors run and authenticate correctly; the combined TLS+GSSAPI mode works end-to-end.

---

## Phase 6: User Story 4 - Truly multiplatform testing (Priority: P2)

**Goal**: Identical suite behavior on Linux, macOS, Windows with only a docker-compose CLI; no platform-specific harness code.

**Independent Test**: Run quickstart V1 on Linux, macOS, and Windows hosts — identical results, no platform branches in the harness (quickstart V9; spec User Story 4).

### Implementation for User Story 4

- [ ] T025 [US4] Audit `kazoo/testing/kazoo_ensemble.py`, `kazoo/tests/integ/conftest.py`, and compose files for platform-specific code: pathlib-only host-side paths, no `/bin/bash` assumptions on the host, `${ZK_WORK_DIR}` bind-mount paths passed to compose as-is; remove any platform branches (FR-011)
- [ ] T026 [P] [US4] Update the Windows sanity job in `.github/workflows/testing.yml`: drop the `actions/setup-java` step and ZK-install cache; run `pytest kazoo/tests/integ` (plain flavor) on `windows-latest` against the compose cluster
- [ ] T027 [US4] Verify ephemeral-port resolution and bind mounts work on Windows/macOS (`get_service_port` against `0:2181`; Docker Desktop path forwarding for `${ZK_WORK_DIR}`) and document any host prerequisites in quickstart.md (FR-011)

**Checkpoint**: Multiplatform claim is validated on all three OSes.

---

## Phase 7: User Story 5 - Retire the hand-rolled ZooKeeper management code (Priority: P3)

**Goal**: All 9 legacy-API test files migrated to the new fixtures; legacy harness modules, cert-gen code, and shell scripts removed; CI fully on the compose path; breaking change documented.

**Independent Test**: `pytest kazoo/tests/ -q` passes on the new fixtures; `git grep -E "KazooTestCase|KazooTestHarness|ZookeeperCluster"` returns nothing outside docs; `CHANGES.md` lists the breaking changes (quickstart V8; spec User Story 5).

### Implementation for User Story 5

- [ ] T028 [US5] Migrate `kazoo/tests/test_election.py` from `KazooTestCase` to the `zkclient`/`zkensemble` fixtures (swap `self.client` → `zkclient`, `self.cluster[i].stop()` → `zkensemble.stop("zooN")`, `self.expire_session` → `client.harness_expire_session`) with no coverage loss (R-08)
- [ ] T029 [P] [US5] Migrate `kazoo/tests/test_lock.py` and `kazoo/tests/test_queue.py` to the new fixtures (R-08)
- [ ] T030 [P] [US5] Migrate `kazoo/tests/test_party.py` and `kazoo/tests/test_partitioner.py` to the new fixtures (R-08)
- [ ] T031 [P] [US5] Migrate `kazoo/tests/test_lease.py` and `kazoo/tests/test_interrupt.py` to the new fixtures (R-08)
- [ ] T032 [P] [US5] Migrate `kazoo/tests/test_gevent_handler.py` to the new fixtures, using `zkensemble.get_client(handler=...)` (handler-specific; R-08)
- [ ] T033 [P] [US5] Migrate `kazoo/tests/test_sasl.py` to the new fixtures, mapping its classes to the `sasl_digest` and `sasl_gssapi` flavors (R-08/US3); delete the `KRB5_TEST_ENV`/`init_krb5.sh` GSSAPI setup paths
- [ ] T034 [US5] Delete `kazoo/testing/harness.py`, `kazoo/testing/common.py`, and strip the exports in `kazoo/testing/__init__.py` (legacy public API removal — breaking change, FR-010)
- [ ] T035 [US5] Delete `ensure-zookeeper-env.sh`, `init_krb5.sh`, and the root `docker-compose.yml` / `docker-compose-test.yml` (superseded by `kazoo/tests/integ/`)
- [ ] T036 [US5] Add a `BREAKING CHANGES` entry to `CHANGES.md`: legacy `kazoo.testing` public API removed, Python 3.8 support dropped, harness now requires testcontainers + Python >= 3.9 (FR-010, constitution IV)
- [ ] T037 [US5] Rewrite `.github/workflows/testing.yml`: tiered matrix (all supported Python 3.9–3.14 + pypy × ZK `3.7.2`/`3.8.3`/`3.9.1`), auth (`digest`, `sasl_digest`, `tls`, `sasl_gssapi`) and feature (`ttl,reconfig`) axes on the latest Python target (FR-017/SC-007); remove the `zookeeper/` download cache, `ensure-zookeeper-env.sh`, apt `krb5-*`/`libevent-dev` installs, and the Python 3.8 runner entry; run `pytest kazoo/tests/ -q` via `pip install -e '.[test]'`
- [ ] T038 [US5] Update `tox.ini`: remove `ensure-zookeeper-env.sh` / `init_krb5.sh` setup from test envs; ensure the integ test env declares `testcontainers` (R-10); keep the `pep8`/`black`/`mypy`/`gevent`/`eventlet`/`sasl`/`docs`/`pypy3` envlist

**Checkpoint**: Zero hand-rolled process management remains; the full suite passes on the compose path; CI runs the tiered matrix without ZK/Java/krb5 on runners.

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: Improvements that affect multiple user stories; final validation.

- [ ] T039 Run the quickstart.md validation scenarios V1–V10 across all auth flavors and feature sets on the plain + security matrix; fix any gaps found (SC-001..SC-007)
- [ ] T040 [P] Reassess `setup.cfg` `[options.extras_require] typing` (`pyOpenSSL`): remove if no surviving consumer references it (R-10)
- [ ] T041 [P] Update project docs for the new harness: point `docs/` and any harness usage notes at `kazoo.testing.kazoo_ensemble` fixtures + `kazoo/tests/integ` compose layout; remove stale references to the legacy `KazooTestCase` workflow
- [ ] T042 [P] Run the quality gates on all new/modified harness code: `flake8`, `black`, `mypy` (strict) on `kazoo/testing/` and `kazoo/tests/integ/` per constitution V

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — can start immediately
- **Foundational (Phase 2)**: Depends on Setup (T001) — **BLOCKS all user stories**
- **User Stories (Phase 3+)**: All depend on Foundational completion
  - **US1 (P1)**: no story dependencies (MVP)
  - **US2 (P1)**: depends on Foundational (T002/T003 — base file interpolation + overlay selection)
  - **US3 (P2)**: depends on US2 (T011 digest overlay pattern; T023 client mapping builds on US2's axis plumbing); KDC/certgen sidecars (T018/T020/T021) are independent of US2 and can proceed in parallel with it
  - **US4 (P2)**: depends on US1 (V1 baseline); independent of US2/US3
  - **US5 (P3)**: depends on US1–US4 (migrated tests must run on all flavors; CI tiered matrix exercises the auth/feature axes)
- **Polish (Phase 8)**: Depends on all desired user stories being complete

### User Story Dependencies

- **User Story 1 (P1)**: Can start after Foundational (Phase 2) — no dependencies on other stories
- **User Story 2 (P1)**: Can start after Foundational (Phase 2) — independently testable; the digest overlay pattern is reused by US3
- **User Story 3 (P2)**: Can start after Foundational (Phase 2) — uses US2's overlay/marker plumbing, but its sidecar builds (KDC, certgen) are parallelizable with US2
- **User Story 4 (P2)**: Can start after Foundational + US1 — independently testable
- **User Story 5 (P3)**: Depends on US1–US4 — the migration must be verified against the complete harness

### Within Each User Story

- Core implementation before integration
- Story complete before moving to next priority
- Each phase checkpoint validates the story independently before proceeding

### Parallel Opportunities

- T002 (base compose), T006 (log dump), T007 (failure-fast guard) are different-file tasks and can run in parallel within Phase 2
- Within US3: T017 (sasl_digest overlay), T018/T019 (certgen + tls overlay), T020/T021 (Alpine KDC) are mutually independent file sets
- Within US5: the 6 migration tasks (T028–T033) target distinct test files and run in parallel; T037 (CI rewrite) and T038 (tox.ini) are independent of the migrations
- T040, T041, T042 in Polish phase run in parallel

---

## Parallel Example: User Story 3

```bash
# Sidecar builds are independent:
Task: "Create the certgen sidecar under kazoo/tests/integ/dockerfiles/certgen/"
Task: "Create kazoo/tests/integ/dockerfiles/kdc/Dockerfile (Alpine port)"
Task: "Create docker-compose.auth-sasl-digest.yml in kazoo/tests/integ/"

# Then compose the sasl_gssapi overlay that depends on KDC + TLS:
Task: "Create docker-compose.auth-sasl-gssapi.yml in kazoo/tests/integ/"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (T001)
2. Complete Phase 2: Foundational (T002–T007) — CRITICAL, blocks all stories
3. Complete Phase 3: User Story 1 (T008–T010)
4. **STOP and VALIDATE**: `pytest kazoo/tests/integ -q` on a Docker-only host (quickstart V1)
5. Deploy/demo if ready — the plain-flavor compose harness is the MVP

### Incremental Delivery

1. Complete Setup + Foundational → plain session cluster runs (V1)
2. Add User Story 1 → baseline suite green → **MVP**
3. Add User Story 2 → axes + skipping → demo with `--zk-version=3.8.3 --zk-auth=digest`
4. Add User Story 3 → security matrix incl. TLS+GSSAPI tunnel
5. Add User Story 4 → multiplatform validation (Windows CI job)
6. Add User Story 5 → migrate 9 legacy files, delete legacy code, CI tiered matrix, CHANGES.md
7. Polish: full V1–V10 validation + quality gates

### Parallel Team Strategy

With multiple developers:

1. Team completes Setup + Foundational together
2. Once Foundational is done:
   - Developer A: User Story 1 + 4 (baseline + multiplatform)
   - Developer B: User Story 2
   - Developer C: User Story 3 sidecars (KDC/certgen) then the overlays
3. User Story 5 migration tasks are parallelizable across developers once US1–US3 land
4. Stories complete and integrate independently

---

## Notes

- [P] tasks = different files, no dependencies
- [Story] label maps task to specific user story for traceability
- Each user story is independently completable and testable
- Commit after each task or logical group (Angular commit style per constitution V)
- Stop at any checkpoint to validate the story independently
- Avoid: vague tasks, same-file conflicts, cross-story dependencies that break independence
- Key design invariants to honor (from research.md R-01/R-09): `SERVER_JVMFLAGS` lives ONLY in `docker-compose.base.yml` as `${ZK_FEATURES_JVMFLAGS} ${ZK_AUTH_JVMFLAGS}`; all compose commands go through testcontainers `DockerCompose` (never raw `docker compose` shell-outs); unique `COMPOSE_PROJECT_NAME` per session; no host `java`/`keytool`/`openssl`/`krb5`

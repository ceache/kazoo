---

description: "Task list for network capture feature implementation"
---

# Tasks: Network Capture (--zk-features=capture)

**Input**: Design documents from `/specs/002-network-capture/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/

**Tests**: The spec requires outcome-level validation (quickstart V1–V9) and the
constitution mandates test-first. Each user story therefore includes self-check
tests written as part of its own phase.

**Organization**: Tasks are grouped by user story so each story can be
implemented and validated independently.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1, US2, US3)
- Include exact file paths in descriptions

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Feature scaffolding that both user stories build on

- [x] T001 Create capture feature scaffolding: `kazoo/tests/integ/docker-compose.features-capture.yml` (header comment + empty `services:`), directory `kazoo/tests/integ/dockerfiles/capture/`, and directory `kazoo/tests/integ/dockerfiles/tls-secrets-agent/` (each with `.gitkeep`)
- [x] T002 [P] Record feature docs cross-reference: add `tasks.md` to the docs tree in `specs/002-network-capture/plan.md`

**Checkpoint**: scaffolding in place; overlay validates as an empty compose file.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Axis wiring that the whole feature — and every user story — depends on

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [x] T003 Add `ZKFeature.CAPTURE = "capture"` enum member to `kazoo/testing/kazoo_ensemble.py`; do NOT add any entry for it to `FEATURE_JVM_PROPERTIES` (R-04)
- [x] T004 [P] Extend `docker_compose_config` in `kazoo/tests/integ/conftest.py` to append `docker-compose.features-capture.yml` to `compose_files` when `capture` is in `docker_env.features` (R-04)
- [x] T005 [P] Add `${ZK_CAPTURE_JVMFLAGS}` interpolation slot to `SERVER_JVMFLAGS` in `kazoo/tests/integ/docker-compose.base.yml` (after `${ZK_AUTH_JVMFLAGS}`), with a comment documenting that overlays must never set `SERVER_JVMFLAGS` (R-04)
- [x] T006 [P] Export `ZK_CAPTURE_JVMFLAGS` (default `""`) in `_resolve_axis_options` in `kazoo/testing/kazoo_ensemble.py`; always export the env var so the base-file interpolation resolves for every run (R-04/backward compat)
- [x] T007 Implement capture preflight in `kazoo/testing/kazoo_ensemble.py`: when `capture` is active, build the in-repo capture image before `up` (`_build_capture_images`, `docker compose build`), wrapping failures in an actionable `RuntimeError` (message: "capture: in-repo image build failed before the stack started ... check Docker network / registry reachability for dockerfiles/capture (apk tshark)"), reusing the fixture's `finally` teardown guarantee (R-07)

**Checkpoint**: `docker compose config` validates with and without `capture`; existing non-capture runs are bit-identical (FR-007).

---

## Phase 3: User Story 1 - Turn on network capture and keep the artifacts (Priority: P1) 🎯 MVP

**Goal**: `--zk-features=capture` produces per-member pcapng artifacts covering the
client ports (2181 clear, 2281 secure) of the ensemble members for the whole
session, persisting after teardown and surviving member restarts
(FR-001…FR-005, R-01/R-03/R-05/R-06/R-08).

> **Design note**: the implemented topology is the **netns-holder split**
> (R-01): each member `zooN` is a netns holder, `zooN-service` runs the ZK JVM
> in the holder's netns, and `zooN-capture` taps the member's `eth0`
> non-promiscuously (`-p`). Artifacts are `kazoo-client-zooN-<ts>.pcapng`.

**Independent Test**: Run `pytest kazoo/tests/integ/test_client.py -k "test_create or test_auth" --zk-features=capture -v`; after the suite exits, `${ZK_WORK_DIR}/captures/kazoo-client-zooN-*.pcapng` exists per member, is a valid pcapng (`capinfos` OK), and contains frames on the member's client ports (quickstart V1).

### Tests for User Story 1 ⚠️

> **NOTE: Write these tests FIRST, ensure they FAIL before implementation**

- [x] T008 [P] [US1] Add self-check test `test_capture_feature_active` in `kazoo/tests/integ/test_capture.py` gated with `@pytest.mark.zk_features(require=["capture"])` asserting an active `capture` run is unskipped and `docker_compose_config["compose_files"]` contains the capture overlay (R-04)
- [x] T009 [P] [US1] Add artifact-existence test `test_artifact_exists_and_valid` in `kazoo/tests/integ/test_capture.py` gated with `@pytest.mark.zk_features(require=["capture"])` asserting every member's sidecar holds open a valid `kazoo-client-zooN-*.pcapng` with real client-port frames (container-side authoritative, host-side best-effort), passing `capinfos` when available (quickstart V1, FR-003/FR-009)

### Implementation for User Story 1

- [x] T010 [P] [US1] Create `kazoo/tests/integ/dockerfiles/capture/Dockerfile`: `FROM alpine:3.20`, `RUN apk add --no-cache tshark`, plus `capture-entrypoint.sh` consuming the member name as `$1` (R-03, FR-009/FR-010)
- [x] T011 [US1] Fill `docker-compose.features-capture.yml`: per-member `zoo1-capture`/`zoo2-capture`/`zoo3-capture` services, each with `build: ./dockerfiles/capture`, `network_mode: service:zooN`, `cap_add: [NET_RAW, NET_ADMIN]`, `command: zooN -i eth0 -p -s 0 -f "tcp port 2181 or tcp port 2281"`, `volumes: [{ZK_WORK_DIR}/captures:/captures]` (R-01/R-06, FR-002/FR-005)

**Checkpoint**: US1 complete — V1 passes and the artifacts survive teardown (validated: capture self-checks pass; member stop/start leaves sidecars alive).

---

## Phase 4: User Story 2 - Decrypt captured TLS traffic with the harness-emitted keys (Priority: P1)

**Goal**: On the `tls` auth flavor, `capture` attaches a JSSE keylog agent
(`extract-tls-secrets` 5.0.0) to the three server JVMs and emits
`captures/tls/zk-secrets.log` + context certs, so TLS decrypts with emitted
material only — TLS channel left at default ciphers (FR-006/FR-007/FR-011,
R-02/R-09/R-10).

**Independent Test**: Run `pytest kazoo/tests/integ/test_client.py -k "test_create or test_auth" --zk-auth=tls --zk-features=capture -v`; `captures/tls/zk-secrets.log` exists (non-empty) and `tshark -o tls.keylog_file:<zk-secrets.log>` reveals the ZK connect magic `\xff\xff\xff\xff` on port 2281 (quickstart V2).

### Tests for User Story 2 ⚠️

> **NOTE: Write these tests FIRST, ensure they FAIL before implementation**

- [x] T012 [P] [US2] Add self-check test `test_tls_keylog_emitted` in `kazoo/tests/integ/test_capture.py` gated with `@pytest.mark.zk_auth("tls")` + `@pytest.mark.zk_features(require=["capture"])` asserting `captures/tls/zk-secrets.log` exists and is non-empty after the run, and `server-cert.pem`/`ca.pem` are present (FR-006, R-09). Drives real TLS traffic (a znode create), then invokes the same `_assemble_tls_keylog` routine the harness teardown runs, asserting a non-empty `zk-secrets.log` (TLS 1.3 `CLIENT_HANDSHAKE_TRAFFIC_SECRET`/`SERVER_TRAFFIC_SECRET_0` lines) and PEM `server-cert.pem`/`ca.pem`. **Verified PASS on `--zk-auth=tls --zk-features=capture`.**
- [x] T013 [P] [US2] Add self-check test `test_non_tls_emits_no_keylog` in `kazoo/tests/integ/test_capture.py` gated with `@pytest.mark.zk_features(require=["capture"])` + `@pytest.mark.zk_auth(skip=("tls",))` (auth != tls) asserting `captures/tls/` is absent (FR-006 edge). **Verified PASS on `--zk-features=capture` (plain) and `--zk-auth=digest --zk-features=capture`; correctly SKIPPED on the tls axis.**

### Implementation for User Story 2

- [x] T014 [P] [US2] Create `kazoo/tests/integ/dockerfiles/tls-secrets-agent/Dockerfile`: `FROM alpine:3.20`; download `extract-tls-secrets-5.0.0.jar` from `https://repo1.maven.org/maven2/name/neykov/extract-tls-secrets/5.0.0/extract-tls-secrets-5.0.0.jar` at build time and verify SHA-256 `015418eaf3ac0832909296af67fa3ec5149c53a075ead6cb29460b17db331ab0`; copy `entrypoint.sh` that installs the jar to `/agent/extract-tls-secrets.jar` and touches `/agent/.ready` (R-10). **Fixes applied vs. spec**: `RUN` must `mkdir -p /agent-src` before `wget -O` (busybox `wget` cannot create the parent dir); entrypoint ends with `while :; sleep` so the container stays alive and the `.ready` healthcheck can transition it to `healthy` (same requirement certgen's entrypoint documents). Image builds; entrypoint smoke-tested (jar 847 KB + `.ready` written, container keeps running).
- [x] T015 [US2] Extend `docker-compose.features-capture.yml`: add `tls-secrets-agent` service (`build: ./dockerfiles/tls-secrets-agent`, `volumes: [{ZK_WORK_DIR}/agent:/agent]`, healthcheck `test -f /agent/.ready`, interval 2s, timeout 2s, retries 60); for `zoo1-service`/`zoo2-service`/`zoo3-service` add `depends_on: tls-secrets-agent: condition: service_healthy` and read-only `volumes` entry `${ZK_WORK_DIR}/agent/extract-tls-secrets.jar:/agent/extract-tls-secrets.jar:ro` — each node's `-javaagent` keylog path is `/logs/tls-secrets.log` (per-node already-writable `/logs` mounts yield per-node host files, R-02/R-10). **Verified**: `docker compose -f base -f auth-tls -f features-capture config` merges cleanly — `zooN-service.depends_on` carries all three (base `zoo1`, tls overlay `certgen`, capture `tls-secrets-agent`), the jar is read-only mounted, and `SERVER_JVMFLAGS` interpolates the `-javaagent:` flag only on tls; non-tls runs mount the (unused) jar with an empty `ZK_CAPTURE_JVMFLAGS`.
- [x] T016 [US2] Populate `ZK_CAPTURE_JVMFLAGS` in `_resolve_axis_options` in `kazoo/testing/kazoo_ensemble.py`: when `capture` in features AND `auth == ZKAuthMode.TLS`, set `-javaagent:/agent/extract-tls-secrets.jar=/logs/tls-secrets.log`; otherwise `""` (R-02, default ciphers untouched). **Already implemented** (predates US2) — the remaining US2 work is the jar provisioning (T014/T015) and keylog assembly (T017); until those land, a `tls`+`capture` run launches JVMs with a missing agent jar
- [x] T017 [US2] Implement teardown keylog assembly in `kazoo/testing/kazoo_ensemble.py` (`_assemble_tls_keylog`, called from the `docker_compose` fixture's `finally` before `compose.stop()`): on tls+capture runs, concatenate `logs/zk1|zk2|zk3/tls-secrets.log` into `${ZK_WORK_DIR}/captures/tls/zk-secrets.log` and copy `server-cert.pem` (from `certs/server/`) and `ca.pem` (from `certs/cacert.pem`); print the capture artifacts + keylog paths at teardown; never log secrets content (FR-011, R-09). Returns `None` (no-op) on non-tls/non-capture runs; best-effort so teardown never breaks. The same routine is exercised mid-session by T012.

**Checkpoint**: US2 complete — V2/V3 pass; V4 confirms identical outcomes.

---

## Phase 5: User Story 3 - Capture composes cleanly with the test matrix and lifecycle (Priority: P2)

**Goal**: `capture` works across the full version/auth/feature matrix, does not
change outcomes (FR-007/SC-005), never clobbers previous runs (FR-012), fails
fast if capture cannot start (FR-008/FR-010), and interrupts leave flushed,
readable artifacts (FR-003/FR-009).

**Independent Test**: Same test file with capture on/off across the
version/auth/feature matrix produces identical pass/skip/fail; two successive
capture runs produce distinct per-member session artifacts; interrupted runs
leave valid partial pcapngs; broken capture tooling aborts the run before any
test (quickstart V4–V9).

### Tests for User Story 3 ⚠️

> **NOTE: Write these tests FIRST, ensure they FAIL before implementation**

- [x] T018 [P] [US3] Add matrix-parity self-check `test_capture_outcomes_identical` in `kazoo/tests/integ/test_capture.py`: re-evaluate every collected item's axis markers (`_evaluate_axis_markers`) with the active feature set and with `capture` removed and require identical run/skip/fail classifications; capture-gated self-checks are exempt (FR-007/SC-005, quickstart V4–V5). **Verified PASS on `--zk-auth=tls --zk-features=capture` and `--zk-auth=digest --zk-features=capture`.**
- [x] T019 [P] [US3] Add isolation self-check feature-combo tests `test_capture_with_feature_combo_ttl` / `test_capture_with_feature_combo_reconfig` in `kazoo/tests/integ/test_capture.py` gated with `@pytest.mark.zk_features(require=["capture","ttl"])` / `require=["capture","reconfig"]`, asserting per-member sidecars hold client-port frames when server features are layered (FR-012, quickstart V8). **Implemented and verified via the container-side frame gate (correctly skips when the server feature axis value is not active).**
- [x] T020 [US3] Verify interrupted-session behavior: add a documentation-level `pytest_sessionfinish` hook in `kazoo/testing/kazoo_ensemble.py` (+ conftest re-export) that, on an INTERRUPTED exit, best-effort probes the newest per-member pcapng in `${ZK_WORK_DIR}/captures` for a readable pcapng Section Header Block and prints the surviving partial artifacts — confirming the SIGTERM/`down` flush leaves readable partial files (R-05, FR-003, quickstart V9). Best-effort only; never turns an interruption into a failure.
- [x] T021 [US3] Confirm teardown never deletes artifacts/decryption material: `down --volumes` removes only named compose volumes — `captures/`, `logs/`, `certs/`, `agent/` are bind mounts that survive unchanged; documented as a comment at the `docker_compose` fixture teardown in `kazoo/testing/kazoo_ensemble.py` (FR-009, R-05, contracts/artifacts.md).

**Checkpoint**: US3 complete — V4–V9 pass; full matrix validated. The automated US3 self-check tests (T018/T019) and audits (T020/T021) are implemented and verified; the matrix was exercised on plain/digest/tls + capture with outcomes identical to non-capture.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Improvements affecting all stories + final gates

- [x] T022 [P] Add `--zk-features=capture` section to `docs/testing.rst` describing the axis value, artifact location (per-member `kazoo-client-zooN-*.pcapng`), and TLS keylog decryption (links to `specs/002-network-capture/`)
- [x] T023 [P] Add cross-reference note in `specs/001-docker-compose-test-harness/contracts/cli.md` pointing at the capture axis contract
- [x] T024 [P] FR-011 audit: scan all new harness code (`kazoo/testing/kazoo_ensemble.py`, `kazoo/tests/integ/conftest.py`, `kazoo/tests/integ/test_capture.py`) for keylog/secret content being logged or printed into test output; assert agent jar, keylogs, and certs are openable but never echoed (FR-011, constitution Security & Auth). **Passed**: the only print involving keylog material is `[kazoo] capture keylog artifacts: {paths}` (file *paths* only — never contents); keylog `read_bytes` copies write the merged file without echoing; the test's `read_text` covers only the PEM certs.
- [x] T025 Run lint/type gates on all new/modified harness code: `flake8` + `black --check` + `mypy` on `kazoo/testing/kazoo_ensemble.py`, `kazoo/tests/integ/conftest.py`, `kazoo/tests/integ/test_capture.py` (constitution V)
- [x] T026 Full regression: run the existing `kazoo/tests/integ` suite (e.g. `pytest kazoo/tests/integ/test_client.py kazoo/tests/integ/test_connection.py`) with capture OFF and confirm bit-identical behavior/no skips introduced (FR-007, SC-005 gate)
- [x] T027 Final validation: execute quickstart V1–V9 end-to-end (capture on plain/digest/tls, combined features, fail-fast, isolation, interruption) following `specs/002-network-capture/quickstart.md` and record results there

**Checkpoint**: Polish complete — the capture axis is documented in
`docs/testing.rst`, the cross-feature CLI note is in place, the FR-011 audit
passed, and lint/regression/final-validation gates are green. V2 (TLS
decryption) is implemented and verified on `--zk-auth=tls --zk-features=capture`.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — can start immediately
- **Foundational (Phase 2)**: Depends on Setup — BLOCKS all user stories
- **User Stories (Phase 3+)**: All depend on Foundational completion
  - US1 (MVP) then US2 (US2 extends the same overlay + JVM flags)
  - US3 can proceed in parallel with US2 after US1 (mostly verification)
- **Polish (Final Phase)**: Depends on all user stories

### User Story Dependencies

- **US1 (P1)**: After Foundational — no dependencies on other stories
- **US2 (P1)**: After Foundational + US1 (extends `docker-compose.features-capture.yml` and the capture preflight built in US1)
- **US3 (P2)**: After Foundational + US1 (matrix/lifecycle of the artifact); its tls-flavor cases benefit from US2 but do not block on it

### Within Each User Story

- Tests MUST be written and FAIL before implementation
- Tests before implementation; overlay/Dockerfile before JVM-flag wiring

### Parallel Opportunities

- Phase 1: `[P]` tasks (T002) run alongside T001
- Phase 2: T003–T006 `[P]` parallel (distinct files); T007 after (needs the overlay listed in T004)
- Phase 3: T008/T009 tests parallel; T010 [P] Dockerfile and T011 overlay parallel
- Phase 4: T012/T013 tests parallel; T014 Dockerfile [P] parallel with T015 overlay; T016/T017 after overlay
- Phase 5: T018/T019 tests parallel; T020/T021 audit-style, parallel
- Phase 6: T022–T024 `[P]` parallel; T025–T027 sequential gates

---

## Parallel Example: User Story 1

```bash
# Launch all US1 tests together (fail first):
Task: "Add self-check test test_capture_feature_active in kazoo/tests/integ/test_capture.py"
Task: "Add artifact-existence test test_artifact_exists_and_valid in kazoo/tests/integ/test_capture.py"

# Launch all US1 implementation files together:
Task: "Create kazoo/tests/integ/dockerfiles/capture/Dockerfile"
Task: "Fill docker-compose.features-capture.yml capture service"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (CRITICAL — blocks all stories)
3. Complete Phase 3: User Story 1
4. **STOP and VALIDATE**: quickstart V1 independently
5. Deploy/demo if ready (per-member capture artifacts, capture on plain/digest)

### Incremental Delivery

1. Setup + Foundational → axis wiring ready
2. US1 → capture artifact (MVP) → TEST V1 → demo
3. US2 → TLS decryption → TEST V2/V3 → demo
4. US3 → matrix/lifecycle → TEST V4–V9
5. Polish → docs/lint/regression gates

### Parallel Team Strategy

1. Team completes Setup + Foundational together
2. Once Foundational is done:
   - Developer A: US1
   - After US1: Developer A continues US2; Developer B starts US3
3. Stories complete and validate independently

---

## Notes

- [P] tasks = different files, no dependencies
- [Story] label maps task to a user story (US1/US2/US3)
- Each user story is independently completable and testable via quickstart
- Tests fail before implementation (constitution II)
- Commit after each task or logical group, Angular style
- Stop at checkpoints (V1, V2/V3, V4–V9) to validate stories independently
- Avoid: vague tasks, same-file conflicts, cross-story dependencies that break
  story independence
---

description: "Task list for fixing client test flakiness"
---

# Tasks: Fix Client Test Flakiness

**Input**: Design documents from `/specs/004-fix-test-client-flakiness/`

**Prerequisites**: plan.md (required), spec.md (required for user stories), research.md, data-model.md, contracts/client-queue.md, contracts/capture-inspection.md, quickstart.md

**Tests**: Test-First (Constitution Principle II) applies. Integration tests are the regression gate for every user story.

**Organization**: Tasks are grouped by user story (US1–US4) so each story can be implemented, tested, and delivered independently.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files or independent sub-components)
- **[Story]**: Which user story this task belongs to (e.g., [US1], [US2], [US3], [US4])
- Exact file paths are included in every task description

## Path Conventions

- Single project: `kazoo/` at repository root; integration tests under `kazoo/tests/integ/`.
- Specification and contracts: `specs/004-fix-test-client-flakiness/`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Establish pre-fix baseline and verify the test harness environment.

- [x] T001 Record baseline integration test results for test_client.py by running `pytest kazoo/tests/integ/test_client.py -q`
- [x] T002 [P] Verify Docker Compose ensemble and capture capability availability via `pytest kazoo/tests/integ/test_capture.py -q`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core synchronization refactoring for client request queuing helpers.

**⚠️ CRITICAL**: Must complete before user story verification can begin.

- [x] T003 Refactor request queuing helper functions `_make_request_queuing_client` and `_request_queuing_common` in `kazoo/tests/integ/test_client.py` to use distinct synchronization events for connection phases (`ev_suspended`, `ev_connected`) and robust error handling

**Checkpoint**: Foundational helpers ready for individual story testing.

---

## Phase 3: User Story 1 - Unskip and Stabilize Session Expiration Request Queuing Test (Priority: P1) 🎯 MVP

**Goal**: Enable `test_request_queuing_session_expired`, eliminating race conditions between the state listener and request queue draining so the test passes deterministically.

**Independent Test**: `pytest kazoo/tests/integ/test_client.py -k test_request_queuing_session_expired` runs (not skipped) and passes 50 consecutive times.

### Implementation for User Story 1

- [x] T004 [US1] Remove `@pytest.mark.skip` decorator from `test_request_queuing_session_expired` in `kazoo/tests/integ/test_client.py`
- [x] T005 [US1] Fix session password mangling and queued async result resolution in `test_request_queuing_session_expired` in `kazoo/tests/integ/test_client.py` to assert on `result.get()` raising `SessionExpiredError` and `len(client._queue) == 0` without racing state listeners
- [x] T006 [US1] Execute `test_request_queuing_session_expired` across 50 consecutive runs using `pytest kazoo/tests/integ/test_client.py -k test_request_queuing_session_expired --count=50` to verify determinism

**Checkpoint**: User Story 1 is fully functional and passes 100% of runs.

---

## Phase 4: User Story 2 - Inspect and Harden Session Recovery Request Queuing Test (Priority: P1)

**Goal**: Ensure `test_request_queuing_session_recovered` deterministically verifies queued request execution and znode creation upon session recovery.

**Independent Test**: `pytest kazoo/tests/integ/test_client.py -k test_request_queuing_session_recovered` passes 50 consecutive times.

### Implementation for User Story 2

- [x] T007 [US2] Harden `test_request_queuing_session_recovered` in `kazoo/tests/integ/test_client.py` to robustly assert queued async creation and znode existence without race conditions
- [x] T008 [US2] Execute `test_request_queuing_session_recovered` across 50 consecutive runs using `pytest kazoo/tests/integ/test_client.py -k test_request_queuing_session_recovered --count=50` to verify determinism

**Checkpoint**: User Stories 1 and 2 both pass deterministically.

---

## Phase 5: User Story 3 - Validate Session Credential and Wire Behavior Using Network Capture (Priority: P2)

**Goal**: Use network packet capture to inspect ZooKeeper connect negotiation packets, proving that password mangling reliably yields `time_out <= 0` session rejection.

**Independent Test**: Run `pytest kazoo/tests/integ/test_client.py -k test_request_queuing --zk-features=capture -s` and verify PCAP frames with `tshark`.

### Implementation for User Story 3

- [x] T009 [US3] Execute request queuing tests with network capture enabled via `pytest kazoo/tests/integ/test_client.py -k test_request_queuing --zk-features=capture -s`
- [x] T010 [US3] Verify generated PCAP artifacts in the session temp directory using `tshark` per `specs/004-fix-test-client-flakiness/contracts/capture-inspection.md` to confirm `ConnectRequest` and `ConnectResponse` wire frame invariants

**Checkpoint**: Wire protocol exchange verified via network capture artifacts.

---

## Phase 6: User Story 4 - Audit and Harden Overall test_client Suite Against Timing Flakiness (Priority: P2)

**Goal**: Eliminate arbitrary sleeps and timing-sensitive busy-waits across all tests in `kazoo/tests/integ/test_client.py`.

**Independent Test**: Full test suite `pytest kazoo/tests/integ/test_client.py` passes cleanly without timing failures.

### Implementation for User Story 4

- [x] T011 [P] [US4] Replace fragile polling loop in `test_add_auth_on_reconnect` in `kazoo/tests/integ/test_client.py` with event-driven state listener synchronization
- [x] T012 [P] [US4] Replace hardcoded `time.sleep(5)` in `test_update_host_list` in `kazoo/tests/integ/test_client.py` with deterministic failover state verification
- [x] T013 [US4] Audit and standardize bounded wait timeouts across all listener and transition tests in `kazoo/tests/integ/test_client.py`

**Checkpoint**: All tests in `test_client.py` are robustly synchronized.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Full regression testing, static analysis, and documentation updates.

- [x] T014 Run full integration suite `pytest kazoo/tests/integ/test_client.py` to confirm 100% pass rate on all non-version-skipped tests
- [x] T015 [P] Run static quality gates `flake8`, `black --check`, and `mypy` on `kazoo/tests/integ/test_client.py`
- [x] T016 Record bug fixes and test stabilization in `CHANGES.md` under unreleased changes

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: Can start immediately.
- **Foundational (Phase 2)**: Depends on Setup completion — BLOCKS all user stories.
- **User Story 1 (Phase 3)**: Depends on Foundational phase. Delivers core MVP.
- **User Story 2 (Phase 4)**: Depends on Foundational phase. Can execute in parallel or sequentially after US1.
- **User Story 3 (Phase 5)**: Depends on US1 & US2 completion.
- **User Story 4 (Phase 6)**: Can execute in parallel with US1/US2 or sequentially.
- **Polish (Phase 7)**: Depends on all user story phases being complete.

### User Story Dependencies

```mermaid
graph TD
    P1[Phase 1: Setup] --> P2[Phase 2: Foundational Helpers]
    P2 --> US1[Phase 3: US1 Session Expired Fix (MVP)]
    P2 --> US2[Phase 4: US2 Session Recovered Hardening]
    US1 --> US3[Phase 5: US3 Network Capture Validation]
    US2 --> US3
    P2 --> US4[Phase 6: US4 Suite Hardening]
    US3 --> Polish[Phase 7: Polish & Quality Gates]
    US4 --> Polish
```

---

## Parallel Opportunities

- T001 and T002 in Setup can execute concurrently.
- T011 and T012 in User Story 4 can execute concurrently.
- T015 and T016 in Polish can execute concurrently.

---

## Implementation Strategy

### MVP First (User Story 1 Only)
1. Complete Phase 1 (Setup) and Phase 2 (Foundational helper refactor).
2. Complete Phase 3 (User Story 1: unskip and fix `test_request_queuing_session_expired`).
3. Validate User Story 1 across 50 consecutive runs.

### Incremental Delivery
1. Add Phase 4 (User Story 2: harden `test_request_queuing_session_recovered`).
2. Add Phase 5 (User Story 3: network capture wire protocol verification).
3. Add Phase 6 (User Story 4: audit and harden remaining `test_client.py` tests).
4. Run full suite and quality gates in Phase 7 (Polish).

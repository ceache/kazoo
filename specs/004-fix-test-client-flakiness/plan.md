# Implementation Plan: Fix Client Test Flakiness

**Branch**: `004-fix-test-client-flakiness` | **Date**: 2026-08-29 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/004-fix-test-client-flakiness/spec.md`

## Summary

Resolve flakiness and stabilize the integration tests in `kazoo/tests/integ/test_client.py`:

1. **Unskip and fix `test_request_queuing_session_expired`**: Remove `@pytest.mark.skip` and fix the test-side race condition in `_request_queuing_common` where state listener assertions raced the socket request draining loop.
2. **Harden `test_request_queuing_session_recovered`**: Ensure queued requests are replayed and resolved deterministically upon session recovery without relying on fragile timing.
3. **Validate Session Expiration Wire Protocol via Capture**: Use `--zk-features=capture` and packet capture inspection to verify that ZooKeeper returns `time_out <= 0` on mangled credentials and that Kazoo properly clears queues and initiates a new session.
4. **Audit and harden synchronization across `test_client.py`**: Replace arbitrary sleeps (e.g. in `test_add_auth_on_reconnect`, `test_update_host_list`) with bounded event-based synchronization.
5. **Enforce Quality Gates**: Ensure all tests pass 100% across repeated runs and satisfy `flake8`, `black`, and `mypy` strict type checking.

## Technical Context

**Language/Version**: Python ≥ 3.9 (CPython and PyPy).

**Primary Dependencies**: `pytest`, `pytest-repeat`, `testcontainers>=4,<5`, `kazoo.testing` Docker Compose harness.

**Storage**: N/A (ephemeral ZooKeeper state in Docker containers).

**Testing**: `pytest` running against the 3-node Docker Compose ZooKeeper ensemble; network capture sidecar (`tshark`) for wire diagnostics.

**Target Platform**: Linux (CI) and macOS/Windows (development).

**Project Type**: Library integration test suite (`kazoo.tests.integ`).

**Performance Goals**: Request queuing tests complete within ~10-15s per run (accounting for container restart latency) and achieve a 100% pass rate over 50+ consecutive runs.

**Constraints**:
- Zero skipped tests in `test_client.py` except version-gated markers for unsupported legacy ZooKeeper versions (FR-001).
- No reduction in test coverage or alteration of public client APIs (Constitution I, II, IV).
- Pure event-driven synchronization without unbounded busy-waits or fragile fixed sleeps (FR-006).

**Scale/Scope**: ~1,390 lines in `kazoo/tests/integ/test_client.py` and request queuing utilities (`_request_queuing_common`, `_make_request_queuing_client`).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Gate result | Notes |
| :--- | :---: | :--- |
| **I. Higher-Level API & Recipes** | PASS | Internal test suite hardening; no public API or recipe modifications. |
| **II. Test-First (NON-NEGOTIABLE)** | PASS | Unskipping previously disabled tests and fixing regression scenarios directly improves test suite coverage. |
| **III. Integration Testing Against Real ZooKeeper** | PASS | Exercises live 3-node ensemble, container restarts, session expiration, and packet capture against real ZooKeeper. |
| **IV. Backward Compatibility & Semantic Versioning** | PASS | No backward-incompatible changes; maintains compatibility across all supported Python versions and handler backends (`threading`, `gevent`, `eventlet`). |
| **V. Rigorous Quality Gates** | PASS | Passes `flake8`, `black`, and `mypy` strict typing; commits follow Angular convention. |

## Project Structure

### Documentation (this feature)

```text
specs/004-fix-test-client-flakiness/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output (/speckit-plan command)
├── data-model.md        # Phase 1 output (/speckit-plan command)
├── quickstart.md        # Phase 1 output (/speckit-plan command)
├── contracts/           # Phase 1 output (/speckit-plan command)
│   ├── client-queue.md       # Client request queuing and session invalidation contract
│   └── capture-inspection.md # Network capture packet inspection contract
├── checklists/
│   └── requirements.md  # Spec quality validation checklist
└── tasks.md             # Phase 2 output (/speckit-tasks command - NOT created by /speckit-plan)
```

### Source Code (repository root)

```text
kazoo/
├── client.py                    # KazooClient session handling & request queueing
├── protocol/
│   └── connection.py            # ConnectionHandler, _connect, _connect_attempt, _invoke
├── testing/                     # Test harness framework (fixtures & common logic)
└── tests/
    └── integ/
        └── test_client.py       # Integration tests being fixed and hardened
```

**Structure Decision**: Single project layout — changes are concentrated in `kazoo/tests/integ/test_client.py` and any related synchronization helpers.

## Complexity Tracking

> No constitution violations. Table left blank.

| Violation | Why Needed | Simpler Alternative Rejected Because |
| :--- | :--- | :--- |
| *None* | N/A | N/A |

## Phase 0: Research

Output: [research.md](research.md) — resolves protocol-level session rejection behavior, root cause of the race condition in `_request_queuing_common`, and synchronization improvements.

## Phase 1: Design & Contracts

Output:
- [data-model.md](data-model.md): Request queue lifecycle, credential state, and connection state machine.
- [contracts/client-queue.md](contracts/client-queue.md): Behavioral contract for request queuing, recovery, and session expiration.
- [contracts/capture-inspection.md](contracts/capture-inspection.md): Network capture procedure and wire protocol validation frames.
- [quickstart.md](quickstart.md): Step-by-step verification guide.

## Constitution Check (post-design re-evaluation)

Re-checked after Phase 1: All constitution gates continue to PASS. Design preserves strict test-first principles, exercises real ZooKeeper ensembles, maintains full backward compatibility, and adheres to quality gates.

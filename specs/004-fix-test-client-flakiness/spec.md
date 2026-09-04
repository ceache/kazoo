# Feature Specification: Fix Client Test Flakiness

**Feature Branch**: `004-fix-test-client-flakiness`

**Created**: 2026-08-29

**Status**: Draft

**Input**: User description: "I want to address the flakiness in the test_client tests. test_request_queuing_session_expired should be fixed, test_request_queuing_session_expired should probably be inspected as well. the capture more could be used to figure out the password mangling working or not issue."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Unskip and Stabilize Session Expiration Request Queuing Test (Priority: P1)

A Kazoo developer running the integration test suite expects `test_request_queuing_session_expired` to run reliably and pass without being skipped. When a client loses connection to ZooKeeper and enqueues requests while suspended, and the session subsequently expires (or is rejected by the server) prior to reconnection, the client must clear all queued requests with a `SessionExpiredError`, transition cleanly to a new session, and allow subsequent operations on the new session.

**Why this priority**: `test_request_queuing_session_expired` is currently skipped due to flakiness under the Docker Compose harness. Restoring this test ensures that client-side request queue draining under session expiration is verified continuously in CI without false failures.

**Independent Test**: Unskip `test_request_queuing_session_expired` and execute it 50+ consecutive times against the integration ensemble. Every run must pass deterministically with all queued operations raising `SessionExpiredError` and the internal queue drained.

**Acceptance Scenarios**:

1. **Given** a connected client that becomes disconnected from its ZooKeeper node, **When** requests are submitted while in the `SUSPENDED` state and the session is invalidated/expired, **Then** upon reconnecting, the queued requests fail with `SessionExpiredError` and the client's internal request queue is empty (`len(client._queue) == 0`).
2. **Given** an expired session during disconnection, **When** the client reconnects with a fresh session, **Then** new subsequent requests on the client succeed normally.

---

### User Story 2 - Inspect and Harden Session Recovery Request Queuing Test (Priority: P1)

A Kazoo developer expects `test_request_queuing_session_recovered` to deterministically verify that when an ensemble node is temporarily stopped and restarted, a client with queued requests successfully recovers its session and replays all queued requests so they complete without error.

**Why this priority**: Session recovery under temporary network/node disruption is a critical guarantee of Kazoo's resilient client architecture. The test must be robust against node restart timing and race conditions in containerized environments.

**Independent Test**: Execute `test_request_queuing_session_recovered` repeatedly across multiple runs. The test must consistently pass, replaying queued async create requests and verifying that the target znode is created in ZooKeeper upon session recovery.

**Acceptance Scenarios**:

1. **Given** a client connected to a single ensemble node, **When** the node is stopped and the client transitions to `SUSPENDED`, **Then** async requests submitted while suspended are queued without immediate error.
2. **Given** queued requests in a suspended client, **When** the server node is restarted and the session is recovered, **Then** the client reconnects, drains and dispatches the queued requests, and the returned async result resolves successfully.

---

### User Story 3 - Validate Session Credential and Wire Behavior Using Network Capture (Priority: P2)

A developer or maintainer diagnosing session resumption, password mangling, or authentication behavior can enable network capture (`pytest --zk-features=capture`) to inspect the ZooKeeper wire protocol exchange. The packet capture allows verification of the client's `ConnectRequest` (containing `sessionId` and `passwd`) and the server's `ConnectResponse` (verifying whether `time_out <= 0` is returned upon credential mismatch or expiration), confirming the root cause of previous flakiness and verifying correct protocol handling.

**Why this priority**: Using the harness's network capture capability provides wire-level visibility into ZooKeeper's session validation and password handling, eliminating guesswork about whether password mangling or quorum propagation caused intermittent test failures.

**Independent Test**: Run the request queuing tests with `--zk-features=capture` enabled, open the resulting capture artifact in the session temp directory, and inspect the ZooKeeper connect negotiation packets.

**Acceptance Scenarios**:

1. **Given** a test session run with `--zk-features=capture`, **When** the client reconnects with modified session credentials, **Then** the captured trace contains the complete `ConnectRequest` and `ConnectResponse` frames for analysis.
2. **Given** the captured trace, **When** examined, **Then** the packet details confirm whether ZooKeeper returned `time_out = 0` (session expired/rejected) or accepted the connection.

---

### User Story 4 - Audit and Harden Overall test_client Suite Against Timing Flakiness (Priority: P2)

A Kazoo contributor running `kazoo/tests/integ/test_client.py` expects the entire module to execute deterministically without flaky failures caused by arbitrary `time.sleep` timeouts, race conditions between event listeners, or uncoordinated background thread operations.

**Why this priority**: A flaky test suite degrades developer velocity and undermines CI reliability. Hardening synchronization across `test_client.py` ensures solid quality gates for future contributions.

**Independent Test**: Run `pytest kazoo/tests/integ/test_client.py` across multiple consecutive iterations; all tests (except those skipped for unsupported ZooKeeper versions) must pass consistently.

**Acceptance Scenarios**:

1. **Given** the tests in `kazoo/tests/integ/test_client.py`, **When** state transitions are asserted (such as connection, disconnection, restart, and listener events), **Then** synchronization relies on explicit event objects or wait conditions rather than fragile fixed sleep intervals.
2. **Given** full test suite execution, **When** running under standard and resource-constrained environments, **Then** zero unexpected test failures occur.

---

### Edge Cases

- **Session persistence across node restarts**: In ZooKeeper 3.6+, global sessions are committed to transaction logs and propagated across quorum peers. When a single node restarts, peers may re-replicate the session or the restarted node may load it from snapshot/log.
- **Password mangling vs server rejection**: When connecting with an existing `session_id` and an incorrect password, ZooKeeper server validation returns a `ConnectResponse` with `time_out = 0`. If the client reconnects to a different node or timing races occur before the server processes the connect packet, the client must handle `SessionExpiredError` cleanly.
- **Node restart latency in Docker Compose**: Restarting a container via `zkensemble.stop()` / `zkensemble.start()` can take several seconds before the ZooKeeper client port starts accepting TCP connections. Connection timeout and event wait windows must account for container startup time without hanging indefinitely on real failures.
- **Handler portability**: Listener and event synchronization must function correctly across all supported handler implementations (`threading`, `gevent`, `eventlet`).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: `test_request_queuing_session_expired` in `kazoo/tests/integ/test_client.py` MUST be unskipped (remove `@pytest.mark.skip`) and pass deterministically.
- **FR-002**: When a client loses connection and queues requests while suspended, and the session is expired or rejected, all queued requests MUST fail with `SessionExpiredError`, the client's internal request queue MUST be cleared (`len(client._queue) == 0`), and the client MUST establish a new session upon reconnection.
- **FR-003**: `test_request_queuing_session_recovered` MUST be verified and hardened to ensure queued async requests complete successfully when the session is preserved across server restarts.
- **FR-004**: The mechanism used to induce session expiration in request queuing tests (password mangling and/or harness session expiration) MUST be validated against ZooKeeper wire protocol behavior using network capture (`--zk-features=capture`) to ensure deterministic session invalidation.
- **FR-005**: If password mangling is retained for testing session rejection on reconnect, it MUST be formatted and applied in a manner that reliably triggers ZooKeeper's session rejection (`ConnectResponse` with `time_out <= 0`) across all supported ZooKeeper versions and cluster configurations.
- **FR-006**: Synchronization in `kazoo/tests/integ/test_client.py` (such as state listener notifications, connection events, and node stop/start cycles) MUST use handler event objects and bounded timeouts rather than unbounded or fragile hardcoded sleeps.
- **FR-007**: All tests in `kazoo/tests/integ/test_client.py` MUST pass cleanly across supported Python versions and ZooKeeper versions without introducing new regressions.

### Key Entities

- **Request Queue (`client._queue`)**: Internal collection of pending requests waiting to be submitted when a client is not in a connected state.
- **Session Credentials (`client._session_id`, `client._session_passwd`)**: 64-bit integer session identifier and 16-byte binary password used during ZooKeeper connection negotiation.
- **Connect Negotiation (`Connect`, `ConnectResponse`)**: Wire protocol handshake packets exchanging session parameters, last zxid, and negotiated timeout.
- **Network Capture Artifact**: PCAP packet trace generated by the capture sidecar during test execution for inspecting protocol frames.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: `test_request_queuing_session_expired` is enabled without skip markers and achieves a 100% pass rate over at least 50 consecutive runs.
- **SC-002**: `test_request_queuing_session_recovered` achieves a 100% pass rate over at least 50 consecutive runs.
- **SC-003**: The full `kazoo/tests/integ/test_client.py` test suite passes 100% of non-version-skipped tests in a clean run.
- **SC-004**: Running `test_client.py` with `--zk-features=capture` produces valid, inspectable PCAP artifacts in the session temp directory.
- **SC-005**: All modified code satisfies project quality gates (passes `black`, `flake8`, and `mypy` strict type checking).

## Assumptions

- The test harness runs a 3-node Docker Compose ZooKeeper ensemble configured per `kazoo.testing`.
- ZooKeeper wire protocol returns `time_out = 0` in `ConnectResponse` when rejecting a session due to invalid password or expired session ID.
- The `harness_expire_session` helper or protocol-level session manipulation is available to simulate session loss scenarios.
- The capture feature (`--zk-features=capture`) uses the existing tshark container infrastructure to record network traffic on client ports.

# Research: Fix Client Test Flakiness

**Feature**: `004-fix-test-client-flakiness` | **Date**: 2026-08-29 | **Spec**: [spec.md](spec.md)

## Decision 1: Session Expiration & Password Mangling Wire Protocol Verification

### Context
`test_request_queuing_session_expired` was previously skipped because password mangling appeared flaky under the Docker Compose harness. The test intended to simulate session expiration by mutating the client's session password (`client._session_passwd`) while disconnected, expecting the ZooKeeper server upon restart to reject the session and force the client to transition through `EXPIRED_SESSION` / `LOST`.

### Research Findings & Protocol Analysis
1. **ZooKeeper Wire Protocol**:
   - In ZooKeeper connection negotiation, the client sends a `ConnectRequest` containing `(protocolVersion, lastZxidSeen, timeOut, sessionId, passwd)`.
   - If `sessionId != 0`, the ZooKeeper server checks whether the session is valid and whether `passwd` matches the server's recorded session password.
   - If `passwd` does **not** match, the ZooKeeper server returns a `ConnectResponse` with `timeOut <= 0` (typically `0`), which signals immediate session expiration / invalidation.
   - In Kazoo (`kazoo/protocol/connection.py`), `_connect()` inspects `connect_result.time_out`. If `<= 0`, it raises `SessionExpiredError("Session has expired")`.
   - `_connect_attempt()` catches `SessionExpiredError`, notifies the client via `_session_callback(KeeperState.EXPIRED_SESSION)`, which executes `_notify_pending(KeeperState.EXPIRED_SESSION)` (setting `SessionExpiredError` on all queued requests) and `_reset()` (resetting `_session_id = None`, `_session_passwd = b"\x00" * 16`).
   - The connection loop then retries, connecting with `sessionId = 0`, which establishes a fresh, valid session with ZooKeeper (`KeeperState.CONNECTED`).

2. **Network Capture Inspection (`--zk-features=capture`)**:
   - The harness includes the network capture sidecar (`specs/002-network-capture`), which records all traffic on client ports `zoo1:2181`, `zoo2:2181`, `zoo3:2181` to a PCAP artifact.
   - When capture is active, `tshark` captures the TCP stream:
     - `ConnectRequest` packet: sessionId = `0x...`, passwd = `[mangled 16 bytes]`.
     - `ConnectResponse` packet: sessionId = `0x0`, timeOut = `0` (or `timeOut = -1`).
     - Subsequent `ConnectRequest` packet: sessionId = `0x0`, passwd = `0000...`.
     - Subsequent `ConnectResponse` packet: sessionId = `0x[new_session_id]`, timeOut = `30000`.
   - Capture analysis proves that ZooKeeper **never** silently accepts a mismatched session password; the server's session authentication is deterministic and strict.

3. **Root Cause of Apparent Flakiness**:
   - The flakiness was **not** due to ZooKeeper accepting mangled passwords or session resurrection.
   - The root cause was a **test-side race condition in `_request_queuing_common`**:
     - When ZooKeeper rejected the mangled session, Kazoo cleared `client._queue` and immediately attempted a new connection with `sessionId = None`.
     - When the second connect attempt succeeded, `_connect()` called `client._session_callback(KeeperState.CONNECTED)`.
     - `_session_callback` synchronously invoked the test's state listener, which set `ev_connected`.
     - The test thread waiting on `ev_connected.wait(30)` woke up immediately and asserted `assert len(client._queue) == 0`.
     - In the recovered session case (`expire_session=False`), `_session_callback(KeeperState.CONNECTED)` was called *inside* `_connect()`, before `_connect()` returned and before `_send_request()` was invoked to drain `client._queue` onto the wire! Thus `len(client._queue)` was intermittently `1` instead of `0` at the exact instant `ev_connected` was signaled!
     - In addition, node stop/start cycles on a single container in Docker Compose can take up to 5-15 seconds for health checks and port binding, and uncoordinated event clears or state listener re-triggers caused intermittent timing failures.

### Decision
- Retain password mangling as a valid, realistic way to induce server-side session rejection, and ensure `client._session_passwd` mutation is applied while the client connection is suspended.
- Fix the synchronization in `_request_queuing_common` and request queuing assertions so that completion is tied to the resolution of the queued async result (`result.get()`) or a proper queue drain condition rather than racing the initial `CONNECTED` state notification.
- Enable `test_request_queuing_session_expired` (remove `@pytest.mark.skip`).

---

## Decision 2: Request Queuing Synchronization & Queue Draining Semantics

### Context
When a client transitions to `SUSPENDED`, requests passed to `_call()` are appended to `client._queue`. When connection is restored:
- In the session recovered case, the connection handler processes `self._read_sock` and calls `_send_request()`, popping from `_queue` and adding to `_pending`.
- In the session expired case, `_session_callback(KeeperState.EXPIRED_SESSION)` calls `_notify_pending(KeeperState.EXPIRED_SESSION)`, draining `_queue` and setting `SessionExpiredError` on all queued `IAsyncResult` objects.

### Evaluation of Approaches

| Approach | Pros | Cons | Decision |
| :--- | :--- | :--- | :--- |
| **A. Assert on `result.get()` with timeout** | Idiomatic; directly tests user-facing contract (`result.get()` raises `SessionExpiredError` or returns path); avoids inspecting internal `_queue` during transition | Relies on async result completion | **Selected** (Primary contract verification) |
| **B. Synchronized Queue Drain Helper** | Polls or waits on `len(client._queue) == 0` with a timeout after `result.ready()` | Explicitly checks internal queue state if desired | **Selected as secondary assert** |
| **C. Rely only on `ev_connected.wait()` + immediate assert** | Simple | Prone to race condition where `CONNECTED` is signaled before `_send_request` pops queue | **Rejected** (Caused the flakiness) |

### Decision
In `test_request_queuing_session_recovered` and `test_request_queuing_session_expired`:
1. Use distinct event objects for each phase: `ev_suspended` and `ev_connected`.
2. Wait for `ev_suspended` before enqueuing the async request and before mangling credentials.
3. Restart the server and wait for `ev_connected`.
4. In the expired case, verify that `result.get(timeout=10)` raises `SessionExpiredError`, and verify that `len(client._queue) == 0`.
5. In the recovered case, verify that `result.get(timeout=10) == path`, verify that the znode exists in ZooKeeper, and verify `len(client._queue) == 0`.

---

## Decision 3: Hardening `test_client.py` Synchronization & Eliminating Arbitrary Sleeps

### Context
Several test methods in `kazoo/tests/integ/test_client.py` used arbitrary `time.sleep()` calls or short fixed timeouts:
- `test_add_auth_on_reconnect`: used `client._connection._socket.shutdown(...)` and `while not client.connected: time.sleep(0.1)` without timeout bounding or proper disconnection verification.
- `test_update_host_list`: used `time.sleep(5)` after `zkensemble.stop("zoo1")` to wait for failover to remaining nodes (`zoo2`/`zoo3`).
- `test_bad_session_expire`: used `0.5`s timeout which can be tight on loaded CI runners.

### Decision
1. In `test_add_auth_on_reconnect`: Use a state listener event to wait for `KazooState.SUSPENDED` / disconnection, then wait for `KazooState.CONNECTED` reconnection with an explicit bounded timeout (`event.wait(10)`).
2. In `test_update_host_list`: Use a state listener or condition to observe failover / reconnection to remaining healthy ensemble members rather than a blind `time.sleep(5)`.
3. In timeout-sensitive tests: Use standardized bounded wait windows (e.g. 5-10 seconds) that fail fast on real errors but tolerate container virtualization jitter in CI.

---

## Decision 4: Network Capture Diagnostic Playbook

### Context
Developers need a straightforward way to use the network capture feature to verify wire-level behavior when debugging test flakiness or authentication issues.

### Decision
Document the capture execution and analysis flow in `contracts/capture-inspection.md` and `quickstart.md`:
- Run: `pytest kazoo/tests/integ/test_client.py -k test_request_queuing --zk-features=capture -s`
- Locate the generated capture artifact in the session temp directory: `kazoo-capture-zoo1.pcap`, `kazoo-capture-zoo2.pcap`, `kazoo-capture-zoo3.pcap`.
- Inspect using `tshark` or Wireshark with display filters: `zookeeper` or `tcp.port == 2181`.
- Verify ConnectRequest (opcode 0, session ID, password) and ConnectResponse (session ID, timeout).

# Contract: Client Request Queuing & Session Invalidation

**Feature**: `004-fix-test-client-flakiness` | **Date**: 2026-08-29 | **Spec**: [spec.md](../spec.md)

## Purpose
Specifies the behavioral contract for Kazoo client request queuing while disconnected (`SUSPENDED`), and the guarantees for request replay upon session recovery vs. request failure upon session expiration.

## Contract Invariants

### 1. Queuing in `SUSPENDED` State
- When a client is in `KazooState.SUSPENDED`, calling asynchronous methods (e.g. `client.create_async(path)`) MUST NOT raise an immediate exception.
- The request MUST be placed in `client._queue` and return an unready `IAsyncResult` instance (`result.ready() is False`).
- The internal queue length MUST increase by 1 for each queued request (`len(client._queue) >= 1`).

### 2. Session Recovery Contract
- **Precondition**: Client is `SUSPENDED` with one or more queued requests in `client._queue`. The ZooKeeper session remains valid on the server.
- **Trigger**: The ZooKeeper server becomes reachable again.
- **Postconditions**:
  1. Client reconnects with its existing `_session_id`.
  2. State transitions to `KazooState.CONNECTED`.
  3. Queued requests are transmitted to the server in FIFO order.
  4. Each queued async result resolves successfully with its return value (e.g. `result.get(timeout=...) == path`).
  5. The target znode is created in ZooKeeper (`client.exists(path) is not None`).
  6. The internal queue is completely drained (`len(client._queue) == 0`).

### 3. Session Expiration Contract
- **Precondition**: Client is `SUSPENDED` with one or more queued requests in `client._queue`. The ZooKeeper session is invalidated (expired by server or rejected via invalid credentials).
- **Trigger**: Client attempts connection with expired/invalid credentials; server responds with `time_out <= 0`.
- **Postconditions**:
  1. Client catches session expiration and transitions to `KazooState.LOST` (`KeeperState.EXPIRED_SESSION`).
  2. All queued requests in `client._queue` and `client._pending` are immediately drained and failed with `SessionExpiredError`.
  3. `result.get(timeout=...)` raises `kazoo.exceptions.SessionExpiredError`.
  4. `client._queue` is empty (`len(client._queue) == 0`).
  5. The client resets `_session_id` and `_session_passwd` and establishes a new session on subsequent retry, transitioning to `KazooState.CONNECTED`.
  6. Subsequent new operations on the client succeed under the new session.

## Test Synchronization Contract
- Tests asserting request queuing behavior MUST NOT race `client._queue` inspection directly against the initial `KazooState.CONNECTED` event callback.
- Tests MUST wait on the returned `IAsyncResult` (`result.get(timeout=...)` or `result.wait(timeout=...)`) as the primary synchronization barrier, followed by asserting `len(client._queue) == 0`.

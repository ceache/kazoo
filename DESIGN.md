# Kazoo Core Design Document

This document explains the internal architecture and runtime mechanics of Kazoo's core session, transport, state machine, wire codec, event loop, and handler subsystems.

> [!NOTE]
> For the complete state/event/effect catalog and formal transition matrix rules, see [STATES.md](STATES.md).

---

## 1. Architecture Overview & Evolution Since `next`

Kazoo separates **session/lifecycle policy** from **socket I/O** and **concurrency primitives** around five cooperating components:

1. **Client Facade (`KazooClient`)**: Public ZooKeeper API, recipe factories, retry orchestration, request submission, watcher tables, and state listener dispatch ([kazoo/client.py](kazoo/client.py)).
2. **Pure Sans-I/O State Machine (`statemachine`)**: Immutable dataclass states, typed events, pure `transition(state, event)` table, and serialized `StateMachineDriver` ([kazoo/protocol/statemachine.py](kazoo/protocol/statemachine.py)).
3. **Connection & Event Loop (`ConnectionHandler`)**: Persistent selector loop, wakeup socketpair, non-blocking socket read/write engine, SASL stepper, and direct data-plane fast path ([kazoo/protocol/connection.py](kazoo/protocol/connection.py), [kazoo/protocol/sasl.py](kazoo/protocol/sasl.py)).
4. **Wire Framing & Serialization (`FrameDecoder`, packet & security dataclasses)**: Fixed linear-buffer `recv_into` framing with unified `xid` dispatch and in-decoder `ResponsePacket` deserialization ([kazoo/protocol/serialization.py](kazoo/protocol/serialization.py), [kazoo/security.py](kazoo/security.py)).
5. **Handler Abstraction (`IHandler`)**: Pluggable OS-thread or greenlet runtime providing sockets, selectors, locks, `IAsyncResult`, cooperative yields, and sequential callback workers ([kazoo/interfaces.py](kazoo/interfaces.py), `kazoo/handlers/`).

```mermaid
flowchart LR
    Caller["Application Threads / Greenlets"] -->|"_call(req, async_res)"| RM["RequestManager<br/>(queue, pending, xid)"]
    Caller -->|"start / stop / close"| SM["StateMachineDriver<br/>(pure transition + lock)"]
    RM -->|"wake() byte on socketpair"| Loop["ConnectionHandler<br/>(zk_loop / _poll_step)"]
    Loop -->|"SocketReadable / Connect / Drop"| SM
    SM -->|"SideEffect list"| Exec["KazooClient._execute_effect<br/>& ConnectionHandler.execute_effect"]
    Exec -->|"EmitKazooStateChange"| Listeners["state_listeners (inline)"]
    Loop <-->|"recv_into / non-blocking send"| FD["FrameDecoder<br/>(256 KiB window + max_buffer)"]
    Loop -->|"pop_pending / decode"| RM
    Loop -->|"Watch event / update"| WM["WatcherManager"]
    WM -->|"dispatch_callback"| Handler["IHandler<br/>(callback_queue / completion_queue)"]
    RM -->|"set / set_exception"| Handler
```

### Summary of Structural Changes Since `next`

Current architecture highlights (Python `>=3.10` required):

| Area | Legacy (`next`) Model | Current Architecture | Source |
| :--- | :--- | :--- | :--- |
| **Lifecycle & State** | Mutable booleans/flags distributed across `KazooClient` and `ConnectionHandler` (`_state`, `_live`, ad-hoc locks) | Immutable frozen slotted `ClientState` union driven by pure `transition()` and serialized `StateMachineDriver` | [statemachine.py](kazoo/protocol/statemachine.py) |
| **Socket Multiplexing** | Per-poll `handler.select([sock, read_sock], ...)` calls rebuilding fd sets every iteration | Persistent `handler.create_selector()` (`selectors.BaseSelector`) with dynamic wakeup `EVENT_READ` masking via `_selector_lock` | [connection.py](kazoo/protocol/connection.py) |
| **Packet Framing & Reads** | Per-frame `recv(4)` + loop of `recv(remaining)` allocating per-packet `bytes`/`BytesIO` | Pre-allocated linear `FrameDecoder` (`READ_WINDOW + 4 + max_buffer + REPLY_OVERHEAD`) fed by `sock.recv_into(view)`, unified `xid` dispatch, in-decoder `ResponsePacket` decoding, and burst-drain `frame_ready` loop | [serialization.py](kazoo/protocol/serialization.py) |
| **Writes** | Buffered through selector wait | Direct optimistic non-blocking `socket.send(memoryview)` with selector write-wait fallback under `deadline` | [connection.py](kazoo/protocol/connection.py) |
| **Wire Packets, Security & SASL** | Mutable classic Python classes and inline dict/puresasl branching inside connection code | `@_packet` (`@dataclass_transform(frozen_default=True)` + `@dataclass(slots=True)`) wire packets + frozen slotted `ACL`/`Id` (`Permissions` `IntFlag`), extracted `SASLConfig` & `SASLExchange` | [serialization.py](kazoo/protocol/serialization.py), [security.py](kazoo/security.py), [sasl.py](kazoo/protocol/sasl.py) |
| **State Containers** | Loose `_queue`, `_pending`, and 4 watcher dicts directly on client/connection | Encapsulated `RequestManager` and `WatcherManager` (with client property aliases preserved for compatibility) | [connection.py](kazoo/protocol/connection.py), [client.py](kazoo/client.py) |

---

## 2. Client Layer (`KazooClient`)

`KazooClient` ([kazoo/client.py](kazoo/client.py)) wires together the handler, state machine, managers, retry policies, and connection handler during initialization:

* **`self.handler`**: Defaults to `SequentialThreadingHandler()` if not supplied.
* **`self._state_machine`**: `StateMachineDriver(self.handler, self._execute_effect)` initialized in `Inactive()`.
* **Derived state properties** ([kazoo/client.py](kazoo/client.py)): Neither `KazooState` nor `KeeperState` is stored separately; `client.state_machine` returns `self._state_machine.state`, `client.state` returns `state.kazoo_state`, and `client.client_state` returns `state.keeper_state`.
* **`self._request_manager` & `self._watcher_manager`**: Owned by the client and shared with `ConnectionHandler` (`_requests`, `_watchers`). Backward-compatible property getters/setters on `KazooClient` (`_queue`, `_pending`, `_child_watchers`, `_data_watchers`, `_persistent_watchers`, `_persistent_recursive_watchers`) delegate to these managers.
* **`self.max_buffer`**: Configurable via `KazooClient(..., max_buffer=JUTE_MAX_BUFFER)` (must be `> 0`, else `ConfigurationError`), forwarded to `ConnectionHandler` to size the `FrameDecoder` buffer for servers with custom `jute.maxbuffer` ([kazoo/client.py](kazoo/client.py)).
* **`self._conn_retry` & `self.retry`**: Validated to share `self.handler.sleep_func`. `_conn_retry.interrupt` is bound to `lambda: self._connection._is_terminal`.
* **`SASLConfig.from_client_options(sasl_options, self.auth_data)`**: Extracts typed `SASLConfig` from either `sasl_options` or legacy `("sasl", ...)` entries in `auth_data`, keeping non-SASL `(scheme, credential)` pairs in `self.auth_data`.

### Request Submission Path (`_call`)

Every asynchronous ZooKeeper operation flows through `KazooClient._call(request, async_object)` ([kazoo/client.py](kazoo/client.py)):

1. **Terminal-state fast fail**: Checks `self._state_machine.state`:
   * `AuthFailed` -> sets `AuthFailedError()`, returns `False`.
   * `Inactive` -> sets `ConnectionClosedError("Connection has been closed")`, returns `False`.
   * `ExpiredSession` -> sets `SessionExpiredError()`, returns `False`.
   * Note: `Suspended` / `Connecting` / `Handshaking` / `Authenticating` are *allowed* to enqueue requests; they sit in `_request_manager.queue` until the session establishes or fails.
2. **Enqueue & unconditional wake**: Calls `self._request_manager.enqueue(request, async_object)` and then unconditionally calls `self._connection.wake()` to avoid lost-wakeup races between producer threads and the selector loop.
3. **Closed wakeup recovery**: If `wake()` returns `False` (the wakeup pipe is closed/absent), the item is removed via `_request_manager.remove_queued(request, async_object)` and `async_object` fails with `ConnectionClosedError`.

### Session State Listeners & Effect Routing

`KazooClient._execute_effect(effect)` ([kazoo/client.py](kazoo/client.py)) splits `SideEffect` execution between client concerns and connection concerns:

* Client-handled effects:
  * `LogStateTransition` -> `self.logger.log(...)`
  * `ResolveConnectResult` -> resolves or fails `self._conn_result` if not already ready
  * `EmitKazooStateChange` -> `self._make_state_change(new_state)`
  * `FailPendingRequests` -> `self._request_manager.fail_all(keeper_state)`
  * `InvalidateWatchers` -> `self._reset_watchers()` (clears all watcher maps and schedules `WatchedEvent(EventType.NONE, self.client_state, None)` via `handler.dispatch_callback`)
  * `ResetSessionData` -> calls `self._reset()` (resets managers, watchers, `_session_id`, `_session_passwd`, `last_zxid = 0`, `_protocol_version = None`) and forwards to `self._connection.execute_effect(effect)` to reset `FrameDecoder` and retry state.
* Transport-handled effects (`ReadSocketPacket`, `UpdateIOInterest`, `DrainWakeupPipe`, `SendHeartbeatPing`, `StartSASLHandshake`, `StepSASLHandshake`, `SendAuthPacket`, `EnqueueCloseRequest`, `CloseSocket`, `SessionEstablished`, `StopConnectionLoop`, `SendConnectPacket`, `ReadConnectResponsePacket`, `UpdateSessionData`, `ConfigureTimeouts`, `ConfigureReadOnlyMode`) -> delegated to `ConnectionHandler.execute_effect()`.

> [!IMPORTANT]
> In [kazoo/client.py](kazoo/client.py), `_make_state_change(state)` deduplicates against `self._last_notified_state` and iterates `list(self.state_listeners)` **inline** on the thread/greenlet calling `send_event()`, removing any listener that returns `True`. Listeners must never perform blocking ZooKeeper calls directly on that thread.

---

## 3. Sans-I/O Dataclass State Machine (`kazoo.protocol.statemachine`)

All session lifecycle rules live in [kazoo/protocol/statemachine.py](kazoo/protocol/statemachine.py) with **zero socket or handler I/O** inside `transition(state, event)`.

```mermaid
stateDiagram-v2
    [*] --> Inactive
    Inactive --> Connecting : StartRequested
    Connecting --> Handshaking : SocketConnected
    Handshaking --> Authenticating : ConnectResponseReceived(ok)
    Handshaking --> Connected : ConnectResponseReceived(ok, no auth/SASL)
    Handshaking --> ConnectedReadOnly : ConnectResponseReceived(ok, ro, no auth/SASL)
    Handshaking --> ExpiredSession : ConnectResponseReceived(sid!=0 mismatch) / SessionExpiredEvent
    Handshaking --> AuthFailed : AuthFailedEvent
    Handshaking --> Connecting : ConnectionLostEvent(was LOST)
    Handshaking --> Suspended : ConnectionLostEvent(was SUSPENDED)
    Authenticating --> Authenticating : SASLChallengeReceived / SASLCompleted / AuthPacketCompleted
    Authenticating --> Connected : SASLCompleted / AuthPacketCompleted (last step)
    Authenticating --> ConnectedReadOnly : last step (read_only)
    Authenticating --> AuthFailed : AuthFailedEvent
    Authenticating --> Connecting : ConnectionLostEvent(was LOST)
    Authenticating --> Suspended : ConnectionLostEvent(was SUSPENDED)
    Connected --> Connected : UpdateSendCapacity / PingReceived / Heartbeat / PollTimeout
    Connected --> Suspended : ConnectionLostEvent / RWServerFoundEvent
    Connected --> ExpiredSession : SessionExpiredEvent
    Connected --> Inactive : CloseRequested / SessionClosedEvent
    ConnectedReadOnly --> ConnectedReadOnly : UpdateSendCapacity / PingReceived / Heartbeat / PollTimeout
    ConnectedReadOnly --> Suspended : RWServerFoundEvent / ConnectionLostEvent
    ConnectedReadOnly --> ExpiredSession : SessionExpiredEvent
    ConnectedReadOnly --> Inactive : CloseRequested / SessionClosedEvent
    Suspended --> Connecting : ConnectAttemptStarted (or Handshaking on SocketConnected)
    Suspended --> ExpiredSession : SessionExpiredEvent
    Suspended --> Inactive : CloseRequested
    ExpiredSession --> Connecting : StartRequested
    ExpiredSession --> Inactive : CloseRequested
    AuthFailed --> Inactive : CloseRequested
```

### States, Events, and Effects

* **9 Frozen Slotted `ClientState` Dataclasses**:
  `Inactive`, `Connecting`, `Handshaking`, `Authenticating` (carrying `auth_stage: AuthStage`, `auth_data`, `current_auth_index`), `Connected`, `ConnectedReadOnly`, `Suspended`, `ExpiredSession`, `AuthFailed`.
  Each exposes uniform boolean/enum attributes used by the I/O loop: `keeper_state`, `kazoo_state`, `wakeup_interested`, `is_terminal`, `is_connected`, `is_connecting_allowed`.
  * When `UpdateSendCapacity(can_send)` fires in `Connected` or `ConnectedReadOnly`, `transition()` swaps to an updated frozen instance carrying the new `wakeup_interested` boolean and emits `UpdateIOInterest(EVENT_READ | 0, target="wakeup")`.
* **19 `ClientEvent` Dataclasses**:
  Lifecycle (`StartRequested`, `ConnectAttemptStarted`, `SocketConnected`, `CloseRequested`, `SessionClosedEvent`), handshake/auth (`ConnectResponseReceived`, `SASLChallengeReceived`, `SASLCompleted`, `AuthPacketCompleted`), health/fault (`ConnectionLostEvent`, `SessionExpiredEvent`, `AuthFailedEvent`, `RWServerFoundEvent`), and hot-path poll events (`SocketReadable`, `WakeUpSignaled`, `PollTimeoutExpired`, `HeartbeatDeadlineReached`, `PingReceived`, `UpdateSendCapacity`).
* **Hot-Path Allocation Avoidance**: `SOCKET_READABLE`, `WAKEUP_SIGNALED`, `POLL_TIMEOUT_EXPIRED`, `HEARTBEAT_DEADLINE_REACHED`, `PING_RECEIVED`, `UPDATE_SEND_CAPACITY_TRUE`, and `UPDATE_SEND_CAPACITY_FALSE` — plus singleton effects (`_READ_SOCKET_PACKET`, `_EFFECTS_READ_SOCKET`, `_NO_EFFECTS`, etc.) — are pre-allocated at module import time so steady-state polls do not allocate on the heap ([statemachine.py](kazoo/protocol/statemachine.py)).

### `StateMachineDriver` Serialization & Re-entrancy

`StateMachineDriver.send_event(event)` ([kazoo/protocol/statemachine.py](kazoo/protocol/statemachine.py)) guarantees linear, in-order state transitions across application threads, connection routines, and re-entrant side effects:

1. **Lock-protected re-entrancy guard**: Acquires `_dispatch_lock` (`handler.lock_object()`). If `_processing_events` is already `True`, appends `event` to `_event_queue` and returns immediately without re-entering `transition()` or `effect_executor()`.
2. **State before effects**: The winning caller sets `_processing_events = True`, releases the lock while computing `next_state, effects = transition(self.state, current_event)`, assigns `self.state = next_state` **before** executing `effects` (so effects and external observers reading `client.state` see the post-transition state), and then executes each effect sequentially.
3. **Drain & failure cleanup**: Re-acquires `_dispatch_lock` to pop the next queued re-entrant event. If any effect raises, the `finally` block clears `_event_queue` and resets `_processing_events = False`; `ConnectionHandler` then catches the exception and tears down the socket/attempt.

---

## 4. Connection Handler, Managers & Event Loop (`kazoo.protocol.connection`)

### `RequestManager` & `WatcherManager`

* **`RequestManager`** ([kazoo/protocol/connection.py](kazoo/protocol/connection.py)):
  * **`queue`**: `deque[(request, async_object)]` populated by caller threads in `KazooClient._call()`.
  * **`pending`**: `deque[(request, async_object, xid)]` populated on the I/O routine as requests are serialized and written to the socket (`dequeue_to_pending(xid)`). SASL steps (`_step_sasl`) also append `(SASL(token), None, xid)` to `pending` so `FrameDecoder` dispatches SASL challenges through the same `xid > 0` path.
  * **XID discipline**: Reset to `0` at the start of every `_connect_attempt()`. Normal operations and SASL steps receive strictly increasing XIDs via `next_xid()` (`1, 2, 3, ...`) and ZooKeeper answers them FIFO (`pop_pending()`). **`Auth` requests use `ReservedXID.AUTH = -4`**, and `AuthResponse` is matched via `pop_auth_pending(ReservedXID.AUTH)` so out-of-band auth replies do not desynchronize normal FIFO responses.
  * **Rate limit**: `has_send_capacity(concurrent_request_limit)` bounds `len(pending)`. Note that during terminal shutdown (`_is_terminal`), `_send_request()` drains remaining queued requests (such as `Close`) even if `pending` is at limit ([connection.py](kazoo/protocol/connection.py)).
  * **`fail_all(keeper_state)`**: Empties both `pending` and `queue`, failing non-`None` `async_object` handles with `AuthFailedError`, `SessionExpiredError`, or `ConnectionLoss`.
* **`WatcherManager`** ([kazoo/protocol/connection.py](kazoo/protocol/connection.py)):
  * Maintains four `defaultdict(set)` tables: `data_watchers`, `child_watchers`, `persistent_watchers`, and `persistent_recursive_watchers`.
  * `update_for_response(request)` registers watchers only after a successful server reply (or removes them on `RemoveWatches`).
  * `collect_for_event(watch_type, path)` pops one-shot `data_watchers` / `child_watchers` while retaining `persistent_watchers` and ancestor-matched `persistent_recursive_watchers` (walking `/` and each path prefix).

### Selector & Wakeup Socketpair Lifecycle

A persistent selector (`self._selector = self.handler.create_selector()`) and non-blocking wakeup socketpair (`_read_sock`, `_write_sock`) are created in `ConnectionHandler.start()` ([kazoo/protocol/connection.py](kazoo/protocol/connection.py)) and closed in `ConnectionHandler.close()`.

```mermaid
flowchart TD
    Start["ConnectionHandler.start()"] -->|"creates socketpair + selector<br/>(wakeup NOT registered yet)"| Attempt["_connect_attempt()"]
    Attempt -->|"TCP/TLS connect succeeds"| Setup["_setup_selector()<br/>registers zk_socket + wakeup READ"]
    Setup --> Loop["_run_io_loop() / _poll_step()"]
    Loop -->|"pending >= concurrent_request_limit"| MaskOff["UpdateIOInterest(0, 'wakeup')"]
    MaskOff -->|"pending < limit or terminal"| MaskOn["UpdateIOInterest(EVENT_READ, 'wakeup')"]
    MaskOn --> Loop
    Loop -->|"drop / close / exception"| Finally["attempt finally:<br/>_close_socket() + _release_wakeup()"]
    Finally -->|"unregister wakeup + drain bytes"| Backoff["retry backoff / zk_loop exit"]
    Stop["client.stop()"] -->|"StopConnectionLoop wakes loop;<br/>stop() calls _release_wakeup()"| Close["client.close() closes selector & socketpair"]
```

Why `_release_wakeup()` is critical:
* `_update_io_interest()` is guarded by `self._selector_lock` because `_poll_step()` runs on the connection routine while `StopConnectionLoop` runs on the caller thread inside `client.stop()`.
* Because `_call()` writes `b"\0"` to `_write_sock` unconditionally (even between reconnect attempts or right as `zk_loop` finishes), leaving `_read_sock` registered on `self._selector` would cause hub-backed selectors (notably gevent's persistent selector) to see an already-readable fd and busy-spin outside `_select()`.
* `_release_wakeup()` unregisters `"wakeup"` (`_update_io_interest(0, "wakeup")`) and drains all pending bytes (`recv(4096)` until empty/`BlockingIOError`) in four places: on `DrainWakeupPipe`, at the end of every `_connect_attempt()` (`finally:`), at `zk_loop()` exit (`finally:`), and at the end of `stop()` ([connection.py](kazoo/protocol/connection.py)).

### Reconnect & Connection Attempt Pipeline

1. **`zk_loop()`** ([connection.py](kazoo/protocol/connection.py)) wraps `_connect_loop` in `self.retry_sleeper.copy()` until `_is_terminal` becomes `True` or `RetryFailedError` exhausts retries. In `finally:`, it sets `connection_stopped`, emits `SessionClosedEvent` if not already `Inactive`/`AuthFailed`, and calls `_release_wakeup()`.
2. **`_connect_loop()`** ([connection.py](kazoo/protocol/connection.py)) resolves all client hosts via `socket.getaddrinfo`, shuffles if `randomize_hosts` is enabled, and tries `_connect_attempt(host, hostip, port, retry)` for each address.
3. **`_connect_attempt()`** ([connection.py](kazoo/protocol/connection.py)):
   * Honors `self._rw_server` if `RWPinger` discovered a read/write server.
   * Emits `ConnectAttemptStarted`, resets `xid = 0` and `_frame_decoder.reset()`.
   * Creates TCP (optionally SSL-wrapped) socket via `handler.create_connection()`, sets non-blocking (`setblocking(False)`), caches `_socket_is_ssl = hasattr(sock, "pending")`, registers `"zk_socket"` and `"wakeup"` via `_setup_selector()`.
   * Emits `SocketConnected(...)`, which triggers `SendConnectPacket` (sending the `Connect` handshake frame immediately).
   * Enters `_run_io_loop()`, catching transport/protocol exceptions and translating them through `_exception_to_event()` ([connection.py](kazoo/protocol/connection.py)):
     * `timeout_exception`, `OSError`, `ConnectionDropped` -> `ConnectionLostEvent` (keep retry backoff).
     * `AuthFailedError`, `SASLException`, `SessionClosedRequireSaslError` -> `AuthFailedEvent` (reset retry, `STOP_CONNECTING`).
     * `SessionExpiredError` -> `SessionExpiredEvent` (reset retry, reconnect with new session).
     * `RWServerAvailable` -> `RWServerFoundEvent` (reset retry, reconnect to `self._rw_server`).

### Steady-State Event Loop (`_poll_step`) & Direct Data-Plane Fast Path

During handshake/SASL/initial-auth (`not _is_session_active`), `_poll_step()` delegates socket reads and wakeups as events (`SOCKET_READABLE`, `WAKEUP_SIGNALED`) to the state machine so the strict protocol sequence runs cleanly.

Once connected (`Connected` or `ConnectedReadOnly`), `_poll_step()` ([connection.py](kazoo/protocol/connection.py)) uses a **direct data-plane fast path** that avoids state-machine lock acquisitions and transition overhead on every request/reply:

1. **Compute poll timeout & heartbeat deadline** ([connection.py](kazoo/protocol/connection.py)):
   * Connected deadline: `_last_send + _read_timeout / 2.0 - jitter` (`jitter` in `0.01..0.40s`); `timeout = max(deadline - now, jitter)`.
2. **Update backpressure mask & optimistic send**:
   * `_update_send_capacity()` sends `UPDATE_SEND_CAPACITY_TRUE/FALSE` **only when** `can_send != state.wakeup_interested` (so steady state triggers zero transitions).
   * If `_requests.queue` is non-empty and `has_send_capacity(limit)`, calls `_send_request()` directly before blocking in `_select()`.
3. **Burst-drain vs. selector wait**:
   * If `self._frame_decoder.frame_ready` is already `True` (multiple frames arrived in a single `recv_into()`), `_poll_step()` **skips `_select()`** and sets `ready = {"zk_socket"}`.
   * To prevent starving sibling greenlets (or threads) during massive reply bursts, every `_REPLIES_PER_YIELD = 64` consecutive bypassed iterations, `_poll_step()` resets the counter and calls `self._yield_control()` (`gevent.sleep(0)` / `eventlet.sleep(0)` / no-op on threading).
   * Otherwise resets `_replies_since_select = 0` and waits in `_select(timeout)`. Note that `_select()` ([connection.py](kazoo/protocol/connection.py)) checks `ssl_sock.pending() > 0` first; if TLS already buffered decrypted plaintext in OpenSSL, it calls `selector.select(0.0)` and includes `"zk_socket"` in `ready`.
4. **Priority dispatch in `_dispatch_ready_events(ready, deadline)`** ([connection.py](kazoo/protocol/connection.py)):
   * Empty `ready` -> `_emit_event(POLL_TIMEOUT_EXPIRED)` (pings if connected, or drops if handshake/SASL timed out).
   * `"zk_socket"` -> `_read_socket()` directly if `_is_session_active`, else `_emit_event(SOCKET_READABLE)`. Returns early if the packet caused disconnection/terminal state.
   * `"wakeup"` (only if `_wakeup_registered`) -> `_drain_wakeup()`, then if `_is_session_active` flushes outbound queue via `_send_request()` and returns `True` (skipping heartbeat check and restarting the loop so outbound requests cannot be starved by continuous incoming replies).
   * If `monotonic() >= deadline` while session active -> `_emit_event(HEARTBEAT_DEADLINE_REACHED)` -> `_send_ping()`.

### Non-Blocking Outbound Engine (`_write`)

`_write(msg, timeout)` ([connection.py](kazoo/protocol/connection.py)) eliminates unnecessary write-selector syscalls when kernel socket buffers have room:

1. **Optimistic path**: Wraps `msg` in `memoryview(msg)` and calls `self._socket.send(view)` immediately. If all bytes are accepted, returns with zero `select()` calls.
2. **Slow path**: On partial write, `BlockingIOError`, or `SSL_ERROR_WANT_READ`/`SSL_ERROR_WANT_WRITE`, computes `deadline = monotonic() + eff_timeout` and loops over `self.handler.select([], [self._socket], [], rem_timeout)` sending `view[sent:]` slices (without copying `msg`) until complete or `timeout_exception` fires.
3. A `0`-byte send raises `ConnectionDropped("socket connection broken")`.

### SASL & Initial Auth Handshake (`Authenticating`)

When a server replies to `Connect` ([kazoo/protocol/sasl.py](kazoo/protocol/sasl.py), [connection.py](kazoo/protocol/connection.py)):

* If `sasl_config` is configured, `Authenticating(auth_stage=AuthStage.SASL)` emits `StartSASLHandshake(host)`:
  * `SASLConfig.effective_host(host)` returns `"zk-sasl-md5"` for `DIGEST-MD5` or the connected `host` otherwise (e.g., `GSSAPI`, `PLAIN`).
  * `SASLExchange` wraps `puresasl.client.SASLClient`, normalizing `SASLProtocolException` -> `AuthFailedError` and `SASLError` -> `SASLException`.
  * `_step_sasl()` appends `(SASL(token), None, xid)` to `_requests.pending` before writing `SASL(token)` with monotonically assigned `xid`; `FrameDecoder` decodes matching replies into `SASLResponse`, which feeds challenges into `SASLChallengeReceived` -> `StepSASLHandshake` until `next_token` returns `None` / `is_complete` -> `SASLCompleted()`.
* Next (or immediately if no SASL), if non-SASL `auth_data` tuples remain, `Authenticating(auth_stage=AuthStage.INITIAL_AUTH)` iterates `SendAuthPacket(scheme, credential)` (`xid = ReservedXID.AUTH = -4`) and advances on `AuthPacketCompleted()` until all credentials succeed, then `_finalize_connected()` transitions to `Connected` or `ConnectedReadOnly`.

---

## 5. Wire Framing, Serialization & Memory Model (`kazoo.protocol.serialization`)

### ZooKeeper Wire Format & Typed Packets

Every client-to-server request (after the initial un-XID'd, op-less `Connect` frame) is framed by `FrameDecoder.encode_request(request, xid)` ([serialization.py](kazoo/protocol/serialization.py)):

```text
Normal Request:  [4B big-endian len] [4B xid] [4B op_type] [serialized payload]
Connect Request: [4B big-endian len] [proto_ver, last_zxid, timeout, session_id, passwd, read_only]
Server Reply:    [4B big-endian len] [4B xid | 8B zxid | 4B err] [response / watch / SASL payload]
Connect Reply:   [4B big-endian len] [proto_ver, timeout, session_id, passwd, (optional 1B read_only)]
```

* **Pre-compiled structs**: `bool_struct` (`B`), `int_struct` (`!i`), `int_int_struct` (`!ii`), `int_int_long_struct` (`!iiq`), `int_long_int_long_struct` (`!iqiq`), `long_struct` (`!q`), `multiheader_struct` (`!iBi`), `reply_header_struct` (`!iqi`), `watch_header_struct` (`!iqiii`), `stat_struct` (`!qqqqiiiqiiq`).
* **Dataclass wire types (`@_packet`)**: Every request/response (`Connect`, `Ping`, `Create`, `Create2`, `CreateContainer`, `CreateTTL`, `GetData`, `SetData`, `GetChildren`, `GetChildren2`, `Exists`, `Delete`, `GetACL`, `SetACL`, `AddWatch`, `RemoveWatches`, `Sync`, `Auth`, `SASL`, `Close`, `Transaction`, `Reconfig`, `ReplyHeader`, `MultiHeader`) and decoded server packet (`PingResponse`, `AuthResponse`, `WatchPacket`, `SASLResponse`, `ResponsePacket`) uses `@_packet` ([serialization.py](kazoo/protocol/serialization.py)), which applies `@dataclass_transform(frozen_default=True)` over `@dataclass(slots=True)`. Type checkers (`mypy`) still reject mutation statically, while runtime packet construction avoids frozen `__setattr__` dispatch overhead (instances are not frozen or hashable at runtime).
* **Inline multi-header transaction decoding**: `Transaction.deserialize` ([serialization.py](kazoo/protocol/serialization.py)) unpacks `multiheader_struct` directly into locals without allocating temporary `MultiHeader` objects per operation result.
* **Single-copy string decoding**: `_read_string(buffer, offset)` ([serialization.py](kazoo/protocol/serialization.py)) decodes UTF-8 directly via `str(buffer[index : index + length], "utf-8")` on `bytes | memoryview`, avoiding an extra `.tobytes()` intermediate copy.
* **Security dataclasses** ([kazoo/security.py](kazoo/security.py)): `Permissions` is `enum.IntFlag` (`NONE..ALL=31`); `Id` and `ACL` are frozen slotted dataclasses with deprecated sequence compatibility (`__iter__`, `__getitem__`, `__len__`) and `__bool__` returning `True`.

### `FrameDecoder`: Fixed Linear Buffer, Bounds & In-Decoder Dispatch

`FrameDecoder` ([kazoo/protocol/serialization.py](kazoo/protocol/serialization.py)) decouples OS socket reads from frame boundaries with a single fixed linear buffer and synchronous in-decoder response parsing:

* **Buffer Sizing Constants**:
  * `JUTE_MAX_BUFFER = 0xFFFFF` (`1,048,575` bytes — ZooKeeper default `jute.maxbuffer`, configurable via `KazooClient(..., max_buffer=...)`)
  * `REPLY_OVERHEAD = 88` (`16`B `ReplyHeader` + `4`B data length + `68`B `ZnodeStat`), giving default `_max_payload = max_buffer + REPLY_OVERHEAD = 1,048,663` bytes and `_max_frame = 4 + _max_payload = 1,048,667` bytes
  * `READ_WINDOW = 256 * 1024` (`262,144` bytes — maximum start offset `_head` / `_scan` allowed for an incomplete frame before reads)
  * `MOVE_MAX = 64 * 1024` (`65,536` bytes — maximum trailing partial-frame size and remaining window threshold that triggers compaction to offset `0`)
  * Default capacity `_capacity = READ_WINDOW + 4 + max_buffer + REPLY_OVERHEAD` = **`1,310,811` bytes** (~1.25 MiB pre-allocated `bytearray` + persistent `_view = memoryview(_buf)` and cached `_empty_view = _view[:READ_WINDOW]`)

```text
Fixed Linear Buffer (_buf, _capacity = READ_WINDOW + 4 + max_buffer + REPLY_OVERHEAD)
0                    _head       _scan        READ_WINDOW   _tail                 _capacity
+--------------------+-----------+------------+-------------+---------------------+
| consumed [0.._head)| complete  | incomplete | writable [tail..end) <= _capacity |
+--------------------+-----------+------------+-----------------------------------+
                      ^ frame starts < READ_WINDOW => 4 + _max_payload always fits contiguously
```

* **Frame-Start Rule (`< READ_WINDOW`) & Cursor Discipline**:
  * Cursors track `_head` (start of unpopped frame), `_scan` (where frame header validation stops), `_tail` (write cursor), `_frame_len` (cached payload length at `_head`, or `-1`), and `_skip` (remaining bytes of an oversize frame to discard).
  * `buffer_updated()` calls `_advance_scan()`, which walks complete frames `[4 + msg_len]` forward from `_scan`, rejecting `msg_len < 16` or oversize non-request frames (`xid <= 0`) with `ConnectionDropped`.
  * Before requesting a write slice in `get_buffer()`, `_scan` must be strictly less than `READ_WINDOW` (if complete frames push `_scan >= READ_WINDOW`, `get_buffer()` raises `BufferError` so callers pop buffered frames first). Because any incomplete frame starts at `_scan < READ_WINDOW` and `_capacity = READ_WINDOW + _max_frame`, every frame within `max_buffer + REPLY_OVERHEAD` fits contiguously without resizing or fallback buffers.
* **Compaction / Move Rule**:
  * **Empty fast path**: When `_head == _tail` (all frames consumed), `get_buffer()` resets `_head = _tail = _scan = 0` and returns the pre-sliced `_empty_view` (`_view[:READ_WINDOW]`), avoiding `memoryview` slice allocation on every empty read.
  * **Lazy partial compaction**: For non-empty buffers, `get_buffer()` targets `end = max(_scan + need, READ_WINDOW)` (where `need` is `4`, `20` for an oversize header, or `4 + msg_len`). Consumed frames advance `_head` without copying; only when `end - _tail < MOVE_MAX` and the buffered remainder `p = _tail - _head <= MOVE_MAX` does `get_buffer()` copy `_view[_head:_tail]` down to `_view[:p]` and reset `_head = 0`.
* **Size Limit (`max_buffer + 88`) & Oversize Frame Skipping**:
  * Replies with `msg_len < 16` raise `ConnectionDropped`.
  * Replies with `msg_len > max_buffer + REPLY_OVERHEAD`:
    * Reserved/unsolicited frames (`xid <= 0`) immediately raise `ConnectionDropped`.
    * Request replies (`xid > 0`) become ready as soon as `20` bytes (`[4B len][16B ReplyHeader]`) are buffered. `pop_packet()` pops FIFO pending via `_pop_pending()`, verifies `header.xid == expected_xid` (raising `ConnectionDropped` on `SASL` or missing/empty pending, `RuntimeError` on mismatch), stores `_skip = max(0, end - _tail)` (keeping any bytes past `end` aligned if already buffered), and returns `ResponsePacket(..., error=ReplyTooLargeError(...))`.
    * While `_skip > 0`, `get_buffer()` returns `_empty_view` and `buffer_updated()` discards `min(_skip, nbytes)` bytes in place, retaining any trailing next-frame bytes without losing stream alignment.
* **Pull/Push Cycle in `ConnectionHandler._read_packet()`** ([connection.py](kazoo/protocol/connection.py)):
  1. Calls `decoder.pop_packet()`. If a frame (or actionable 20-byte oversize header) is already buffered, returns immediately without touching the socket.
  2. Otherwise calls `buf = decoder.get_buffer()` and `sock.recv_into(buf)` (never calling `buf.release()` because `get_buffer()` may return cached `_empty_view`).
  3. `nbytes == 0` -> resets decoder and raises `ConnectionDropped("socket connection broken")`.
  4. `nbytes > 0` -> calls `decoder.buffer_updated(nbytes)` and re-checks `pop_packet()`. If the reply is still incomplete (a short read), loops straight back to step 2 without `select`: more bytes may have arrived while the decoder ran.
  5. Only an empty read (`BlockingIOError`, or SSL `WANT_READ`/`WANT_WRITE`) leads to a wait: it loops back to step 2 immediately when SSL `pending() > 0`, otherwise waits via `handler.select([sock], [], [], timeout)` (`timeout` applies per wait for incoming bytes).
* **Unified `xid` Dispatch & In-Decoder Deserialization**:
  * `FrameDecoder(..., pop_pending=...)` removes `is_connect` / `is_sasl` state flags: ZooKeeper's `ConnectResponse` has `0` in its first `int` field (`protocolVersion`), so `decode_packet()` ([serialization.py](kazoo/protocol/serialization.py)) unpacks `reply_header_struct` (`!iqi`) once at offset `0` and dispatches uniformly on `xid`:
    * `0` (`ReservedXID.CONNECT`) -> `Connect.deserialize(frame, 0)`
    * `-1` (`ReservedXID.WATCH`) -> `WatchPacket.deserialize(frame, 0)`
    * `-2` (`ReservedXID.PING`) -> `PingResponse(zxid=zxid, err=err)`
    * `-4` (`ReservedXID.AUTH`) -> `AuthResponse(zxid=zxid, err=err)`
    * Other `xid <= 0` -> `ConnectionDropped(f"Unknown reserved xid: {xid}")`
    * `xid > 0` -> pops `(request, async_object, expected_xid)` from `pop_pending()`, verifies matching `xid`, returns `SASLResponse` if `request.type == SASL.type`, skips body parsing if `err != 0`, or calls `request.deserialize(frame, 16)` synchronously into `ResponsePacket` (capturing deserialization exceptions into `ResponsePacket.error`).
  * **Exact end-of-frame check**: every `deserialize(buffer, offset)` returns `(value, end_offset)`, and `decode_packet()` requires `end_offset == len(frame)`. Slicing clamps at the end of the frame, so an overstated last length field would otherwise decode silently; one comparison per frame covers every field, including each entry of `GetChildren` and `Transaction` replies. A mismatched request reply fails only that request with `MarshallingError` in `ResponsePacket.error` (the frame boundary is known, so the stream stays in sync). A mismatched or malformed connect, watch, or SASL frame raises `ConnectionDropped`, since there is no request to fail. Error replies (`err != 0`), ping, and auth frames carry no parsed body and are not checked. `Connect`'s optional trailing read-only byte is only consumed when present, so frames with and without it both match.
  * `pop_packet()` updates cursors *before* calling `decode_packet(frame_view)` inside `try ... finally: frame_view.release()`, ensuring every frame view is released immediately with zero lent-`memoryview` lifetime state across calls.

### Packet Dispatch

`_process_packet(packet)` ([connection.py](kazoo/protocol/connection.py)) pattern-matches on the 6 `ServerPacket` variants:
* `Connect` -> `ConnectResponseReceived(connect_result=packet)`
* `PingResponse` -> `PING_RECEIVED` (resets poll timeout accounting)
* `AuthResponse` -> updates `last_zxid`, pops `ReservedXID.AUTH` from `RequestManager`, resolves or fails the pending auth `AsyncResult`, emits `AuthFailedEvent` or `AuthPacketCompleted`
* `WatchPacket` -> collects and dispatches matching watchers via `WatcherManager` + `handler.dispatch_callback` (stripping `client.chroot`)
* `SASLResponse` -> updates `last_zxid`, raises `EXCEPTIONS[err]()` if non-zero, else emits `SASLChallengeReceived`
* `ResponsePacket` -> `_read_response(packet)` ([connection.py](kazoo/protocol/connection.py)): updates `last_zxid`, fails `async_object` with `packet.error` if present (`ReplyTooLargeError` logs at debug and leaves the session alive; `KazooRetry` does not retry it), maps non-`Exists`/`NoNodeError` codes via `EXCEPTIONS[header.err]()`, unchroots `Transaction` results, registers/removes request watchers via `WatcherManager.update_for_response`, resolves `async_object`, and emits `SessionClosedEvent` on `Close`.

---

## 6. Concurrency Handlers (`kazoo.interfaces` & `kazoo.handlers`)

`IHandler` ([kazoo/interfaces.py](kazoo/interfaces.py)) abstracts execution primitives so `ConnectionHandler` and `StateMachineDriver` behave uniformly across OS threads, Gevent, and Eventlet:

| Primitive / Mechanism | `SequentialThreadingHandler` ([threading.py](kazoo/handlers/threading.py)) | `SequentialGeventHandler` ([gevent.py](kazoo/handlers/gevent.py)) | `SequentialEventletHandler` ([eventlet.py](kazoo/handlers/eventlet.py)) |
| :--- | :--- | :--- | :--- |
| **Selector (`create_selector()`)** | `selectors.DefaultSelector()` | `gevent.selectors.DefaultSelector()` | `_EventletSelector` (`green_selectors.DefaultSelector()` wrapped with `eventlet.sleep(0)` before/after `select()`) |
| **Burst Yield (`yield_control()`)** | No-op (inherited default from `IHandler`) | `gevent.sleep(0)` every 64 cached frames | `eventlet.sleep(0)` every 64 cached frames |
| **Watch Queue (`callback_queue`)** | `queue.Queue` + daemon worker thread | `gevent.queue.Queue` + worker greenlet | `green_queue.LightQueue` + worker greenthread (with `_yield_before_after`) |
| **Completion Callbacks (`rawlink`)** | `completion_queue` (`queue.Queue`) + worker thread via `utils.AsyncResult` | Native `gevent.event.AsyncResult` hub dispatch | `completion_queue` (`LightQueue`) + worker greenthread via `utils.AsyncResult` |
| **State Listeners** | Executed inline on `send_event` thread inside `_make_state_change()` | Executed inline on `send_event` greenlet | Executed inline on `send_event` greenthread |
| **Locks & Async Primitives** | `threading.Lock`, `RLock`, `Event`, `daemon Thread` | `gevent.lock.Semaphore`, `RLock`, `Event`, `Greenlet` | `green_threading.Lock`, `RLock`, `Event`, `eventlet.spawn` |

Because watch callbacks and `AsyncResult` completion callbacks execute on dedicated sequential workers (or hub callbacks on Gevent), blocking user code inside a watch handler stalls subsequent watches **but does not stall the `zk_loop` I/O routine, heartbeats, or `AsyncResult` completions**. Blocking inside a `state_listeners` function, however, runs inline on the `send_event()` caller and delays effect execution until it returns.

---

## 7. End-to-End Execution Flows

### A. Startup, Handshake, SASL & Authentication Sequence

```mermaid
sequenceDiagram
    participant App as Caller (start / start_async)
    participant Client as KazooClient
    participant SM as StateMachineDriver
    participant Conn as ConnectionHandler
    participant ZK as ZooKeeper Server

    App->>Client: start_async()
    Client->>Conn: start() (create socketpair + selector)
    Conn->>SM: send_event(StartRequested)
    SM-->>Client: Inactive -> Connecting
    Conn->>Conn: spawn(zk_loop)
    Conn->>SM: send_event(ConnectAttemptStarted)
    Conn->>ZK: TCP/TLS connect (non-blocking)
    Conn->>Conn: _setup_selector() [zk_socket + wakeup]
    Conn->>SM: send_event(SocketConnected)
    SM-->>Conn: Connecting -> Handshaking + SendConnectPacket
    Conn->>ZK: send(Connect frame)
    ZK-->>Conn: Connect Response
    Conn->>SM: SOCKET_READABLE -> ConnectResponseReceived
    opt SASL configured
        SM-->>Conn: Handshaking -> Authenticating(SASL) + StartSASLHandshake
        loop Challenge / Response
            Conn->>ZK: SASL(token, xid)
            ZK-->>Conn: SASLResponse
            Conn->>SM: SASLChallengeReceived -> StepSASLHandshake
        end
        Conn->>SM: SASLCompleted
    end
    opt auth_data present
        loop Each (scheme, credential)
            SM-->>Conn: Authenticating(INITIAL_AUTH) + SendAuthPacket(xid=-4)
            Conn->>ZK: Auth(scheme, credential)
            ZK-->>Conn: AuthResponse(xid=-4)
            Conn->>SM: AuthPacketCompleted
        end
    end
    SM-->>Client: Connected / ConnectedReadOnly + ResolveConnectResult + EmitKazooStateChange(CONNECTED)
    Client-->>App: AsyncResult.set(True)
```

### B. Steady-State Request/Reply & Reconnect Recovery

1. **Enqueue**: Application thread calls `zk.get_async("/p")` -> `_call()` -> `_request_manager.enqueue()` -> `wake()` writes `b"\0"` to wakeup socketpair.
2. **Wake & Send**: `_poll_step()` wakes from `self._selector.select()`, drains `_read_sock`, checks `has_send_capacity(limit)`, dequeues `(req, async_res)` to `pending` with monotonic `xid`, encodes `[len][xid][type][payload]`, and calls `_socket.send(memoryview)` optimistically.
3. **Receive & In-Decoder Decode**: Server replies -> `_select()` returns `"zk_socket"` -> `_read_packet()` reads into `_view` via `recv_into()` -> `pop_packet()` correlates `xid` via `pop_pending()`, deserializes directly from the frame `memoryview` slice into `ResponsePacket`, and immediately releases `frame_view` -> `_read_response()` updates watches via `WatcherManager` and calls `async_res.set(value)` (or fails with `ReplyTooLargeError` if the reply exceeded `max_buffer + REPLY_OVERHEAD`).
4. **Burst Drain**: If additional complete frames remain buffered, `FrameDecoder.frame_ready` bypasses `_select()` for up to 64 replies before calling `handler.yield_control()`.
5. **Drop & Recovery**: On socket drop or timeout, `_exception_to_event` emits `ConnectionLostEvent` -> `Suspended` -> fails `pending` + queued requests (`FailPendingRequests`), invalidates one-shot & persistent watches with `EventType.NONE` (`InvalidateWatchers`), emits `KazooState.SUSPENDED`, closes socket and runs `_release_wakeup()`, then `zk_loop` backs off via `KazooRetry` and attempts the next server host—preserving `session_id`/`_session_passwd` so ZooKeeper can resume the session (or reject it as expired -> `ExpiredSession` -> `ResetSessionData` -> `KazooState.LOST`).

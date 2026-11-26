"""Zookeeper Protocol Connection Handler"""

from __future__ import annotations

import logging
import random
import selectors
import socket
import ssl
import time
from binascii import hexlify
from collections import defaultdict, deque
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    TypeVar,
    cast,
)

from typing_extensions import Buffer

from kazoo.exceptions import (
    EXCEPTIONS,
    AuthFailedError,
    ConnectionDropped,
    ConnectionLoss,
    InvalidStateTransitionError,
    KazooException,
    NoNodeError,
    OperationTimeoutError,
    ReplyTooLargeError,
    SASLException,
    SessionClosedRequireSaslError,
    SessionExpiredError,
    SessionMovedError,
    ThrottledOpError,
    _error_for_code,
)
from kazoo.loggingsupport import BLATHER
from kazoo.protocol.sasl import (
    SASLConfig,
    SASLExchange,
)
from kazoo.protocol.serialization import (
    JUTE_MAX_BUFFER,
    SASL,
    AddWatch,
    Auth,
    AuthResponse,
    Close,
    Connect,
    Exists,
    FrameDecoder,
    GetChildren,
    GetChildren2,
    Ping,
    PingResponse,
    RemoveWatches,
    ReservedXID,
    ResponsePacket,
    SASLResponse,
    Transaction,
    WatchPacket,
)
from kazoo.protocol.statemachine import (
    HEARTBEAT_DEADLINE_REACHED,
    PING_RECEIVED,
    POLL_TIMEOUT_EXPIRED,
    SOCKET_READABLE,
    WAKEUP_SIGNALED,
    Authenticating,
    AuthFailed,
    AuthFailedEvent,
    AuthPacketCompleted,
    AuthStage,
    ClientEvent,
    CloseSocket,
    ConfigureReadOnlyMode,
    ConfigureTimeouts,
    ConnectAttemptStarted,
    Connected,
    ConnectedReadOnly,
    ConnectionLostEvent,
    ConnectResponseReceived,
    DrainWakeupPipe,
    EnqueueCloseRequest,
    Handshaking,
    Inactive,
    ReadConnectResponsePacket,
    ReadSocketPacket,
    ResetSessionData,
    RWServerFoundEvent,
    SASLChallengeReceived,
    SASLCompleted,
    SendAuthPacket,
    SendConnectPacket,
    SendHeartbeatPing,
    SessionClosedEvent,
    SessionEstablished,
    SessionExpiredEvent,
    SideEffect,
    SocketConnected,
    StartRequested,
    StartSASLHandshake,
    StepSASLHandshake,
    StopConnectionLoop,
    UpdateSessionData,
)
from kazoo.protocol.states import (
    EVENT_TYPE_MAP,
    AddWatchMode,
    Callback,
    KeeperState,
    WatchedEvent,
    WatcherType,
)
from kazoo.retry import (
    ForceRetryError,
    KazooRetry,
    RetryFailedError,
)

if TYPE_CHECKING:
    from kazoo.client import KazooClient, WatchFunc
    from kazoo.interfaces import IAsyncResult, Socket, Threadlike
    from kazoo.protocol.serialization import ServerPacket


log = logging.getLogger(__name__)


# Special testing hook objects used to force a session expired error as
# if it came from the server
_SESSION_EXPIRED = object()
_CONNECTION_DROP = object()

STOP_CONNECTING = object()

CREATED_EVENT = 1
DELETED_EVENT = 2
CHANGED_EVENT = 3
CHILD_EVENT = 4

CLOSE_RESPONSE = Close.type

# Micro-optimization: We will always use the same ping instance
_PingInstance = Ping()


# removed from Python3+
def buffer(obj: Buffer, offset: int = 0) -> memoryview:
    return memoryview(obj)[offset:]


class RequestManager:
    """Encapsulates client request queuing, in-flight pending requests, and
    XID sequence generation.
    """

    def __init__(self) -> None:
        self.queue: deque[tuple[Any, IAsyncResult | None]] = deque()
        self.pending: deque[tuple[Any, IAsyncResult | None, int]] = deque()
        self.xid: int | None = None

    def reset_xid(self, value: int = 0) -> None:
        """Reset the XID counter for a new connection attempt."""
        self.xid = value

    def next_xid(self) -> int:
        """Advance and return the next positive 31-bit XID."""
        self.xid = ((self.xid or 0) % 2147483647) + 1
        return self.xid

    def enqueue(
        self, request: Any, async_object: IAsyncResult | None = None
    ) -> None:
        """Append a request and its completion handle to the send queue."""
        self.queue.append((request, async_object))

    def remove_queued(
        self, request: Any, async_object: IAsyncResult | None
    ) -> None:
        """Remove a specific request pair from the send queue if present.

        Uses O(n) ``deque.remove``; for rare wake-failure cleanup only.
        """
        try:
            self.queue.remove((request, async_object))
        except ValueError:
            pass

    def peek_queued(self) -> tuple[Any, IAsyncResult | None]:
        """Return the head of the send queue without removing it."""
        return self.queue[0]

    def dequeue_to_pending(self, xid: int) -> tuple[Any, IAsyncResult | None]:
        """Pop the oldest queued request and record it as pending with xid."""
        request, async_object = self.queue.popleft()
        self.pending.append((request, async_object, xid))
        return request, async_object

    def pop_pending(self) -> tuple[Any, IAsyncResult | None, int]:
        """Pop and return the oldest in-flight pending request."""
        return self.pending.popleft()

    def pop_auth_pending(
        self, auth_xid: int = ReservedXID.AUTH
    ) -> IAsyncResult | None:
        """Find and remove the first pending Auth request matching auth_xid."""
        pending = self.pending
        for idx, (_, a_obj, xid) in enumerate(pending):
            if xid == auth_xid:
                del pending[idx]
                return a_obj
        return None

    def has_send_capacity(self, limit: int | None) -> bool:
        """Return True if the number of pending requests is below limit."""
        return limit is None or len(self.pending) < limit

    def fail_all(self, state: KeeperState) -> None:
        """Fail all pending and queued requests with the state's exception."""
        exc: KazooException
        if state == KeeperState.AUTH_FAILED:
            exc = AuthFailedError()
        elif state == KeeperState.EXPIRED_SESSION:
            exc = SessionExpiredError()
        else:
            exc = ConnectionLoss()

        pending = self.pending
        while True:
            try:
                _, async_object, _ = pending.popleft()
                if async_object:
                    async_object.set_exception(exc)
            except IndexError:
                break

        queue = self.queue
        while True:
            try:
                _, async_object = queue.popleft()
                if async_object:
                    async_object.set_exception(exc)
            except IndexError:
                break

    def reset(self, state: KeeperState | None = None) -> None:
        """Fail any remaining requests and reinitialize fresh deques."""
        if state is not None:
            self.fail_all(state)
        self.queue = deque()
        self.pending = deque()


class WatcherManager:
    """Encapsulates client data, child, persistent, and recursive watchers."""

    def __init__(self) -> None:
        self.child_watchers: defaultdict[str, set[WatchFunc]] = defaultdict(
            set
        )
        self.data_watchers: defaultdict[str, set[WatchFunc]] = defaultdict(set)
        self.persistent_watchers: defaultdict[str, set[WatchFunc]] = (
            defaultdict(set)
        )
        self.persistent_recursive_watchers: defaultdict[
            str, set[WatchFunc]
        ] = defaultdict(set)

    def find_persistent_recursive(self, path: str) -> list[WatchFunc]:
        """Return persistent recursive watchers matching path or ancestors."""
        watchers: list[WatchFunc] = []
        candidates = ["/"]
        stripped = path.strip("/")
        if stripped:
            parts = stripped.split("/")
            candidates.extend(
                "/" + "/".join(parts[: i + 1]) for i in range(len(parts))
            )
        pr_watchers = self.persistent_recursive_watchers
        for candidate in candidates:
            watchers.extend(pr_watchers.get(candidate, []))
        return watchers

    def collect_for_event(
        self, watch_type: int, path: str
    ) -> list[WatchFunc] | None:
        """Pop one-shot watchers and collect persistent watchers for event."""
        watchers: list[WatchFunc] = []
        if watch_type in (CREATED_EVENT, CHANGED_EVENT):
            watchers.extend(self.data_watchers.pop(path, []))
            watchers.extend(self.persistent_watchers.get(path, []))
            watchers.extend(self.find_persistent_recursive(path))
        elif watch_type == DELETED_EVENT:
            watchers.extend(self.data_watchers.pop(path, []))
            watchers.extend(self.child_watchers.pop(path, []))
            watchers.extend(self.persistent_watchers.get(path, []))
            watchers.extend(self.find_persistent_recursive(path))
        elif watch_type == CHILD_EVENT:
            watchers.extend(self.child_watchers.pop(path, []))
            watchers.extend(self.persistent_watchers.get(path, []))
        else:
            return None
        return watchers

    def update_for_response(self, request: Any) -> None:
        """Register or remove watchers after a successful request response."""
        watcher = getattr(request, "watcher", None)
        if watcher:
            if isinstance(request, AddWatch):
                if request.mode == AddWatchMode.PERSISTENT:
                    self.persistent_watchers[request.path].add(watcher)
                elif request.mode == AddWatchMode.PERSISTENT_RECURSIVE:
                    self.persistent_recursive_watchers[request.path].add(
                        watcher
                    )
                else:
                    raise ValueError(
                        f"Unexpected AddWatchMode: {request.mode}"
                    )
            elif isinstance(request, (GetChildren, GetChildren2)):
                self.child_watchers[request.path].add(watcher)
            else:
                self.data_watchers[request.path].add(watcher)
        if isinstance(request, RemoveWatches):
            if request.watcher_type == WatcherType.CHILDREN:
                self.child_watchers.pop(request.path, None)
            elif request.watcher_type == WatcherType.DATA:
                self.data_watchers.pop(request.path, None)
            elif request.watcher_type == WatcherType.ANY:
                self.child_watchers.pop(request.path, None)
                self.data_watchers.pop(request.path, None)
                self.persistent_watchers.pop(request.path, None)
                self.persistent_recursive_watchers.pop(request.path, None)
            else:
                raise ValueError(
                    f"Unexpected WatcherType: {request.watcher_type}"
                )

    def reset(self) -> list[WatchFunc]:
        """Clear all watcher tables and return previously registered ones."""
        watchers: list[WatchFunc] = []
        for child_w in self.child_watchers.values():
            watchers.extend(child_w)
        for data_w in self.data_watchers.values():
            watchers.extend(data_w)
        for pers_w in self.persistent_watchers.values():
            watchers.extend(pers_w)
        for pr_w in self.persistent_recursive_watchers.values():
            watchers.extend(pr_w)

        self.child_watchers = defaultdict(set)
        self.data_watchers = defaultdict(set)
        self.persistent_watchers = defaultdict(set)
        self.persistent_recursive_watchers = defaultdict(set)
        return watchers


class RWPinger:
    """A Read/Write Server Pinger Iterable

    This object is initialized with resolved ``(host, hostip, port)``
    tuples and a socket creation function. Anytime ``next`` is called on
    its iterator it yields either ``False``, or a ``(host, hostip, port)``
    tuple if it found a r/w capable Zookeeper node.

    After the first run-through of hosts, an exponential back-off delay
    is added before the next run. This delay is tracked internally and
    the iterator will yield ``False`` if called too soon.

    """

    def __init__(
        self,
        hosts: list[tuple[str, str, int]],
        connection_func: Callable[[str, str, int, float | None], Socket],
        socket_handling: Callable[[], AbstractContextManager[None]],
        timeout: float = 1.0,
        logger: logging.Logger | None = None,
    ):
        self.hosts = hosts
        self.connection = connection_func
        self.last_attempt: float | None = None
        self.socket_handling = socket_handling
        self.timeout = timeout
        self.logger = logger or log

    def __iter__(
        self,
    ) -> Iterator[tuple[str, str, int] | Literal[False]]:
        delay = 0.5
        while True:
            if self.last_attempt is not None:
                jitter = random.randint(0, 100) / 100.0
                while time.monotonic() < self.last_attempt + delay + jitter:
                    # Skip rw ping checks if its too soon
                    yield False
                delay = min(delay * 2, 60.0)
            for idx, (host, hostip, port) in enumerate(self.hosts):
                self.logger.debug(
                    "Pinging server for r/w: %s(%s):%s",
                    host,
                    hostip,
                    port,
                )
                self.last_attempt = time.monotonic()
                rw_host: tuple[str, str, int] | None = None
                try:
                    with self.socket_handling():
                        sock = self.connection(
                            host, hostip, port, self.timeout
                        )
                        try:
                            sock.settimeout(self.timeout)
                            sock.sendall(b"isro")
                            result = sock.recv(8192)
                        finally:
                            sock.close()
                        if result == b"rw":
                            rw_host = (host, hostip, port)
                except (OSError, ConnectionDropped):
                    pass

                if rw_host is not None:
                    delay = 0.5
                    yield rw_host
                elif idx < len(self.hosts) - 1:
                    # Probe at most one endpoint per heartbeat tick.
                    yield False
            # No RW server found in this pass; retry after back-off.
            self.last_attempt = time.monotonic()
            yield False


class RWServerAvailable(Exception):
    """Thrown if a RW Server becomes available"""


ReturnValue = TypeVar("ReturnValue")


def _exception_to_event(
    exc: BaseException,
    timeout_exception: type[BaseException],
    session_id: int | None,
    rw_server: tuple[str, int] | None,
    logger: logging.Logger,
) -> tuple[ClientEvent, bool, bool] | None:
    """Map an I/O error to ``(event, reset_retry, stop_connecting)``.

    Returns ``None`` if ``exc`` is not a handled connection exception.
    """
    if isinstance(exc, timeout_exception):
        logger.warning("Connection time-out: %s", exc)
        return ConnectionLostEvent(reason=str(exc)), False, False
    if isinstance(exc, (OSError, ConnectionDropped)):
        logger.warning("Connection dropped: %s", exc)
        return ConnectionLostEvent(reason=str(exc)), False, False
    if isinstance(
        exc,
        (AuthFailedError, SASLException, SessionClosedRequireSaslError),
    ):
        if isinstance(exc, SessionClosedRequireSaslError):
            logger.warning(
                "AUTH_FAILED closing (server requires SASL auth): %s", exc
            )
        else:
            logger.warning("AUTH_FAILED closing: %s", exc)
        return AuthFailedEvent(error=exc), True, True
    if isinstance(exc, SessionExpiredError):
        logger.warning("Session has expired")
        return SessionExpiredEvent(session_id=session_id), True, False
    if isinstance(exc, RWServerAvailable):
        logger.warning("Found a RW server, dropping connection")
        return RWServerFoundEvent(server=rw_server), True, False
    return None


class ConnectionHandler:
    """Zookeeper connection handler"""

    _REPLIES_PER_YIELD = 64

    def __init__(
        self,
        client: KazooClient,
        retry_sleeper: KazooRetry,
        logger: logging.Logger | None = None,
        sasl_config: SASLConfig | None = None,
        max_buffer: int = JUTE_MAX_BUFFER,
    ):
        self.client = client
        self.handler = client.handler
        self.retry_sleeper = retry_sleeper
        self.retry_sleeper.interrupt = lambda: self._is_terminal
        self.logger = logger or log

        # Our event objects
        self.connection_closed = client.handler.event_object()
        self.connection_closed.set()
        self.connection_stopped = client.handler.event_object()
        self.connection_stopped.set()

        self._read_sock: Socket | None = None
        self._write_sock: Socket | None = None

        self._socket: Socket | None = None
        self._socket_is_ssl: bool = False
        self._selector: selectors.BaseSelector | None = None
        self._requests: RequestManager = client._request_manager
        self._watchers: WatcherManager = client._watcher_manager
        self._frame_decoder = FrameDecoder(
            max_buffer=max_buffer,
            pop_pending=self._requests.pop_pending,
        )
        self._replies_since_select: int = 0
        self._yield_control = client.handler.yield_control

        self._host_ports: list[tuple[str, str, int]] = []
        self._rw_server: tuple[str, str, int] | None = None
        self._ro_mode: (
            Iterator[Literal[False] | tuple[str, str, int]]
            | Literal[False]
            | None
        ) = False

        self._connection_routine: Threadlike | None = None
        self._last_send: float = 0.0
        self._read_timeout: float = 10.0
        self._connect_timeout: float = 10.0
        self._phase_deadline: float | None = None

        self.sasl_config: SASLConfig | None = sasl_config
        self._sasl_exchange: SASLExchange | None = None

        self._current_retry: KazooRetry | None = None

    @property
    def _is_session_active(self) -> bool:
        return self.client._state_machine.state.is_connected

    @property
    def _is_terminal(self) -> bool:
        return self._check_is_terminal()

    def _check_is_terminal(self) -> bool:
        # A method, not only the property above, on purpose: code that
        # re-checks after a blocking call must use this form. mypy narrows
        # a bool property to False after ``if self._is_terminal: return``
        # and then reports the later re-check as unreachable, although
        # another thread can change the state in between.
        state = self.client._state_machine.state
        return state.is_terminal or not state.is_connecting_allowed

    @property
    def _wakeup_registered(self) -> bool:
        if self._selector is None or self._read_sock is None:
            return False
        try:
            self._selector.get_key(self._read_sock)
            return True
        except (KeyError, ValueError):
            return False

    def _emit_event(self, event: ClientEvent) -> None:
        """Send an event to the client state machine."""
        try:
            self.client.send_event(event)
        except InvalidStateTransitionError as exc:
            self.logger.warning(
                "Unexpected event %r in state %r; dropping connection",
                event,
                self.client.state_machine,
            )
            raise ConnectionDropped(
                f"unexpected event {event!r} in {self.client.state_machine!r}"
            ) from exc

    def _setup_selector(self) -> None:
        """Register active sockets on the persistent selector."""
        self._update_io_interest(selectors.EVENT_READ, "zk_socket")
        self._update_io_interest(selectors.EVENT_READ, "wakeup")

    def _close_selector(self) -> None:
        """Close the persistent selector if active."""
        if self._selector is not None:
            try:
                self._selector.close()
            except Exception:
                pass
            self._selector = None

    def _close_socket(self) -> None:
        """Unregister and close the active transport socket."""
        self._update_io_interest(0, "zk_socket")
        if self._socket is not None:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None
        self._socket_is_ssl = False
        self._replies_since_select = 0
        self._phase_deadline = None
        self._frame_decoder.reset()

    def _update_io_interest(
        self, interest_mask: int, target: Literal["zk_socket", "wakeup"]
    ) -> None:
        """Set the selector registration for the target socket.

        Called exclusively from the connection routine (and in stop() only
        after the connection routine has exited).
        """
        sock = self._read_sock if target == "wakeup" else self._socket
        sel = self._selector
        if sock is None or sel is None:
            return
        if interest_mask:
            # Usually a transition from unregistered, so register
            # first: a modify() miss costs more than a register() hit.
            try:
                sel.register(sock, interest_mask, data=target)
            except KeyError:
                sel.modify(sock, interest_mask, data=target)
        else:
            try:
                sel.unregister(sock)
            except (KeyError, ValueError):
                pass

    def _select(self, timeout: float) -> set[str]:
        """Wait for readable events on the persistent selector."""
        has_ssl_pending = (
            self._socket_is_ssl
            and self._socket is not None
            and cast(ssl.SSLSocket, self._socket).pending() > 0
        )
        assert self._selector is not None
        events = self._selector.select(0.0 if has_ssl_pending else timeout)

        ready: set[str] = {key.data for key, _ in events}
        if has_ssl_pending:
            ready.add("zk_socket")
        return ready

    def _dispose_sasl(self) -> None:
        """Dispose of active SASL exchange if present."""
        if self._sasl_exchange is not None:
            self._sasl_exchange.dispose()
            self._sasl_exchange = None

    def _start_sasl(self, host: str) -> None:
        """Initialize the SASL exchange and send the initial token."""
        self._dispose_sasl()
        if not self.sasl_config:
            self._emit_event(SASLCompleted())
            return
        self._sasl_exchange = SASLExchange(self.sasl_config, host)
        self.client.sasl_cli = (  # type: ignore[attr-defined]
            self._sasl_exchange.client
        )
        self._step_sasl(challenge=None)

    def _step_sasl(self, challenge: bytes | None) -> None:
        """Advance the SASL exchange by one step."""
        if self._sasl_exchange is None:
            return
        if self._sasl_exchange.is_complete:
            self._dispose_sasl()
            self._emit_event(SASLCompleted())
            return
        token = self._sasl_exchange.next_token(challenge)
        if token is None:
            self._dispose_sasl()
            self._emit_event(SASLCompleted())
            return
        req = SASL(token)
        xid = self._requests.next_xid()
        self._requests.pending.append((req, None, xid))
        self._submit(req, self._connect_timeout, xid)
        self._last_send = time.monotonic()

    def execute_effect(self, effect: SideEffect) -> None:
        """Execute I/O and selector side effects from the state machine."""
        match effect:
            case ReadSocketPacket():
                self._read_socket(self._read_timeout)

            case DrainWakeupPipe():
                self._drain_wakeup()

            case SendHeartbeatPing():
                self._send_ping(self._connect_timeout)
                self._last_send = time.monotonic()

            case StartSASLHandshake(host=host):
                self._phase_deadline = time.monotonic() + self._connect_timeout
                self._start_sasl(host)

            case StepSASLHandshake(challenge=challenge):
                self._phase_deadline = time.monotonic() + self._connect_timeout
                self._step_sasl(challenge)

            case SendAuthPacket(scheme=scheme, credential=credential):
                self._phase_deadline = time.monotonic() + self._connect_timeout
                self._submit(
                    Auth(0, scheme, credential),
                    self._connect_timeout,
                    ReservedXID.AUTH,
                )
                self._last_send = time.monotonic()

            case EnqueueCloseRequest():
                self._requests.enqueue(Close(), None)
                self.wake()

            case CloseSocket():
                self._close_socket()

            case SessionEstablished():
                self._phase_deadline = None
                if self._current_retry is not None:
                    self._current_retry.reset()
                if self._requests.queue:
                    self.wake()

            case ResetSessionData():
                self._frame_decoder.reset()
                if self._current_retry is not None:
                    self._current_retry.reset()

            case StopConnectionLoop():
                self.wake()

            case SendConnectPacket(connect=connect, timeout=timeout):
                self._connect_timeout = timeout
                self._phase_deadline = time.monotonic() + timeout
                self._submit(connect, timeout)
                self._last_send = time.monotonic()

            case ReadConnectResponsePacket():
                packet = self._read_packet(self._connect_timeout)
                if packet is None:
                    return
                if not isinstance(packet, Connect):
                    raise ConnectionDropped(
                        "Unexpected packet during handshake: "
                        f"{type(packet).__name__}"
                    )
                self._process_packet(packet)

            case UpdateSessionData(
                session_id=sid,
                session_passwd=passwd,
                protocol_version=proto_version,
                last_zxid=zxid,
            ):
                self.client._session_id = sid
                self.client._session_passwd = passwd
                self.client._protocol_version = proto_version
                if zxid is not None and zxid > 0:
                    self.client.last_zxid = zxid

            case ConfigureTimeouts(
                read_timeout=r_timeout, connect_timeout=c_timeout
            ):
                self._read_timeout = r_timeout
                self._connect_timeout = c_timeout
                self._last_send = time.monotonic()

            case ConfigureReadOnlyMode(read_only=ro_enabled):
                if ro_enabled:
                    self._ro_mode = iter(self._server_pinger())
                else:
                    self._ro_mode = None

    def start(self) -> None:
        """Start the connection up"""
        if self.connection_closed.is_set():
            rw_sockets = self.handler.create_socket_pair()
            self._read_sock, self._write_sock = rw_sockets
            self._read_sock.setblocking(False)
            self._write_sock.setblocking(False)
            # The wakeup socket is registered per connection attempt, by
            # _setup_selector, and not here: see _release_wakeup.
            self._selector = self.handler.create_selector()
            self.connection_closed.clear()
        if self._connection_routine:
            raise Exception(
                "Unable to start, connection routine already active."
            )
        self.connection_stopped.clear()
        self._emit_event(StartRequested())
        self._connection_routine = self.handler.spawn(self.zk_loop)

    def stop(self, timeout: float | None = None) -> bool:
        """Ensure the writer has stopped, wait to see if it does."""
        if not self.connection_stopped.wait(timeout):
            return False
        if self._connection_routine:
            self._connection_routine.join()
            self._connection_routine = None
        # client.stop() runs StopConnectionLoop on this thread. When the
        # routine had already returned, nothing consumed that wake-up.
        self._release_wakeup()
        return True

    def close(self) -> None:
        """Release resources held by the connection

        The connection can be restarted afterwards.
        """
        if not self.connection_stopped.is_set():
            raise Exception("Cannot close connection until it is stopped")
        self.connection_closed.set()
        self._close_selector()
        ws, rs = self._write_sock, self._read_sock
        self._write_sock = self._read_sock = None
        if ws is not None:
            ws.close()
        if rs is not None:
            rs.close()

    @contextmanager
    def _socket_error_handling(self) -> Iterator[None]:
        try:
            yield
        except OSError as e:
            err = getattr(e, "strerror", e)
            raise ConnectionDropped("socket connection error: %s" % (err,))

    def _open_socket(
        self,
        host: str,
        hostip: str,
        port: int,
        timeout: float | None,
    ) -> Socket:
        client = self.client
        return self.handler.create_connection(
            address=(hostip, port),
            hostname=host,
            timeout=timeout,
            use_ssl=client.use_ssl,
            keyfile=client.keyfile,
            certfile=client.certfile,
            ca=client.ca,
            keyfile_password=client.keyfile_password,
            verify_certs=client.verify_certs,
            check_hostname=client.check_hostname,
        )

    def wake(self) -> bool:
        """Wake the connection loop by sending a byte on the wakeup pipe.

        Returns True if the byte was sent successfully, False otherwise.
        """
        sock = self._write_sock
        if sock is None:
            return False
        try:
            sock.send(b"\0")
            return True
        except BlockingIOError:
            return True
        except OSError:
            return False

    def _drain_wakeup(self) -> None:
        """Drain all bytes from the non-blocking wakeup pipe."""
        sock = self._read_sock
        if sock is None:
            return
        while True:
            try:
                chunk = sock.recv(4096)
                if not chunk or not isinstance(chunk, (bytes, bytearray)):
                    break
            except (BlockingIOError, OSError):
                break

    def _release_wakeup(self) -> None:
        """Unregister the wakeup socket and discard pending wake bytes.

        The wakeup socket is registered only while a connection attempt
        is selecting. _setup_selector registers it for each attempt; this
        releases it when a wake byte has nothing to interrupt: outside a
        session, at the end of every attempt, when the connection routine
        exits and in stop(). The bytes carry no information; the routine
        reads the state machine and the request queue itself.

        This must not be left to the next select(). A selector may keep
        watching a registered socket between select() calls. gevent's
        does, and a wake byte that arrives while the routine sleeps in
        the retry back-off, or after it has returned, then makes the hub
        loop spin until the next select() or until close().
        """
        self._update_io_interest(0, "wakeup")
        self._drain_wakeup()

    def _server_pinger(self) -> RWPinger:
        """Returns a server pinger iterable, that will ping the next
        server in the list, and apply a back-off between attempts."""
        return RWPinger(
            list(self._host_ports),
            self._open_socket,
            self._socket_error_handling,
            timeout=min(self.client._session_timeout / 3000.0, 1.0),
            logger=self.logger,
        )

    def _read_packet(
        self, timeout: float | None = None
    ) -> ServerPacket | None:
        """Read and decode the next typed server packet via FrameDecoder.

        Non-blocking: returns ``None`` when the socket would block before a
        complete frame is available so all waiting happens in the unified
        selector loop (including oversize-frame skipping and TLS control
        records).
        """
        packet = self._frame_decoder.pop_packet()
        if packet is not None:
            return packet

        while True:
            buf = self._frame_decoder.get_buffer()
            try:
                nbytes = self._socket.recv_into(  # type: ignore[union-attr]
                    buf
                )
            except BlockingIOError:
                if (
                    self._socket_is_ssl
                    and cast(ssl.SSLSocket, self._socket).pending() > 0
                ):
                    continue
                return None
            except ssl.SSLError as e:
                if e.errno == ssl.SSL_ERROR_WANT_READ:
                    if cast(ssl.SSLSocket, self._socket).pending() > 0:
                        continue
                    return None
                if e.errno == ssl.SSL_ERROR_WANT_WRITE:
                    eff = self._read_timeout if timeout is None else timeout
                    if not self.handler.select(
                        [], [cast("Socket", self._socket)], [], eff
                    )[1]:
                        raise self.handler.timeout_exception(
                            "socket time-out during read"
                        )
                    continue
                raise

            if nbytes == 0:
                self._frame_decoder.reset()
                raise ConnectionDropped("socket connection broken")
            self._frame_decoder.buffer_updated(nbytes)
            packet = self._frame_decoder.pop_packet()
            if packet is not None:
                return packet

    def _submit(
        self,
        request: Auth | Connect | Ping | SASL,
        timeout: float | None,
        xid: int | None = None,
    ) -> None:
        """Submit a request object with a timeout value and optional
        xid"""
        self.logger.log(
            (BLATHER if isinstance(request, Ping) else logging.DEBUG),
            "Sending request(xid=%s): %s",
            xid,
            request,
        )
        self._write(FrameDecoder.encode_request(request, xid), timeout)

    def _write(self, msg: bytes, timeout: float | None = None) -> None:
        """Write raw bytes to the socket.

        Attempts an optimistic non-blocking send first for the fast-path.
        If a partial send occurs or socket buffer is full, drains the
        remainder using handler.select bounded by timeout.
        """
        if self._socket is None:
            raise ConnectionDropped("socket closed")

        sent = 0
        msg_length = len(msg)
        view = memoryview(msg)

        # Fast-path: optimistic direct non-blocking send
        try:
            bytes_sent = self._socket.send(view)
            if not bytes_sent:
                raise ConnectionDropped("socket connection broken")
            if bytes_sent >= msg_length:
                return
            sent = bytes_sent
        except BlockingIOError:
            pass
        except ssl.SSLError as e:
            if e.errno not in (
                ssl.SSL_ERROR_WANT_READ,
                ssl.SSL_ERROR_WANT_WRITE,
            ):
                raise
        except OSError as e:
            raise ConnectionDropped(f"socket connection error: {e}") from e

        # Slow-path: partial write or buffer full, drain bounded by timeout
        eff_timeout = self._connect_timeout if timeout is None else timeout
        deadline = time.monotonic() + eff_timeout

        while sent < msg_length:
            rem_timeout = max(0.0, deadline - time.monotonic())
            s = self.handler.select([], [self._socket], [], rem_timeout)[1]
            if not s:
                raise self.handler.timeout_exception(
                    "socket time-out during write"
                )
            try:
                bytes_sent = self._socket.send(view[sent:])
            except BlockingIOError:
                continue
            except ssl.SSLError as e:
                if e.errno in (
                    ssl.SSL_ERROR_WANT_READ,
                    ssl.SSL_ERROR_WANT_WRITE,
                ):
                    continue
                else:
                    raise
            except OSError as e:
                raise ConnectionDropped(f"socket connection error: {e}") from e
            if not bytes_sent:
                raise ConnectionDropped("socket connection broken")
            sent += bytes_sent

    def _find_persistent_recursive_watchers(
        self, path: str
    ) -> list[WatchFunc]:
        return self._watchers.find_persistent_recursive(path)

    def _read_watch_event(self, watch: WatchPacket) -> None:
        client = self.client
        path = watch.path

        self.logger.debug("Received EVENT: %s", watch)

        watchers = self._watchers.collect_for_event(watch.type, path)
        if watchers is None:
            self.logger.warn("Received unknown event %r", watch.type)
            return

        # Strip the chroot if needed
        path = client.unchroot(path)
        ev = WatchedEvent(
            EVENT_TYPE_MAP[watch.type], client.client_state, path, watch.zxid
        )

        # Last check to ignore watches if we've been stopped or terminated
        if self._is_terminal:
            return

        # Dump the watchers to the watch thread
        for watch1 in watchers:
            client.handler.dispatch_callback(Callback("watch", watch1, (ev,)))

    def _update_watchers_for_response(self, request: Any) -> None:
        """Register or remove client watchers after a successful response."""
        self._watchers.update_for_response(request)

    def _read_response(self, packet: ResponsePacket) -> object | None:
        client = self.client
        header = packet.header
        request = packet.request
        async_object = packet.async_object
        xid = header.xid
        if header.zxid and header.zxid > 0:
            client.last_zxid = header.zxid

        if packet.error is not None:
            if isinstance(packet.error, ReplyTooLargeError):
                self.logger.debug(
                    "Received error(xid=%s) %r", xid, packet.error
                )
            else:
                self.logger.error(
                    "Exception raised during deserialization of request: %s",
                    request,
                    exc_info=packet.error,
                )
            if async_object is not None:
                async_object.set_exception(packet.error)
            if isinstance(request, Close):
                self.logger.log(BLATHER, "Read close response")
                self._emit_event(SessionClosedEvent())
                return CLOSE_RESPONSE
            return None

        # Determine if its an exists request and a no node error
        exists_error = (
            header.err == NoNodeError.code and request.type == Exists.type
        )

        # Set the exception if its not an exists error
        if header.err and not exists_error:
            callback_exception = _error_for_code(header.err)
            self.logger.debug(
                "Received error(xid=%s) %r", xid, callback_exception
            )
            if async_object:
                async_object.set_exception(callback_exception)
        elif request and async_object:
            if exists_error:
                # It's a NoNodeError, which is fine for an exists
                # request
                response = None
            else:
                response = packet.response
                self.logger.debug(
                    "Received response(xid=%s): %r", xid, response
                )

                # We special case a Transaction as we have to unchroot things
                if request.type == Transaction.type:
                    response = Transaction.unchroot(client, response)

            # Determine if watchers should be registered or unregistered
            if not self._is_terminal:
                self._update_watchers_for_response(request)

            async_object.set(response)

        if isinstance(request, Close):
            self.logger.log(BLATHER, "Read close response")
            self._emit_event(SessionClosedEvent())
            return CLOSE_RESPONSE
        return None

    def _process_packet(self, packet: ServerPacket) -> object | None:
        """Process a decoded ServerPacket from FrameDecoder."""
        client = self.client
        state = client._state_machine.state
        match packet:
            case Connect():
                self._emit_event(
                    ConnectResponseReceived(connect_result=packet)
                )
            case PingResponse():
                if not self._is_session_active:
                    raise ConnectionDropped(
                        f"Unexpected PingResponse in {type(state).__name__}"
                    )
                self.logger.log(BLATHER, "Received Ping")
                self._emit_event(PING_RECEIVED)
            case AuthResponse(zxid=zxid, err=err_code):
                if not (
                    self._is_session_active
                    or (
                        isinstance(state, Authenticating)
                        and state.stage == AuthStage.INITIAL_AUTH
                    )
                ):
                    raise ConnectionDropped(
                        f"Unexpected AuthResponse in {type(state).__name__}"
                    )
                self.logger.log(BLATHER, "Received AUTH")
                if zxid and zxid > 0:
                    client.last_zxid = zxid
                async_object = self._requests.pop_auth_pending(
                    ReservedXID.AUTH
                )
                if err_code:
                    err = (
                        EXCEPTIONS[err_code]()
                        if err_code in EXCEPTIONS
                        else AuthFailedError()
                    )
                    if async_object is not None:
                        async_object.set_exception(err)
                    self._emit_event(AuthFailedEvent(error=err))
                else:
                    if async_object is not None:
                        async_object.set(True)
                    self._emit_event(AuthPacketCompleted())
            case WatchPacket():
                if not self._is_session_active:
                    raise ConnectionDropped(
                        f"Unexpected WatchPacket in {type(state).__name__}"
                    )
                self._read_watch_event(packet)
            case SASLResponse(
                xid=xid, zxid=zxid, err=err_code, challenge=challenge
            ):
                if not (
                    isinstance(state, Authenticating)
                    and state.stage == AuthStage.SASL
                ):
                    raise ConnectionDropped(
                        f"Unexpected SASLResponse in {type(state).__name__}"
                    )
                if zxid > 0:
                    client.last_zxid = zxid
                if err_code:
                    if err_code not in EXCEPTIONS:
                        raise AuthFailedError(f"SASL error {err_code}")
                    callback_exception = EXCEPTIONS[err_code]()
                    self.logger.debug(
                        "Received error(xid=%s) %r",
                        xid,
                        callback_exception,
                    )
                    if isinstance(
                        callback_exception,
                        (
                            AuthFailedError,
                            SessionClosedRequireSaslError,
                            SessionExpiredError,
                        ),
                    ):
                        raise callback_exception
                    if isinstance(
                        callback_exception,
                        (
                            ConnectionLoss,
                            OperationTimeoutError,
                            SessionMovedError,
                            ThrottledOpError,
                        ),
                    ):
                        raise ConnectionDropped(
                            f"Transient SASL error: {callback_exception!r}"
                        ) from callback_exception
                    raise AuthFailedError(
                        f"SASL error: {callback_exception!r}"
                    ) from callback_exception
                self._emit_event(SASLChallengeReceived(challenge=challenge))
            case ResponsePacket():
                if not self._is_session_active:
                    raise ConnectionDropped(
                        f"Unexpected ResponsePacket in {type(state).__name__}"
                    )
                self.logger.log(
                    BLATHER, "Reading for header %r", packet.header
                )
                return self._read_response(packet)
        return None

    def _read_socket(
        self,
        read_timeout: float | None = None,
    ) -> object | None:
        """Called when there's something to read on the socket"""
        eff_timeout = (
            self._read_timeout if read_timeout is None else read_timeout
        )
        packet = self._read_packet(eff_timeout)
        if packet is None:
            return None
        return self._process_packet(packet)

    def _send_request(
        self,
        read_timeout: float,
        connect_timeout: float,
    ) -> None:
        """Called when we have something to send out on the socket.

        When terminal (e.g. during client.stop()), all queued requests bypass
        the concurrent request limit window so that shutdown completes promptly
        without waiting behind a saturated pipeline.
        """
        limit = self.client.concurrent_request_limit
        terminal = self._is_terminal
        while terminal or self._requests.has_send_capacity(limit):
            try:
                request, _ = self._requests.peek_queued()
            except IndexError:
                return

            # Special case for testing, if this is a _SessionExpire object
            # then throw a SessionExpiration error as if we were dropped
            if request is _SESSION_EXPIRED:
                raise SessionExpiredError("Session expired: Testing")
            if request is _CONNECTION_DROP:
                raise ConnectionDropped("Connection dropped: Testing")

            # Special case for auth packets
            if request.type == Auth.type:
                xid: int = ReservedXID.AUTH
            else:
                xid = self._requests.next_xid()

            self._requests.dequeue_to_pending(xid)
            self._submit(request, connect_timeout, xid)
            self._last_send = time.monotonic()

    def _send_ping(self, connect_timeout: float) -> None:
        self._submit(_PingInstance, connect_timeout, ReservedXID.PING)

        # Determine if we need to check for a r/w server
        if self._ro_mode:
            result = next(self._ro_mode)
            if result:
                self._rw_server = result
                raise RWServerAvailable()

    def zk_loop(self) -> None:
        """Main Zookeeper handling loop"""
        self.logger.log(BLATHER, "ZK loop started")

        self.connection_stopped.clear()

        retry = self.retry_sleeper.copy()
        try:
            while not self._is_terminal:
                # If the connect_loop returns STOP_CONNECTING, stop retrying
                if retry(self._connect_loop, retry) is STOP_CONNECTING:
                    break
        except RetryFailedError:
            self.logger.warning(
                "Failed connecting to Zookeeper "
                "within the connection retry policy."
            )
        finally:
            self.connection_stopped.set()
            if not isinstance(
                self.client.state_machine, (Inactive, AuthFailed)
            ):
                self._emit_event(SessionClosedEvent())
            # StopConnectionLoop registered the wakeup socket and wrote
            # a byte to it; nothing selects after this routine returns.
            self._release_wakeup()
            self.logger.log(BLATHER, "Connection stopped")

    def _expand_client_hosts(self) -> list[tuple[str, str, int]]:
        # Expand the entire list in advance so we can randomize it if needed
        host_ports: list[tuple[str, str, int]] = []
        for host, port in self.client.hosts:
            try:
                host = host.strip()
                for rhost in socket.getaddrinfo(
                    host, port, 0, 0, socket.IPPROTO_TCP
                ):
                    host_ports.append(
                        (
                            host,
                            cast("str", rhost[4][0]),
                            cast("int", rhost[4][1]),
                        )
                    )
            except socket.gaierror as e:
                # Skip hosts that don't resolve
                self.logger.warning("Cannot resolve %s: %s", host, e)
                pass
        if self.client.randomize_hosts:
            random.shuffle(host_ports)
        return host_ports

    def _connect_loop(self, retry: KazooRetry) -> object:
        # Iterate through the hosts a full cycle before starting over
        status = None
        host_ports = self._host_ports = self._expand_client_hosts()

        # Check for an empty hostlist, indicating none resolved
        if len(host_ports) == 0:
            raise ForceRetryError("No host resolved. Reconnecting")

        for host, hostip, port in host_ports:
            if self._is_terminal:
                status = STOP_CONNECTING
                break
            status = self._connect_attempt(host, hostip, port, retry)
            if status is STOP_CONNECTING:
                break

        if status is STOP_CONNECTING:
            return STOP_CONNECTING
        else:
            raise ForceRetryError("Reconnecting")

    def _compute_poll_timeout(self) -> tuple[float, float | None]:
        """Compute the selector timeout and optional heartbeat deadline."""
        if not self._is_session_active:
            if self._phase_deadline is not None:
                rem = max(0.0, self._phase_deadline - time.monotonic())
                return rem, None
            return self._connect_timeout, None

        jitter_time = random.randint(1, 40) / 100.0
        deadline = self._last_send + self._read_timeout / 2.0 - jitter_time
        timeout = max(deadline - time.monotonic(), jitter_time)
        return timeout, deadline

    def _dispatch_ready_events(
        self, ready: set[str], deadline: float | None
    ) -> bool:
        """Translate ready selector handles into state machine events.

        Returns True if processing should immediately skip to the next
        iteration.
        """
        if not ready:
            self._emit_event(POLL_TIMEOUT_EXPIRED)
        else:
            if "zk_socket" in ready:
                if self._is_session_active:
                    self._read_socket()
                    # Spelled out for mypy; see _check_is_terminal.
                    if not self.client._state_machine.state.is_connected:
                        return False
                else:
                    self._emit_event(SOCKET_READABLE)
                    if isinstance(
                        self.client._state_machine.state,
                        (Inactive, AuthFailed),
                    ):
                        return False

            # Check if any requests need sending before proceeding
            # to process more responses. Otherwise the responses
            # may choke out the requests. See PR#633.
            if "wakeup" in ready:
                self._drain_wakeup()
                if self._is_session_active:
                    self._send_request(
                        self._read_timeout, self._connect_timeout
                    )
                    return True
                else:
                    self._emit_event(WAKEUP_SIGNALED)
                    return True

        if (
            self._is_session_active
            and deadline is not None
            and time.monotonic() >= deadline
        ):
            self._emit_event(HEARTBEAT_DEADLINE_REACHED)
        elif (
            not self._is_session_active
            and self._phase_deadline is not None
            and time.monotonic() >= self._phase_deadline
        ):
            self._emit_event(POLL_TIMEOUT_EXPIRED)

        return False

    def _poll_step(self) -> None:
        """Execute a single cycle of the selector I/O loop."""
        timeout, deadline = self._compute_poll_timeout()

        if (
            self._is_session_active
            and self._requests.queue
            and (
                self._is_terminal
                or self._requests.has_send_capacity(
                    self.client.concurrent_request_limit
                )
            )
        ):
            self._drain_wakeup()
            self._send_request(self._read_timeout, self._connect_timeout)

        if self._frame_decoder.frame_ready:
            self._replies_since_select += 1
            if self._replies_since_select >= self._REPLIES_PER_YIELD:
                self._replies_since_select = 0
                self._yield_control()
            ready: set[str] = {"zk_socket"}
        else:
            self._replies_since_select = 0
            ready = self._select(timeout)
        self._dispatch_ready_events(ready, deadline)

    def _run_io_loop(self) -> None:
        """Drive selector loop while in an active-socket state."""
        while (
            isinstance(
                self.client._state_machine.state,
                (Handshaking, Authenticating, Connected, ConnectedReadOnly),
            )
            and self._socket is not None
        ):
            self._poll_step()

    def _connect_attempt(
        self,
        host: str,
        hostip: str,
        port: int,
        retry: KazooRetry,
    ) -> object:
        client = self.client
        KazooTimeoutError = self.handler.timeout_exception

        if self._is_terminal:
            return STOP_CONNECTING

        self._socket = None
        self._frame_decoder.reset()
        self._current_retry = retry

        # Were we given a r/w server? If so, use that instead
        if self._rw_server:
            host, hostip, port = self._rw_server
            self._rw_server = None
            self.logger.log(
                BLATHER,
                "Found r/w server to use, %s(%s):%s",
                host,
                hostip,
                port,
            )

        self._emit_event(ConnectAttemptStarted(host=host, port=port))

        try:
            self._requests.reset_xid(0)
            self.logger.info(
                "Connecting to %s(%s):%s, use_ssl: %r",
                host,
                hostip,
                port,
                client.use_ssl,
            )
            passwd_hex = (
                hexlify(client._session_passwd)
                if client._session_passwd
                else b""
            )
            self.logger.log(
                BLATHER,
                "    Using session_id: %r session_passwd: %s",
                client._session_id,
                passwd_hex,
            )

            session_timeout = client._session_timeout
            num_hosts = max(len(client.hosts), 1)

            self._socket = self._open_socket(
                host,
                hostip,
                port,
                retry.cur_delay,
            )
            self._socket.setblocking(False)
            self._socket_is_ssl = hasattr(self._socket, "pending")
            if self._check_is_terminal():
                return STOP_CONNECTING
            self._setup_selector()

            has_sasl = self.sasl_config is not None
            auth_tuple = tuple(client.auth_data)

            self._emit_event(
                SocketConnected(
                    host=host,
                    port=port,
                    num_hosts=num_hosts,
                    session_id=client._session_id or 0,
                    session_passwd=client._session_passwd,
                    session_timeout=session_timeout,
                    last_zxid=client.last_zxid,
                    read_only=bool(client.read_only),
                    has_sasl=has_sasl,
                    auth_data=auth_tuple,
                )
            )

            self._run_io_loop()

            if self._check_is_terminal():
                self.logger.info(
                    "Closing connection to %s(%s):%s", host, hostip, port
                )
                return STOP_CONNECTING
        except (
            OSError,
            ConnectionDropped,
            KazooTimeoutError,
            AuthFailedError,
            SASLException,
            SessionClosedRequireSaslError,
            SessionExpiredError,
            RWServerAvailable,
        ) as exc:
            rw_server_addr = (
                (self._rw_server[0], self._rw_server[2])
                if self._rw_server
                else None
            )
            mapped = _exception_to_event(
                exc,
                KazooTimeoutError,
                client._session_id,
                rw_server_addr,
                self.logger,
            )
            assert mapped is not None
            event, reset_retry, stop_connecting = mapped
            if reset_retry:
                retry.reset()
            self._emit_event(event)
            if stop_connecting or self._is_terminal:
                return STOP_CONNECTING
        except Exception:
            self.logger.exception("Unhandled exception in connection loop")
            raise
        finally:
            self._current_retry = None
            self._dispose_sasl()
            self._close_socket()
            # The retry back-off sleep and the next connect do not
            # select; _setup_selector registers the wakeup socket again.
            self._release_wakeup()
        return None

"""Zookeeper Protocol Connection Handler"""

from __future__ import annotations

from binascii import hexlify
from collections import defaultdict, deque
from collections.abc import Callable, Iterator
import logging
import random
import selectors
import socket
import ssl
import time
from typing import (
    Any,
    Literal,
    TypeVar,
    TYPE_CHECKING,
    cast,
)
from typing_extensions import Buffer

from kazoo.exceptions import (
    AuthFailedError,
    ConnectionDropped,
    ConnectionLoss,
    EXCEPTIONS,
    KazooException,
    SessionExpiredError,
    SessionClosedRequireSaslError,
    NoNodeError,
    SASLException,
)
from kazoo.loggingsupport import BLATHER
from kazoo.protocol.sasl import (
    SASLConfig,
    SASLExchange,
)
from kazoo.protocol.serialization import (
    AddWatch,
    Auth,
    AUTH_XID,
    AuthResponse,
    Close,
    Connect,
    Exists,
    FrameDecoder,
    GetChildren,
    GetChildren2,
    Ping,
    PING_XID,
    PingInstance,
    PingResponse,
    RemoveWatches,
    ReplyHeader,
    ReplyPacket,
    SASL,
    SASLResponse,
    ServerPacket,
    Transaction,
    Watch,
    WATCH_XID,
    WatchPacket,
)
from kazoo.protocol.states import (
    AddWatchMode,
    Callback,
    KeeperState,
    WatchedEvent,
    EVENT_TYPE_MAP,
    WatcherType,
)
from kazoo.protocol.statemachine import (
    AuthFailed,
    AuthFailedEvent,
    AuthPacketCompleted,
    ClientEvent,
    ConfigureReadOnlyMode,
    ConfigureTimeouts,
    ConnectAttemptStarted,
    ConnectionLostEvent,
    ConnectResponseReceived,
    DrainWakeupPipe,
    HEARTBEAT_DEADLINE_REACHED,
    Inactive,
    PING_RECEIVED,
    POLL_TIMEOUT_EXPIRED,
    ReadConnectResponsePacket,
    ReadSocketPacket,
    ResetSessionData,
    RWServerFoundEvent,
    SASLChallengeReceived,
    SASLCompleted,
    SendAuthPacket,
    SendConnectPacket,
    SendHeartbeatPing,
    SendQueuedRequest,
    SessionClosedEvent,
    SessionEstablished,
    SessionExpiredEvent,
    SideEffect,
    SOCKET_READABLE,
    SocketConnected,
    StartRequested,
    StartSASLHandshake,
    StepSASLHandshake,
    StopConnectionLoop,
    TriggerConnectionDrop,
    UPDATE_SEND_CAPACITY_FALSE,
    UPDATE_SEND_CAPACITY_TRUE,
    UpdateIOInterest,
    UpdateSessionData,
    WAKEUP_SIGNALED,
)
from kazoo.retry import (
    ForceRetryError,
    KazooRetry,
    RetryFailedError,
)

if TYPE_CHECKING:
    from kazoo.client import KazooClient, WatchFunc
    from kazoo.interfaces import IAsyncResult, Socket, Threadlike


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


# removed from Python3+
def buffer(obj: Buffer, offset: int = 0) -> memoryview:
    return memoryview(obj)[offset:]


class RequestManager:
    """Encapsulates client request queuing, in-flight pending requests, and
    XID sequence generation.
    """

    def __init__(self, client_fallback: Any = None) -> None:
        self._queue: deque[tuple[Any, IAsyncResult]] = deque()
        self._pending: deque[tuple[Any, IAsyncResult, int]] = deque()
        self.xid: int | None = None
        self._client_fallback = client_fallback

    @property
    def queue(self) -> deque[tuple[Any, IAsyncResult]]:
        if self._client_fallback is not None:
            q = getattr(self._client_fallback, "_queue", None)
            if isinstance(q, deque):
                return q
        return self._queue

    @queue.setter
    def queue(self, value: deque[tuple[Any, IAsyncResult]]) -> None:
        self._queue = value

    @property
    def pending(self) -> deque[tuple[Any, IAsyncResult, int]]:
        if self._client_fallback is not None:
            p = getattr(self._client_fallback, "_pending", None)
            if isinstance(p, (deque, list)):
                return cast("deque[tuple[Any, IAsyncResult, int]]", p)
        return self._pending

    @pending.setter
    def pending(self, value: deque[tuple[Any, IAsyncResult, int]]) -> None:
        self._pending = value

    def reset_xid(self, value: int = 0) -> None:
        """Reset the XID counter for a new connection attempt."""
        self.xid = value

    def next_xid(self) -> int:
        """Advance and return the next positive 31-bit XID."""
        self.xid = ((self.xid or 0) % 2147483647) + 1
        return self.xid

    def enqueue(self, request: Any, async_object: IAsyncResult) -> None:
        """Append a request and its completion handle to the send queue."""
        self.queue.append((request, async_object))

    def remove_queued(self, request: Any, async_object: IAsyncResult) -> None:
        """Remove a specific request pair from the send queue if present."""
        try:
            self.queue.remove((request, async_object))
        except ValueError:
            pass

    def peek_queued(self) -> tuple[Any, IAsyncResult]:
        """Return the head of the send queue without removing it."""
        return self.queue[0]

    def dequeue_to_pending(self, xid: int) -> tuple[Any, IAsyncResult]:
        """Pop the oldest queued request and record it as pending with xid."""
        request, async_object = self.queue.popleft()
        self.pending.append((request, async_object, xid))
        return request, async_object

    def pop_pending(self) -> tuple[Any, IAsyncResult, int]:
        """Pop and return the oldest in-flight pending request."""
        return self.pending.popleft()

    def pop_auth_pending(
        self, auth_xid: int = AUTH_XID
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
        self._queue = deque()
        self._pending = deque()


class WatcherManager:
    """Encapsulates client data, child, persistent, and recursive watchers."""

    def __init__(self, client_fallback: Any = None) -> None:
        self._child_watchers: defaultdict[str, set[WatchFunc]] = defaultdict(
            set
        )
        self._data_watchers: defaultdict[str, set[WatchFunc]] = defaultdict(
            set
        )
        self._persistent_watchers: defaultdict[str, set[WatchFunc]] = (
            defaultdict(set)
        )
        self._persistent_recursive_watchers: defaultdict[
            str, set[WatchFunc]
        ] = defaultdict(set)
        self._client_fallback = client_fallback

    @property
    def child_watchers(self) -> defaultdict[str, set[WatchFunc]]:
        if self._client_fallback is not None:
            d = getattr(self._client_fallback, "_child_watchers", None)
            if isinstance(d, dict):
                return cast("defaultdict[str, set[WatchFunc]]", d)
        return self._child_watchers

    @child_watchers.setter
    def child_watchers(
        self, value: defaultdict[str, set[WatchFunc]]
    ) -> None:
        self._child_watchers = value

    @property
    def data_watchers(self) -> defaultdict[str, set[WatchFunc]]:
        if self._client_fallback is not None:
            d = getattr(self._client_fallback, "_data_watchers", None)
            if isinstance(d, dict):
                return cast("defaultdict[str, set[WatchFunc]]", d)
        return self._data_watchers

    @data_watchers.setter
    def data_watchers(self, value: defaultdict[str, set[WatchFunc]]) -> None:
        self._data_watchers = value

    @property
    def persistent_watchers(self) -> defaultdict[str, set[WatchFunc]]:
        if self._client_fallback is not None:
            d = getattr(self._client_fallback, "_persistent_watchers", None)
            if isinstance(d, dict):
                return cast("defaultdict[str, set[WatchFunc]]", d)
        return self._persistent_watchers

    @persistent_watchers.setter
    def persistent_watchers(
        self, value: defaultdict[str, set[WatchFunc]]
    ) -> None:
        self._persistent_watchers = value

    @property
    def persistent_recursive_watchers(
        self,
    ) -> defaultdict[str, set[WatchFunc]]:
        if self._client_fallback is not None:
            d = getattr(
                self._client_fallback, "_persistent_recursive_watchers", None
            )
            if isinstance(d, dict):
                return cast("defaultdict[str, set[WatchFunc]]", d)
        return self._persistent_recursive_watchers

    @persistent_recursive_watchers.setter
    def persistent_recursive_watchers(
        self, value: defaultdict[str, set[WatchFunc]]
    ) -> None:
        self._persistent_recursive_watchers = value

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

        self._child_watchers = defaultdict(set)
        self._data_watchers = defaultdict(set)
        self._persistent_watchers = defaultdict(set)
        self._persistent_recursive_watchers = defaultdict(set)
        return watchers


class RWPinger:
    """A Read/Write Server Pinger Iterable

    This object is initialized with the hosts iterator object and the
    socket creation function. Anytime `next` is called on its iterator
    it yields either False, or a host, port tuple if it found a r/w
    capable Zookeeper node.

    After the first run-through of hosts, an exponential back-off delay
    is added before the next run. This delay is tracked internally and
    the iterator will yield False if called too soon.

    """

    def __init__(
        self,
        hosts: list[tuple[str, int]],
        connection_func: Callable[[tuple[str, int]], Socket],
    ):
        self.hosts = hosts
        self.connection = connection_func
        self.last_attempt: float | None = None

    def __iter__(self) -> Iterator[tuple[str, int] | Literal[False] | None]:
        if not self.last_attempt:
            self.last_attempt = time.monotonic()
        delay = 0.5
        while True:
            yield self._next_server(delay)

    def _next_server(
        self, delay: float
    ) -> tuple[str, int] | Literal[False] | None:
        jitter = random.randint(0, 100) / 100.0
        while (
            time.monotonic() < self.last_attempt + delay + jitter  # type: ignore[operator]
        ):
            # Skip rw ping checks if its too soon
            return False
        for host, port in self.hosts:
            log.debug("Pinging server for r/w: %s:%s", host, port)
            self.last_attempt = time.monotonic()
            try:
                sock = self.connection((host, port))
                sock.sendall(b"isro")
                result = sock.recv(8192)
                sock.close()
                if result == b"rw":
                    return (host, port)
                else:
                    return False
            except (OSError, ConnectionDropped):
                return False

            # NOTE: This does actually look like it's unreachable but I don't
            # want to alter the code any more than necessary for the first
            # pass.
            # The loop is basically a sleep with jitter that can be
            # Add some jitter between host pings
            while (  # type: ignore[unreachable]
                time.monotonic() < self.last_attempt + jitter
            ):
                return False
        delay *= 2  # And while not unreachable, this is pointless
        return None


class RWServerAvailable(Exception):
    """Thrown if a RW Server becomes available"""


ReturnValue = TypeVar("ReturnValue")


class ConnectionHandler:
    """Zookeeper connection handler"""

    def __init__(
        self,
        client: KazooClient,
        retry_sleeper: KazooRetry,
        logger: logging.Logger | None = None,
        sasl_config: SASLConfig | None = None,
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
        self.ping_outstanding = client.handler.event_object()

        self._read_sock: Socket | None = None
        self._write_sock: Socket | None = None

        self._socket: Socket | None = None
        self._selector: selectors.BaseSelector | None = None
        self._frame_decoder = FrameDecoder()

        req_mgr = getattr(client, "_request_manager", None)
        if isinstance(req_mgr, RequestManager):
            self._requests = req_mgr
        else:
            self._requests = RequestManager(client_fallback=client)

        watch_mgr = getattr(client, "_watcher_manager", None)
        if isinstance(watch_mgr, WatcherManager):
            self._watchers = watch_mgr
        else:
            self._watchers = WatcherManager(client_fallback=client)

        self._rw_server: tuple[str, int] | None = None
        self._ro_mode: (
            Iterator[Literal[False] | tuple[str, int] | None]
            | Literal[False]
            | None
        ) = False

        self._connection_routine: Threadlike | None = None
        self._last_send: float = 0.0
        self._read_timeout: float = 10.0
        self._connect_timeout: float = 10.0

        self.sasl_config: SASLConfig | None = sasl_config
        self._sasl_exchange: SASLExchange | None = None

        self._current_retry: KazooRetry | None = None

    @property
    def _xid(self) -> int | None:
        return self._requests.xid

    @_xid.setter
    def _xid(self, value: int | None) -> None:
        self._requests.xid = value

    @property
    def _is_session_active(self) -> bool:
        state_machine = getattr(self.client, "_state_machine", None)
        if state_machine is not None and hasattr(state_machine, "state"):
            return getattr(state_machine.state, "is_connected", False) is True
        return False

    @property
    def _is_terminal(self) -> bool:
        return self._check_is_terminal()

    def _check_is_terminal(self) -> bool:
        state_machine = getattr(self.client, "_state_machine", None)
        if state_machine is not None and hasattr(state_machine, "state"):
            state = state_machine.state
            if getattr(state, "is_terminal", False) is True:
                return True
            if getattr(state, "is_connecting_allowed", True) is False:
                return True
        return False

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
        self.client.send_event(event)

    def _ensure_selector(self) -> selectors.BaseSelector:
        if self._selector is None:
            self._selector = self.handler.create_selector()
        return self._selector

    def _setup_selector(self) -> None:
        """Ensure persistent selector exists and registers active sockets."""
        self._ensure_selector()
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

    def _update_io_interest(
        self, interest_mask: int, target: Literal["zk_socket", "wakeup"]
    ) -> None:
        """Modify selector registration for the target socket."""
        sock = self._read_sock if target == "wakeup" else self._socket
        if sock is None:
            return
        sel = self._ensure_selector()
        try:
            if interest_mask:
                try:
                    sel.modify(sock, interest_mask, data=target)
                except KeyError:
                    sel.register(sock, interest_mask, data=target)
            else:
                try:
                    sel.unregister(sock)
                except (KeyError, ValueError):
                    pass
        except ValueError:
            try:
                sel.unregister(sock)
            except Exception:
                pass
            if interest_mask:
                try:
                    sel.register(sock, interest_mask, data=target)
                except Exception:
                    pass
        except Exception:
            pass

    def _select(self, timeout: float) -> set[str]:
        """Wait for readable events on the persistent selector."""
        has_ssl_pending = (
            self._socket is not None
            and hasattr(self._socket, "pending")
            and self._socket.pending() > 0  # type: ignore[union-attr]
        )
        assert self._selector is not None
        events = self._selector.select(0.0 if has_ssl_pending else timeout)
        ready: set[str] = {
            key.data for key, mask in events if mask & selectors.EVENT_READ
        }
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
        xid = self._requests.next_xid()
        self._submit(SASL(token), self._connect_timeout, xid)
        self._last_send = time.monotonic()

    def execute_effect(self, effect: SideEffect) -> None:
        """Execute I/O and selector side effects from the state machine."""
        match effect:
            case ReadSocketPacket():
                self._read_socket(self._read_timeout)

            case SendQueuedRequest():
                self._send_request(self._read_timeout, self._connect_timeout)
                # Requests act as implicit pings.
                self._last_send = time.monotonic()

            case UpdateIOInterest(interest_mask=mask, target=target):
                self._update_io_interest(mask, target)

            case DrainWakeupPipe():
                self._update_io_interest(0, "wakeup")
                if not self._requests.queue and self._read_sock is not None:
                    try:
                        self._read_sock.recv(1024)
                    except OSError:
                        pass

            case SendHeartbeatPing():
                self._send_ping(self._connect_timeout)
                self._last_send = time.monotonic()

            case StartSASLHandshake(host=host):
                self._start_sasl(host or "")

            case StepSASLHandshake(challenge=challenge):
                self._step_sasl(challenge)

            case SendAuthPacket(scheme=scheme, credential=credential):
                self._submit(
                    Auth(0, scheme, credential),
                    self._connect_timeout,
                    AUTH_XID,
                )
                self._last_send = time.monotonic()

            case TriggerConnectionDrop(reason=reason):
                self.ping_outstanding.clear()
                raise ConnectionDropped(reason)

            case SessionEstablished():
                if self._current_retry is not None:
                    self._current_retry.reset()

            case ResetSessionData():
                self._frame_decoder.reset()
                if self._current_retry is not None:
                    self._current_retry.reset()

            case StopConnectionLoop():
                self._update_io_interest(selectors.EVENT_READ, "wakeup")
                self.wake()

            case SendConnectPacket(connect=connect, timeout=timeout):
                self._submit(connect, timeout)
                self._last_send = time.monotonic()

            case ReadConnectResponsePacket():
                self._read_socket(self._connect_timeout, is_connect=True)

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
            self._update_io_interest(selectors.EVENT_READ, "wakeup")
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
        self.connection_stopped.wait(timeout)
        if self._connection_routine:
            self._connection_routine.join()
            self._connection_routine = None
        return self.connection_stopped.is_set()

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
        except OSError:
            return False

    def _server_pinger(self) -> RWPinger:
        """Returns a server pinger iterable, that will ping the next
        server in the list, and apply a back-off between attempts."""
        return RWPinger(
            self.client.hosts,
            self.handler.create_connection,
        )

    def _read_packet(
        self,
        timeout: float | None,
        *,
        is_connect: bool = False,
        is_sasl: bool = False,
    ) -> ServerPacket:
        """Read and decode the next typed server packet via FrameDecoder."""
        packet = self._frame_decoder.pop_packet(
            is_connect=is_connect, is_sasl=is_sasl
        )
        while packet is None:
            needed = self._frame_decoder.bytes_needed()
            chunk = self._read(needed, timeout)
            self._frame_decoder.feed(chunk)
            packet = self._frame_decoder.pop_packet(
                is_connect=is_connect, is_sasl=is_sasl
            )
        return packet

    def _read(self, length: int, timeout: float | None) -> bytes:
        msgparts = []
        remaining = length
        use_fast_read = "select" not in getattr(self.handler, "__dict__", {})
        # We know that self._socket is not None here because we only call
        # this method when we have set up the connection and the read socket.
        # But mypy doesn't understand that.
        while remaining > 0:
            if use_fast_read:
                try:
                    chunk = self._socket.recv(  # type: ignore[union-attr]
                        remaining
                    )
                    if chunk == b"":
                        raise ConnectionDropped("socket connection broken")
                    msgparts.append(chunk)
                    remaining -= len(chunk)
                    continue
                except BlockingIOError:
                    pass
                except ssl.SSLError as e:
                    if e.errno not in (
                        ssl.SSL_ERROR_WANT_READ,
                        ssl.SSL_ERROR_WANT_WRITE,
                    ):
                        raise
            # Because of SSL framing, a select may not return when using
            # an SSL socket because the underlying physical socket may not
            # have anything to select, but the wrapped object may still
            # have something to read as it has previously gotten enough
            # data from the underlying socket.
            if (
                hasattr(self._socket, "pending") and self._socket.pending() > 0  # type: ignore[union-attr]
            ):
                pass
            else:
                s = self.handler.select(
                    [cast("Socket", self._socket)], [], [], timeout
                )[0]
                if not s:  # pragma: nocover
                    # If the read list is empty, we got a timeout. We don't
                    # have to check wlist and xlist as we don't set any
                    raise self.handler.timeout_exception(
                        "socket time-out during read"
                    )
            try:
                chunk = self._socket.recv(  # type: ignore[union-attr]
                    remaining
                )
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
            if chunk == b"":
                raise ConnectionDropped("socket connection broken")
            msgparts.append(chunk)
            remaining -= len(chunk)
        return b"".join(msgparts)

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

    def _write(self, msg: bytes, timeout: float | None) -> None:
        """Write a raw msg to the socket"""
        sent = 0
        msg_length = len(msg)
        # Note: The casts/type: ignore are because mypy can't work out
        # self._socket is not None, and I don't want to change any code.
        use_fast_write = "select" not in getattr(self.handler, "__dict__", {})
        while sent < msg_length:
            msg_slice = buffer(msg, sent)
            if use_fast_write:
                try:
                    bytes_sent = self._socket.send(  # type: ignore[union-attr]
                        msg_slice
                    )
                    if not bytes_sent:
                        raise ConnectionDropped("socket connection broken")
                    sent += bytes_sent
                    continue
                except BlockingIOError:
                    pass
                except ssl.SSLError as e:
                    if e.errno not in (
                        ssl.SSL_ERROR_WANT_READ,
                        ssl.SSL_ERROR_WANT_WRITE,
                    ):
                        raise
            s = self.handler.select(
                [], [cast("Socket", self._socket)], [], timeout
            )[1]
            if not s:  # pragma: nocover
                # If the write list is empty, we got a timeout. We don't
                # have to check rlist and xlist as we don't set any
                raise self.handler.timeout_exception(
                    "socket time-out during write"
                )
            try:
                bytes_sent = self._socket.send(  # type: ignore[union-attr]
                    msg_slice
                )
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
            if not bytes_sent:
                raise ConnectionDropped("socket connection broken")
            sent += bytes_sent

    def _find_persistent_recursive_watchers(
        self, path: str
    ) -> list[WatchFunc]:
        return self._watchers.find_persistent_recursive(path)

    def _read_watch_event(
        self,
        watch: Watch,
        zxid: int = WatchedEvent.NO_ZXID,
    ) -> None:
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
            EVENT_TYPE_MAP[watch.type], client.client_state, path, zxid
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

    def _read_response(
        self,
        header: ReplyHeader,
        buffer: bytes,
        offset: int,
    ) -> object | None:
        client = self.client
        request, async_object, xid = self._requests.pop_pending()
        if header.zxid and header.zxid > 0:
            client.last_zxid = header.zxid
        if header.xid != xid:
            exc = RuntimeError(
                "xids do not match, expected %r received %r",
                xid,
                header.xid,
            )
            async_object.set_exception(exc)
            raise exc

        # Determine if its an exists request and a no node error
        exists_error = (
            # NoNodeError does actually have a code. It's added by a decorator,
            # which could possibly be better done via inheritance but this is
            # less invasive to the existing code.
            header.err == NoNodeError.code  # type: ignore[attr-defined]
            and request.type == Exists.type
        )

        # Set the exception if its not an exists error
        if header.err and not exists_error:
            callback_exception = EXCEPTIONS[header.err]()
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
                try:
                    response = request.deserialize(buffer, offset)
                except Exception as exc:
                    self.logger.exception(
                        "Exception raised during deserialization "
                        "of request: %s",
                        request,
                    )
                    async_object.set_exception(exc)
                    return None
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
        match packet:
            case Connect():
                self._emit_event(
                    ConnectResponseReceived(connect_result=packet)
                )
            case PingResponse():
                self.logger.log(BLATHER, "Received Ping")
                self.ping_outstanding.clear()
                self._emit_event(PING_RECEIVED)
            case AuthResponse(zxid=zxid, err=err_code):
                self.logger.log(BLATHER, "Received AUTH")
                if zxid and zxid > 0:
                    client.last_zxid = zxid
                async_object = self._requests.pop_auth_pending(AUTH_XID)
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
            case WatchPacket(zxid=zxid, watch=watch):
                self._read_watch_event(watch, zxid)
            case SASLResponse(
                xid=xid, zxid=zxid, err=err_code, challenge=challenge
            ):
                if xid != self._xid:
                    raise RuntimeError(
                        "xids do not match, expected %r received %r",
                        self._xid,
                        xid,
                    )
                if zxid > 0:
                    client.last_zxid = zxid
                if err_code:
                    callback_exception = EXCEPTIONS[err_code]()
                    self.logger.debug(
                        "Received error(xid=%s) %r",
                        self._xid,
                        callback_exception,
                    )
                    raise callback_exception
                self._emit_event(SASLChallengeReceived(challenge=challenge))
            case ReplyPacket(header=header, buffer=buffer, offset=offset):
                self.logger.log(BLATHER, "Reading for header %r", header)
                return self._read_response(header, buffer, offset)
        return None

    def _read_socket(
        self,
        read_timeout: float | None = None,
        *,
        is_connect: bool = False,
    ) -> object | None:
        """Called when there's something to read on the socket"""
        eff_timeout = (
            (self._connect_timeout if is_connect else self._read_timeout)
            if read_timeout is None
            else read_timeout
        )
        packet = self._read_packet(
            eff_timeout,
            is_connect=is_connect,
            is_sasl=self._sasl_exchange is not None,
        )
        return self._process_packet(packet)

    def _send_request(
        self,
        read_timeout: float,
        connect_timeout: float,
    ) -> None:
        """Called when we have something to send out on the socket"""
        try:
            request, _ = self._requests.peek_queued()
        except IndexError:
            # Not actually something on the queue, this can occur if
            # something happens to cancel the request such that we
            # don't clear the socket below after sending
            try:
                # Clear possible inconsistence (no request in the queue
                # but have data in the read socket), which causes cpu to spin.
                #
                # We know _read_sock is not None because we only call this
                # method when we have set up the connection and the read
                # socket, but mypy doesn't understand that.
                self._read_sock.recv(1)  # type: ignore[union-attr]
            except OSError:
                pass
            return

        # Special case for testing, if this is a _SessionExpire object
        # then throw a SessionExpiration error as if we were dropped
        if request is _SESSION_EXPIRED:
            raise SessionExpiredError("Session expired: Testing")
        if request is _CONNECTION_DROP:
            raise ConnectionDropped("Connection dropped: Testing")

        # Special case for auth packets
        if request.type == Auth.type:
            xid = AUTH_XID
        else:
            xid = self._requests.next_xid()

        self._requests.dequeue_to_pending(xid)
        # _read_sock should never be None here as we only call this method
        # when we have set up the connection and the read socket.
        try:
            self._read_sock.recv(1)  # type: ignore[union-attr]
        except OSError:
            pass
        self._submit(request, connect_timeout, xid)

    def _send_ping(self, connect_timeout: float) -> None:
        self.ping_outstanding.set()
        self._submit(PingInstance, connect_timeout, PING_XID)

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
        except (RetryFailedError, InterruptedError):
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
                    # FIXME These casts seem to be unnecessary on later
                    # versions of mypy/python
                    host_ports.append(
                        (
                            host,
                            cast(  # type: ignore[redundant-cast]
                                "str", rhost[4][0]
                            ),
                            cast(  # type: ignore[redundant-cast]
                                "int", rhost[4][1]
                            ),
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
        host_ports = self._expand_client_hosts()

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
            return self._connect_timeout, None

        jitter_time = random.randint(1, 40) / 100.0
        deadline = self._last_send + self._read_timeout / 2.0 - jitter_time
        timeout = max(deadline - time.monotonic(), jitter_time)
        return timeout, deadline

    def _update_send_capacity(self) -> None:
        """Evaluate request queuing interest and notify state machine."""
        can_send = bool(self._is_terminal) or self._requests.has_send_capacity(
            self.client.concurrent_request_limit
        )
        self._emit_event(
            UPDATE_SEND_CAPACITY_TRUE
            if can_send
            else UPDATE_SEND_CAPACITY_FALSE
        )

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
                self._emit_event(SOCKET_READABLE)
                if isinstance(
                    self.client._state_machine.state, (Inactive, AuthFailed)
                ):
                    return False

            # Check if any requests need sending before proceeding
            # to process more responses. Otherwise the responses
            # may choke out the requests. See PR#633.
            if "wakeup" in ready and self._wakeup_registered:
                self._emit_event(WAKEUP_SIGNALED)
                return True

        if (
            self._is_session_active
            and deadline is not None
            and time.monotonic() >= deadline
        ):
            self._emit_event(HEARTBEAT_DEADLINE_REACHED)

        return False

    def _poll_step(self) -> None:
        """Execute a single cycle of the selector I/O loop."""
        timeout, deadline = self._compute_poll_timeout()

        if self._is_session_active:
            self._update_send_capacity()

        ready = self._select(timeout)
        self._dispatch_ready_events(ready, deadline)

    def _run_io_loop(self) -> None:
        """Drive the selector event loop until entering a terminal state."""
        while not isinstance(
            self.client._state_machine.state, (Inactive, AuthFailed)
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
            self.logger.log(
                BLATHER, "Found r/w server to use, %s:%s", host, port
            )
            host, port = self._rw_server
            self._rw_server = None

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

            # Seed default timeouts before session negotiation
            session_timeout = client._session_timeout
            num_hosts = max(len(client.hosts), 1)
            init_connect_timeout = (session_timeout / 1000.0) / num_hosts
            self._connect_timeout = init_connect_timeout
            self._read_timeout = (session_timeout / 1000.0) * 2.0 / 3.0

            self._socket = self.handler.create_connection(
                address=(hostip, port),
                hostname=host,
                timeout=retry.cur_delay,
                use_ssl=client.use_ssl,
                keyfile=client.keyfile,
                certfile=client.certfile,
                ca=client.ca,
                keyfile_password=client.keyfile_password,
                verify_certs=client.verify_certs,
                check_hostname=client.check_hostname,
            )
            self._socket.setblocking(False)
            if self._check_is_terminal():
                return STOP_CONNECTING
            self._setup_selector()

            self.ping_outstanding.clear()

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

            self.logger.info("Closing connection to %s:%s", host, port)
            if not isinstance(
                self.client.state_machine, (Inactive, AuthFailed)
            ):
                self._emit_event(SessionClosedEvent())
            return STOP_CONNECTING
        except (OSError, ConnectionDropped, KazooTimeoutError) as e:
            if isinstance(e, KazooTimeoutError):
                self.logger.warning("Connection time-out: %s", e)
            else:
                self.logger.warning("Connection dropped: %s", e)
            self._emit_event(ConnectionLostEvent(reason=str(e)))
            if self._check_is_terminal():
                return STOP_CONNECTING
        except (
            AuthFailedError,
            SASLException,
            SessionClosedRequireSaslError,
        ) as err:
            retry.reset()
            if isinstance(err, SessionClosedRequireSaslError):
                self.logger.warning(
                    "AUTH_FAILED closing (server requires SASL auth): %s", err
                )
            else:
                self.logger.warning("AUTH_FAILED closing: %s", err)
            self._emit_event(AuthFailedEvent(error=err))
            return STOP_CONNECTING
        except SessionExpiredError:
            retry.reset()
            self.logger.warning("Session has expired")
            self._emit_event(
                SessionExpiredEvent(session_id=client._session_id)
            )
        except RWServerAvailable:
            retry.reset()
            self.logger.warning("Found a RW server, dropping connection")
            self._emit_event(RWServerFoundEvent(server=self._rw_server))
        except Exception:
            self.logger.exception("Unhandled exception in connection loop")
            raise
        finally:
            self._current_retry = None
            self._dispose_sasl()
            self._update_io_interest(0, "wakeup")
            if self._socket is not None:
                # I think this is a bug in mypy, as the socket does get set up
                # in self._connect, but it doesn't seem to be able to track
                # that.
                self._update_io_interest(  # type: ignore[unreachable]
                    0, "zk_socket"
                )
                self._socket.close()
                self._socket = None
        return None

"""Transport-agnostic ZooKeeper session managers, packet router, and probes.

This module contains pure, socket-free protocol session components extracted
from :mod:`kazoo.protocol.connection`:

* :class:`RequestManager`: client request queue, positive-XID pending FIFO,
  dedicated ``auth_pending`` queue, and 31-bit XID generator.
* :class:`WatcherManager`: one-shot, persistent, and persistent-recursive
  watcher registries.
* :class:`RWProbeSchedule`: exponential-backoff endpoint scheduler for
  read-only -> read-write server discovery.
* :class:`PacketRouter`: decoded :data:`ServerPacket` validation and dispatch.
* :func:`_exception_to_event`: I/O exception to state-machine event mapping.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from kazoo.exceptions import (
    EXCEPTIONS,
    AuthFailedError,
    ConnectionDropped,
    ConnectionLoss,
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
from kazoo.protocol.serialization import (
    AddWatch,
    AuthResponse,
    Close,
    Connect,
    Exists,
    GetChildren,
    GetChildren2,
    PingResponse,
    RemoveWatches,
    ReservedXID,
    ResponsePacket,
    SASLResponse,
    Transaction,
    WatchPacket,
)
from kazoo.protocol.statemachine import (
    PING_RECEIVED,
    Authenticating,
    AuthFailedEvent,
    AuthPacketCompleted,
    AuthStage,
    ClientEvent,
    ClientState,
    ConnectionLostEvent,
    ConnectResponseReceived,
    RWServerFoundEvent,
    SASLChallengeReceived,
    SessionClosedEvent,
    SessionExpiredEvent,
)
from kazoo.protocol.states import (
    EVENT_TYPE_MAP,
    AddWatchMode,
    Callback,
    KeeperState,
    WatchedEvent,
    WatcherType,
)

if TYPE_CHECKING:
    from kazoo.client import WatchFunc
    from kazoo.interfaces import ICompletion
    from kazoo.protocol.serialization import ServerPacket


# Special testing hook objects used to force a session expired or drop error as
# if it came from the server.
_SESSION_EXPIRED = object()
_CONNECTION_DROP = object()

STOP_CONNECTING = object()

CREATED_EVENT = 1
DELETED_EVENT = 2
CHANGED_EVENT = 3
CHILD_EVENT = 4

CLOSE_RESPONSE = Close.type


class RWServerAvailable(Exception):
    """Thrown if a RW Server becomes available"""


class RequestManager:
    """Encapsulates client request queuing, in-flight pending requests, and
    XID sequence generation.
    """

    __slots__ = ("queue", "pending", "auth_pending", "xid")

    def __init__(self) -> None:
        self.queue: deque[tuple[Any, ICompletion[Any] | None]] = deque()
        self.pending: deque[tuple[Any, ICompletion[Any] | None, int]] = deque()
        self.auth_pending: deque[ICompletion[Any] | None] = deque()
        self.xid: int | None = None

    def reset_xid(self, value: int = 0) -> None:
        """Reset the XID counter for a new connection attempt."""
        self.xid = value

    def next_xid(self) -> int:
        """Advance and return the next positive 31-bit XID."""
        self.xid = ((self.xid or 0) % 2147483647) + 1
        return self.xid

    def enqueue(
        self, request: Any, async_object: ICompletion[Any] | None = None
    ) -> None:
        """Append a request and its completion handle to the send queue."""
        self.queue.append((request, async_object))

    def remove_queued(
        self, request: Any, async_object: ICompletion[Any] | None
    ) -> bool:
        """Remove a specific request pair from the send queue if present.

        Uses O(n) ``deque.remove``; for rare wake-failure cleanup only.
        Returns ``True`` if the pair was found and removed, ``False`` if it
        had already been popped or failed.
        """
        try:
            self.queue.remove((request, async_object))
            return True
        except ValueError:
            return False

    def peek_queued(self) -> tuple[Any, ICompletion[Any] | None]:
        """Return the head of the send queue without removing it."""
        return self.queue[0]

    def dequeue_to_pending(
        self, xid: int
    ) -> tuple[Any, ICompletion[Any] | None]:
        """Pop the oldest queued request and record it as pending with xid."""
        request, async_object = self.queue.popleft()
        if xid == ReservedXID.AUTH:
            self.auth_pending.append(async_object)
        else:
            self.pending.append((request, async_object, xid))
        return request, async_object

    def pop_pending(self) -> tuple[Any, ICompletion[Any] | None, int]:
        """Pop and return the oldest in-flight pending request."""
        return self.pending.popleft()

    def pop_auth_pending(
        self, auth_xid: int = ReservedXID.AUTH
    ) -> ICompletion[Any] | None:
        """Pop the oldest pending Auth request (or fallback scan pending)."""
        if self.auth_pending:
            return self.auth_pending.popleft()
        pending = self.pending
        for idx, (_, a_obj, xid) in enumerate(pending):
            if xid == auth_xid:
                del pending[idx]
                return a_obj
        return None

    def has_send_capacity(self, limit: int | None) -> bool:
        """Return True if the number of pending requests is below limit."""
        return (
            limit is None or len(self.pending) + len(self.auth_pending) < limit
        )

    def fail_in_flight(self, exc: Exception) -> None:
        """Fail and drain in-flight ``pending`` and ``auth_pending`` entries.

        Preserves ``self.queue`` so queued requests still drain after a
        reconnect. In-flight entries belong strictly to one transport socket
        and must be cleared before a new connection resets the XID sequence.
        """
        pending = self.pending
        while True:
            try:
                _, async_object, _ = pending.popleft()
                if async_object:
                    async_object.set_exception(exc)
            except IndexError:
                break

        auth_pending = self.auth_pending
        while True:
            try:
                async_object = auth_pending.popleft()
                if async_object:
                    async_object.set_exception(exc)
            except IndexError:
                break

    def fail_all(self, state: KeeperState) -> None:
        """Fail all pending and queued requests with the state's exception."""
        exc: KazooException
        if state == KeeperState.AUTH_FAILED:
            exc = AuthFailedError()
        elif state == KeeperState.EXPIRED_SESSION:
            exc = SessionExpiredError()
        else:
            exc = ConnectionLoss()

        self.fail_in_flight(exc)

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
        self.auth_pending = deque()


class WatcherManager:
    """Encapsulates client data, child, persistent, and recursive watchers."""

    __slots__ = (
        "child_watchers",
        "data_watchers",
        "persistent_watchers",
        "persistent_recursive_watchers",
    )

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
        pr_watchers = self.persistent_recursive_watchers
        if not pr_watchers:
            return []
        watchers: list[WatchFunc] = []
        root_watchers = pr_watchers.get("/")
        if root_watchers:
            watchers.extend(root_watchers)
        stripped = path.strip("/")
        if stripped:
            prefix = ""
            for part in stripped.split("/"):
                prefix = f"{prefix}/{part}"
                hit = pr_watchers.get(prefix)
                if hit:
                    watchers.extend(hit)
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


class RWProbeSchedule:
    """Pure host-cursor and exponential-backoff schedule for RW server probes.

    Separates timing and endpoint rotation from socket I/O so callers supply
    monotonic timestamps and jitter explicitly.
    """

    __slots__ = (
        "hosts",
        "delay",
        "last_attempt",
        "_idx",
        "_waiting_backoff",
        "_backoff_until",
    )

    def __init__(self, hosts: list[tuple[str, str, int]]) -> None:
        self.hosts = hosts
        self.delay: float = 0.5
        self.last_attempt: float | None = None
        self._idx: int = 0
        self._waiting_backoff: bool = False
        self._backoff_until: float | None = None

    def next_host(
        self, now: float, jitter_fn: Callable[[], float]
    ) -> tuple[str, str, int] | None:
        """Return the next ``(host, hostip, port)`` to probe, or ``None``.

        Returns ``None`` when ``hosts`` is empty or while inside the
        inter-pass exponential backoff window.
        """
        if not self.hosts:
            return None
        if self._waiting_backoff:
            if self._backoff_until is None:
                assert self.last_attempt is not None
                self._backoff_until = (
                    self.last_attempt + self.delay + jitter_fn()
                )
            if now < self._backoff_until:
                return None
            self.delay = min(self.delay * 2, 60.0)
            self._waiting_backoff = False
            self._backoff_until = None
            self._idx = 0

        endpoint = self.hosts[self._idx]
        self._idx += 1
        self.last_attempt = now
        return endpoint

    def mark_found(self) -> None:
        """Reset backoff delay after discovering a read/write endpoint."""
        self.delay = 0.5
        self._backoff_until = None

    def finish_probe(self, now: float) -> None:
        """Record completion of the current endpoint probe.

        If the last host in the list was probed, arms the inter-pass backoff
        window starting at ``now``.
        """
        if self._idx >= len(self.hosts):
            self.last_attempt = now
            self._waiting_backoff = True
            self._backoff_until = None


class PacketRouter:
    """Sans-I/O dispatcher for decoded :data:`ServerPacket` instances."""

    __slots__ = (
        "_requests",
        "_watchers",
        "_get_state",
        "_set_last_zxid",
        "_unchroot",
        "_emit_event",
        "_dispatch_watch",
        "_logger",
    )

    def __init__(
        self,
        *,
        requests: RequestManager,
        watchers: WatcherManager,
        get_state: Callable[[], ClientState],
        set_last_zxid: Callable[[int], None],
        unchroot: Callable[[str], str],
        emit_event: Callable[[ClientEvent], None],
        dispatch_watch: Callable[[Callback], None],
        logger: logging.Logger,
    ) -> None:
        self._requests = requests
        self._watchers = watchers
        self._get_state = get_state
        self._set_last_zxid = set_last_zxid
        self._unchroot = unchroot
        self._emit_event = emit_event
        self._dispatch_watch = dispatch_watch
        self._logger = logger

    def unchroot(self, path: str) -> str:
        """Satisfy :class:`~kazoo.interfaces.HasUnchroot` for transactions."""
        return self._unchroot(path)

    def _is_terminal(self) -> bool:
        state = self._get_state()
        return state.is_terminal or not state.is_connecting_allowed

    def read_watch_event(self, watch: WatchPacket) -> None:
        """Collect matching watchers and dispatch a :class:`WatchedEvent`."""
        path = watch.path

        if self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug("Received EVENT: %s", watch)

        watchers = self._watchers.collect_for_event(watch.type, path)
        if watchers is None:
            self._logger.warning("Received unknown event %r", watch.type)
            return
        if not watchers:
            return

        # Strip the chroot if needed
        path = self._unchroot(path)
        state = self._get_state()
        ev = WatchedEvent(
            EVENT_TYPE_MAP[watch.type], state.keeper_state, path, watch.zxid
        )

        # Last check to ignore watches if we've been stopped or terminated
        if state.is_terminal or not state.is_connecting_allowed:
            return

        for watch1 in watchers:
            self._dispatch_watch(Callback("watch", watch1, (ev,)))

    def read_response(self, packet: ResponsePacket) -> object | None:
        """Resolve a decoded request reply and update watchers."""
        header = packet.header
        request = packet.request
        async_object = packet.async_object
        xid = header.xid
        if header.zxid and header.zxid > 0:
            self._set_last_zxid(header.zxid)

        if packet.error is not None:
            if isinstance(packet.error, ReplyTooLargeError):
                self._logger.debug(
                    "Received error(xid=%s) %r", xid, packet.error
                )
            else:
                self._logger.error(
                    "Exception raised during deserialization of request: %s",
                    request,
                    exc_info=packet.error,
                )
            if async_object is not None:
                async_object.set_exception(packet.error)
            if isinstance(request, Close):
                self._logger.log(BLATHER, "Read close response")
                self._emit_event(SessionClosedEvent())
                return CLOSE_RESPONSE
            return None

        # Determine if it's an exists request and a no-node error
        exists_error = (
            header.err == NoNodeError.code and request.type == Exists.type
        )

        if header.err and not exists_error:
            callback_exception = _error_for_code(header.err)
            if self._logger.isEnabledFor(logging.DEBUG):
                self._logger.debug(
                    "Received error(xid=%s) %r", xid, callback_exception
                )
            if async_object:
                async_object.set_exception(callback_exception)
        elif request and async_object:
            if exists_error:
                response = None
            else:
                response = packet.response
                if self._logger.isEnabledFor(logging.DEBUG):
                    self._logger.debug(
                        "Received response(xid=%s): %r", xid, response
                    )

                if request.type == Transaction.type:
                    response = Transaction.unchroot(self, response)

            if not self._is_terminal():
                self._watchers.update_for_response(request)

            async_object.set(response)

        if isinstance(request, Close):
            self._logger.log(BLATHER, "Read close response")
            self._emit_event(SessionClosedEvent())
            return CLOSE_RESPONSE
        return None

    def process_packet(self, packet: ServerPacket) -> object | None:
        """Validate state guards and route a decoded :data:`ServerPacket`."""
        state = self._get_state()
        is_active = state.is_connected
        match packet:
            case Connect():
                self._emit_event(
                    ConnectResponseReceived(connect_result=packet)
                )
            case PingResponse():
                if not is_active:
                    raise ConnectionDropped(
                        f"Unexpected PingResponse in {type(state).__name__}"
                    )
                if self._logger.isEnabledFor(BLATHER):
                    self._logger.log(BLATHER, "Received Ping")
                self._emit_event(PING_RECEIVED)
            case AuthResponse(zxid=zxid, err=err_code):
                if not (
                    is_active
                    or (
                        isinstance(state, Authenticating)
                        and state.stage == AuthStage.INITIAL_AUTH
                    )
                ):
                    raise ConnectionDropped(
                        f"Unexpected AuthResponse in {type(state).__name__}"
                    )
                self._logger.log(BLATHER, "Received AUTH")
                if zxid and zxid > 0:
                    self._set_last_zxid(zxid)
                async_object = self._requests.pop_auth_pending(
                    ReservedXID.AUTH
                )
                if err_code:
                    err = (
                        EXCEPTIONS[err_code]()
                        if err_code in EXCEPTIONS
                        else AuthFailedError(f"Auth error {err_code}")
                    )
                    if async_object is not None:
                        async_object.set_exception(err)
                    if isinstance(err, SessionExpiredError):
                        raise err
                    if isinstance(
                        err,
                        (
                            ConnectionLoss,
                            OperationTimeoutError,
                            SessionMovedError,
                            ThrottledOpError,
                        ),
                    ):
                        raise ConnectionDropped(
                            f"Transient auth error: {err!r}"
                        ) from err
                    self._emit_event(AuthFailedEvent(error=err))
                else:
                    if async_object is not None:
                        async_object.set(True)
                    self._emit_event(AuthPacketCompleted())
            case WatchPacket():
                if not is_active:
                    raise ConnectionDropped(
                        f"Unexpected WatchPacket in {type(state).__name__}"
                    )
                self.read_watch_event(packet)
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
                    self._set_last_zxid(zxid)
                if err_code:
                    if err_code not in EXCEPTIONS:
                        raise AuthFailedError(f"SASL error {err_code}")
                    callback_exception = EXCEPTIONS[err_code]()
                    self._logger.debug(
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
                if not is_active:
                    raise ConnectionDropped(
                        f"Unexpected ResponsePacket in {type(state).__name__}"
                    )
                if self._logger.isEnabledFor(BLATHER):
                    self._logger.log(
                        BLATHER, "Reading for header %r", packet.header
                    )
                return self.read_response(packet)
        return None


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


__all__ = (
    "CHANGED_EVENT",
    "CHILD_EVENT",
    "CLOSE_RESPONSE",
    "CREATED_EVENT",
    "DELETED_EVENT",
    "PacketRouter",
    "RequestManager",
    "RWProbeSchedule",
    "RWServerAvailable",
    "STOP_CONNECTING",
    "WatcherManager",
)

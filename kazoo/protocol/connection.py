"""Zookeeper Protocol Connection Handler"""

from __future__ import annotations

import contextvars
import logging
import random
import selectors
import socket
import ssl
import time
from binascii import hexlify
from collections.abc import Callable, Iterator
from collections.abc import Set as AbstractSet
from contextlib import AbstractContextManager, contextmanager
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    cast,
)

from kazoo.exceptions import (
    AuthFailedError,
    ConnectionDropped,
    ConnectionLoss,
    InvalidStateTransitionError,
    SASLException,
    SessionClosedRequireSaslError,
    SessionExpiredError,
)
from kazoo.loggingsupport import BLATHER
from kazoo.protocol.sasl import (
    SASLConfig,
    SASLExchange,
)
from kazoo.protocol.serialization import (
    JUTE_MAX_BUFFER,
    SASL,
    Auth,
    Close,
    Connect,
    FrameDecoder,
    Ping,
    ReservedXID,
    ResponsePacket,
    WatchPacket,
)
from kazoo.protocol.session import (
    _CONNECTION_DROP,
    _SESSION_EXPIRED,
    CHANGED_EVENT,
    CHILD_EVENT,
    CLOSE_RESPONSE,
    CREATED_EVENT,
    DELETED_EVENT,
    STOP_CONNECTING,
    PacketRouter,
    RequestManager,
    RWProbeSchedule,
    RWServerAvailable,
    WatcherManager,
    _exception_to_event,
)
from kazoo.protocol.statemachine import (
    HEARTBEAT_DEADLINE_REACHED,
    POLL_TIMEOUT_EXPIRED,
    SOCKET_READABLE,
    WAKEUP_SIGNALED,
    Authenticating,
    AuthFailed,
    ClientEvent,
    CloseSocket,
    ConfigureReadOnlyMode,
    ConfigureTimeouts,
    ConnectAttemptStarted,
    Connected,
    ConnectedReadOnly,
    DrainWakeupPipe,
    EnqueueCloseRequest,
    Handshaking,
    Inactive,
    ReadConnectResponsePacket,
    ReadSocketPacket,
    ResetSessionData,
    SASLCompleted,
    SendAuthPacket,
    SendConnectPacket,
    SendHeartbeatPing,
    SessionClosedEvent,
    SessionEstablished,
    SideEffect,
    SocketConnected,
    StartRequested,
    StartSASLHandshake,
    StepSASLHandshake,
    StopConnectionLoop,
    UpdateSessionData,
)
from kazoo.retry import (
    ForceRetryError,
    KazooRetry,
    RetryFailedError,
)

if TYPE_CHECKING:
    from kazoo.client import KazooClient, WatchFunc
    from kazoo.interfaces import Socket, Threadlike
    from kazoo.protocol.serialization import ServerPacket


log = logging.getLogger(__name__)

# Re-exported from kazoo.protocol.session for backwards compatibility
__all__ = (
    "CHANGED_EVENT",
    "CHILD_EVENT",
    "CLOSE_RESPONSE",
    "CREATED_EVENT",
    "DELETED_EVENT",
    "STOP_CONNECTING",
    "ConnectionHandler",
    "RequestManager",
    "RWPinger",
    "RWServerAvailable",
    "WatcherManager",
)

# Micro-optimization: reuse ping instance and pre-encoded ping wire frame
_PingInstance = Ping()
_PING_FRAME = FrameDecoder.encode_request(_PingInstance, ReservedXID.PING)
# Cap per-write coalesced outbound frame batch size at 64 KiB
_SEND_BATCH_MAX = 64 * 1024
# Pre-allocated selector readiness singletons (0 allocations per poll)
_READY_EMPTY: frozenset[str] = frozenset()
_READY_ZK: frozenset[str] = frozenset(("zk_socket",))
_READY_WAKEUP: frozenset[str] = frozenset(("wakeup",))
_READY_BOTH: frozenset[str] = frozenset(("zk_socket", "wakeup"))

# Tracks which ConnectionHandler owns the currently executing zk_loop routine
# (works across OS threads, Gevent greenlets, and Eventlet greenthreads).
_ACTIVE_CONNECTION_LOOP: contextvars.ContextVar[ConnectionHandler | None] = (
    contextvars.ContextVar("_ACTIVE_CONNECTION_LOOP", default=None)
)


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
        self.schedule = RWProbeSchedule(hosts)
        self.socket_handling = socket_handling
        self.timeout = timeout
        self.logger = logger or log

    @property
    def last_attempt(self) -> float | None:
        return self.schedule.last_attempt

    @last_attempt.setter
    def last_attempt(self, value: float | None) -> None:
        self.schedule.last_attempt = value
        self.schedule._backoff_until = None

    def __iter__(
        self,
    ) -> Iterator[tuple[str, str, int] | Literal[False]]:
        sched = self.schedule
        while True:
            endpoint = sched.next_host(
                time.monotonic(), lambda: random.randint(0, 100) / 100.0
            )
            if endpoint is None:
                yield False
                continue
            host, hostip, port = endpoint
            self.logger.debug(
                "Pinging server for r/w: %s(%s):%s",
                host,
                hostip,
                port,
            )
            rw_host: tuple[str, str, int] | None = None
            try:
                with self.socket_handling():
                    sock = self.connection(host, hostip, port, self.timeout)
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
                sched.mark_found()
                sched.finish_probe(time.monotonic())
                yield rw_host
            else:
                sched.finish_probe(time.monotonic())
                yield False


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
        self._wakeup_signaled: bool = False

        self._socket: Socket | None = None
        self._ssl_socket: ssl.SSLSocket | None = None
        self._selector: selectors.BaseSelector | None = None
        self._requests: RequestManager = client._request_manager
        self._watchers: WatcherManager = client._watcher_manager
        self._packet_router = PacketRouter(
            requests=self._requests,
            watchers=self._watchers,
            get_state=lambda: self.client._state_machine.state,
            set_last_zxid=self._set_client_last_zxid,
            unchroot=lambda path: self.client.unchroot(path),
            emit_event=lambda ev: self._emit_event(ev),
            dispatch_watch=lambda cb: self.client.handler.dispatch_callback(
                cb
            ),
            logger=self.logger,
        )
        self._frame_decoder = FrameDecoder(
            max_buffer=max_buffer,
            pop_pending=self._requests.pop_pending,
        )
        self._send_buf = bytearray()
        self._replies_since_select: int = 0
        self._yield_control = client.handler.yield_control

        self._host_ports: list[tuple[str, str, int]] = []
        self._rw_server: tuple[str, str, int] | None = None
        self._ro_mode: (
            Iterator[Literal[False] | tuple[str, str, int]] | None
        ) = None

        self._connection_routine: Threadlike | None = None
        self._last_send: float = 0.0
        self._last_recv: float = 0.0
        self._read_timeout: float = 10.0
        self._connect_timeout: float = 10.0
        self._phase_deadline: float | None = None
        self._heartbeat_deadline: float | None = None
        self._heartbeat_jitter: float = 0.0
        self._heartbeat_cache_key: tuple[float, float] | None = None

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
        """Internal helper (used by tests) to inspect wakeup registration."""
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

    def _is_on_loop_routine(self) -> bool:
        """Return True if safe to mutate socket, selector, and decoder."""
        return (
            self.connection_stopped.is_set()
            or _ACTIVE_CONNECTION_LOOP.get() is self
        )

    def _close_socket(self) -> None:
        """Unregister and close the active transport socket."""
        self._update_io_interest(0, "zk_socket")
        if self._socket is not None:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None
        self._ssl_socket = None
        self._replies_since_select = 0
        self._phase_deadline = None
        self._heartbeat_deadline = None
        self._heartbeat_cache_key = None
        self._frame_decoder.reset()
        self._requests.fail_in_flight(ConnectionLoss())

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

    def _select(self, timeout: float) -> frozenset[str]:
        """Wait for readable events on the persistent selector."""
        ssl_sock = self._ssl_socket
        has_ssl_pending = ssl_sock is not None and ssl_sock.pending() > 0
        assert self._selector is not None
        events = self._selector.select(0.0 if has_ssl_pending else timeout)

        has_zk = has_ssl_pending
        has_wakeup = False
        for key, _ in events:
            data = key.data
            if data == "zk_socket":
                has_zk = True
            elif data == "wakeup":
                has_wakeup = True
        if has_zk:
            return _READY_BOTH if has_wakeup else _READY_ZK
        return _READY_WAKEUP if has_wakeup else _READY_EMPTY

    @contextmanager
    def _socket_io_wait(self) -> Iterator[Callable[[int, float], bool]]:
        """Temporarily dedicate ``self._selector`` to single-socket waiting.

        Unregisters ``wakeup`` for the duration of a blocking partial-write or
        TLS renegotiation wait (leaving unread wake bytes untouched in
        ``_read_sock`` so they neither busy-spin the level-triggered selector
        nor get lost), switches ``self._socket`` between ``EVENT_WRITE`` and
        ``EVENT_READ`` via ``modify()`` only when direction changes, and
        restores ``zk_socket`` (``EVENT_READ``) and ``wakeup`` on exit.
        """
        sel = self._selector
        sock = self._socket
        if sock is None:
            raise ConnectionDropped("socket closed")
        assert sel is not None

        wakeup_was_registered = self._wakeup_registered
        if wakeup_was_registered:
            self._update_io_interest(0, "wakeup")

        current_events = selectors.EVENT_READ

        def wait(events: int, timeout: float) -> bool:
            nonlocal current_events
            if events != current_events:
                sel.modify(sock, events, data="zk_socket")
                current_events = events
            return bool(sel.select(timeout))

        try:
            yield wait
        finally:
            if current_events != selectors.EVENT_READ and self._socket is sock:
                try:
                    sel.modify(sock, selectors.EVENT_READ, data="zk_socket")
                except (KeyError, ValueError, OSError):
                    pass
            if wakeup_was_registered and self._read_sock is not None:
                self._update_io_interest(selectors.EVENT_READ, "wakeup")

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
                if self._is_on_loop_routine():
                    self._close_socket()
                else:
                    self.wake()

            case SessionEstablished():
                self._phase_deadline = None
                if self._current_retry is not None:
                    self._current_retry.reset()
                if self._requests.queue:
                    self.wake()

            case ResetSessionData():
                if self._is_on_loop_routine():
                    self._frame_decoder.reset()
                    if self._current_retry is not None:
                        self._current_retry.reset()
                else:
                    self.wake()

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
            ):
                self.client._session_id = sid
                self.client._session_passwd = passwd
                self.client._protocol_version = proto_version

            case ConfigureTimeouts(
                read_timeout=r_timeout, connect_timeout=c_timeout
            ):
                self._read_timeout = r_timeout
                self._connect_timeout = c_timeout
                now = time.monotonic()
                self._last_send = now
                self._last_recv = now

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
        self._frame_decoder.release()
        self._send_buf = bytearray()
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
        kwargs: dict[str, Any] = {
            "address": (hostip, port),
            "hostname": host,
            "timeout": timeout,
            "use_ssl": client.use_ssl,
            "keyfile": client.keyfile,
            "certfile": client.certfile,
            "ca": client.ca,
            "keyfile_password": client.keyfile_password,
            "verify_certs": client.verify_certs,
            "check_hostname": client.check_hostname,
        }
        if client.use_ssl:
            ssl_ctx = client._get_ssl_context()
            if ssl_ctx is not None:
                kwargs["ssl_context"] = ssl_ctx
        return self.handler.create_connection(**kwargs)

    def wake(self) -> bool:
        """Wake the connection loop by sending a byte on the wakeup pipe.

        Returns True if the byte was sent successfully, False otherwise.
        """
        sock = self._write_sock
        if sock is None:
            return False
        self._wakeup_signaled = True
        try:
            sock.send(b"\0")
            return True
        except BlockingIOError:
            return True
        except OSError:
            return False

    def _drain_wakeup(self, *, drain_all: bool = False) -> None:
        """Drain bytes from the non-blocking wakeup pipe.

        When ``drain_all`` is False (hot path), stops after a short read
        (``< 4096`` bytes) to avoid a second ``recv()`` + ``BlockingIOError``
        syscall on every wakeup. Pass ``drain_all=True`` during teardown.
        """
        self._wakeup_signaled = False
        sock = self._read_sock
        if sock is None:
            return
        while True:
            try:
                chunk = sock.recv(4096)
                if not chunk or not isinstance(chunk, (bytes, bytearray)):
                    break
                if not drain_all and len(chunk) < 4096:
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
        self._drain_wakeup(drain_all=True)

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

        ssl_sock = self._ssl_socket
        write_deadline: float | None = None
        while True:
            buf = self._frame_decoder.get_buffer()
            try:
                nbytes = self._socket.recv_into(  # type: ignore[union-attr]
                    buf
                )
            except BlockingIOError:
                if ssl_sock is not None and ssl_sock.pending() > 0:
                    continue
                return None
            except ssl.SSLError as e:
                if e.errno == ssl.SSL_ERROR_WANT_READ:
                    if ssl_sock is not None and ssl_sock.pending() > 0:
                        continue
                    return None
                if e.errno == ssl.SSL_ERROR_WANT_WRITE:
                    now = time.monotonic()
                    if write_deadline is None:
                        eff = (
                            self._read_timeout if timeout is None else timeout
                        )
                        write_deadline = now + eff
                    rem = write_deadline - now
                    if rem <= 0.0:
                        raise self.handler.timeout_exception(
                            "socket time-out during read"
                        )
                    with self._socket_io_wait() as wait:
                        if not wait(selectors.EVENT_WRITE, rem):
                            raise self.handler.timeout_exception(
                                "socket time-out during read"
                            )
                    continue
                raise

            if nbytes == 0:
                self._frame_decoder.reset()
                raise ConnectionDropped("socket connection broken")
            self._last_recv = time.monotonic()
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
        level = BLATHER if isinstance(request, Ping) else logging.DEBUG
        if self.logger.isEnabledFor(level):
            self.logger.log(
                level,
                "Sending request(xid=%s): %s",
                xid,
                request,
            )
        self._write(FrameDecoder.encode_request(request, xid), timeout)

    def _write(
        self, msg: bytes | bytearray, timeout: float | None = None
    ) -> None:
        """Write raw bytes to the socket.

        Attempts an optimistic non-blocking write first for the fast-path.
        If a partial write occurs or socket buffer is full, drains the
        remainder using the persistent selector bounded by timeout.
        """
        if self._socket is None:
            raise ConnectionDropped("socket closed")

        sent = 0
        msg_length = len(msg)
        want_read = False
        # Use .write() on SSL sockets so non-blocking gevent.ssl.SSLSocket
        # re-raises SSLWantReadError/SSLWantWriteError instead of returning 0.
        ssl_sock = self._ssl_socket
        write_fn = (
            ssl_sock.write
            if ssl_sock is not None and hasattr(ssl_sock, "write")
            else self._socket.send
        )

        with memoryview(msg) as view:
            # Fast-path: optimistic direct non-blocking send
            try:
                bytes_sent = write_fn(view)
                if not bytes_sent:
                    raise ConnectionDropped("socket connection broken")
                if bytes_sent >= msg_length:
                    return
                sent = bytes_sent
            except BlockingIOError:
                pass
            except ssl.SSLError as e:
                if e.errno == ssl.SSL_ERROR_WANT_READ:
                    want_read = True
                elif e.errno != ssl.SSL_ERROR_WANT_WRITE:
                    raise
            except OSError as e:
                raise ConnectionDropped(f"socket connection error: {e}") from e

            # Slow-path: partial write or buffer full, drain bounded by timeout
            eff_timeout = self._connect_timeout if timeout is None else timeout
            deadline = time.monotonic() + eff_timeout
            with self._socket_io_wait() as wait:
                while sent < msg_length:
                    rem_timeout = deadline - time.monotonic()
                    if rem_timeout <= 0.0:
                        raise self.handler.timeout_exception(
                            "socket time-out during write"
                        )
                    events = (
                        selectors.EVENT_READ
                        if want_read
                        else selectors.EVENT_WRITE
                    )
                    if not wait(events, rem_timeout):
                        raise self.handler.timeout_exception(
                            "socket time-out during write"
                        )
                    try:
                        with view[sent:] as chunk:
                            bytes_sent = write_fn(chunk)
                    except BlockingIOError:
                        want_read = False
                        continue
                    except ssl.SSLError as e:
                        if e.errno == ssl.SSL_ERROR_WANT_READ:
                            want_read = True
                            continue
                        if e.errno == ssl.SSL_ERROR_WANT_WRITE:
                            want_read = False
                            continue
                        raise
                    except OSError as e:
                        raise ConnectionDropped(
                            f"socket connection error: {e}"
                        ) from e
                    if not bytes_sent:
                        raise ConnectionDropped("socket connection broken")
                    want_read = False
                    sent += bytes_sent

    def _set_client_last_zxid(self, zxid: int) -> None:
        self.client.last_zxid = zxid

    def _find_persistent_recursive_watchers(
        self, path: str
    ) -> list[WatchFunc]:
        """Internal test helper forwarding to ``WatcherManager``."""
        return self._watchers.find_persistent_recursive(path)

    def _read_watch_event(self, watch: WatchPacket) -> None:
        """Internal test helper forwarding to ``PacketRouter``."""
        self._packet_router.read_watch_event(watch)

    def _read_response(self, packet: ResponsePacket) -> object | None:
        """Internal test helper forwarding to ``PacketRouter``."""
        return self._packet_router.read_response(packet)

    def _process_packet(self, packet: ServerPacket) -> object | None:
        """Process a decoded ServerPacket from FrameDecoder."""
        return self._packet_router.process_packet(packet)

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
        connect_timeout: float,
    ) -> None:
        """Called when we have something to send out on the socket.

        Coalesces queued requests into contiguous send batches (up to
        ``_SEND_BATCH_MAX`` bytes per ``_write``) to reduce per-request
        ``send()`` syscalls while preserving wire order.

        When terminal (e.g. during client.stop()), all queued requests bypass
        the concurrent request limit window so that shutdown completes promptly
        without waiting behind a saturated pipeline.
        """
        limit = self.client.concurrent_request_limit
        terminal = self._is_terminal
        batch = self._send_buf
        batch.clear()
        encode_into = FrameDecoder.encode_request_into

        try:
            while terminal or self._requests.has_send_capacity(limit):
                try:
                    request, _ = self._requests.peek_queued()
                except IndexError:
                    break

                # Special case for testing, if this is a _SessionExpire object
                # then throw a SessionExpiration error as if we were dropped
                if request is _SESSION_EXPIRED:
                    if batch:
                        self._write(batch, connect_timeout)
                        self._last_send = time.monotonic()
                    raise SessionExpiredError("Session expired: Testing")
                if request is _CONNECTION_DROP:
                    if batch:
                        self._write(batch, connect_timeout)
                        self._last_send = time.monotonic()
                    raise ConnectionDropped("Connection dropped: Testing")

                # Special case for auth packets
                if request.type == Auth.type:
                    xid: int = ReservedXID.AUTH
                else:
                    xid = self._requests.next_xid()

                self._requests.dequeue_to_pending(xid)
                level = BLATHER if isinstance(request, Ping) else logging.DEBUG
                if self.logger.isEnabledFor(level):
                    self.logger.log(
                        level, "Sending request(xid=%s): %s", xid, request
                    )
                encode_into(batch, request, xid)
                if len(batch) >= _SEND_BATCH_MAX:
                    self._write(batch, connect_timeout)
                    self._last_send = time.monotonic()
                    if len(batch) > _SEND_BATCH_MAX:
                        batch = self._send_buf = bytearray()
                    else:
                        batch.clear()

            if batch:
                self._write(batch, connect_timeout)
                self._last_send = time.monotonic()
        finally:
            if len(batch) > _SEND_BATCH_MAX:
                self._send_buf = bytearray()
            else:
                batch.clear()

    def _send_ping(self, connect_timeout: float) -> None:
        if self.logger.isEnabledFor(BLATHER):
            self.logger.log(
                BLATHER,
                "Sending request(xid=%s): %s",
                ReservedXID.PING,
                _PingInstance,
            )
        self._write(_PING_FRAME, connect_timeout)

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
        loop_token = _ACTIVE_CONNECTION_LOOP.set(self)

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
            try:
                if not isinstance(
                    self.client.state_machine, (Inactive, AuthFailed)
                ):
                    self._emit_event(SessionClosedEvent())
            finally:
                # StopConnectionLoop wrote a wake byte; nothing selects after
                # this routine returns.
                self._release_wakeup()
                _ACTIVE_CONNECTION_LOOP.reset(loop_token)
                self.connection_stopped.set()
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

    def _get_heartbeat_deadline(self) -> tuple[float, float]:
        """Return cached ``(deadline, jitter)`` for the current activity."""
        ref_time = (
            min(self._last_send, self._last_recv)
            if self._last_recv > 0.0
            else self._last_send
        )
        key = (ref_time, self._read_timeout)
        if (
            self._heartbeat_cache_key != key
            or self._heartbeat_deadline is None
        ):
            jitter = random.randint(1, 40) / 100.0
            self._heartbeat_jitter = jitter
            self._heartbeat_deadline = (
                ref_time + self._read_timeout / 2.0 - jitter
            )
            self._heartbeat_cache_key = key
        return self._heartbeat_deadline, self._heartbeat_jitter

    def _compute_poll_timeout(self) -> tuple[float, float | None]:
        """Compute the selector timeout and optional heartbeat deadline."""
        if not self._is_session_active:
            if self._phase_deadline is not None:
                rem = max(0.0, self._phase_deadline - time.monotonic())
                return rem, None
            return self._connect_timeout, None

        now = time.monotonic()
        deadline, jitter_time = self._get_heartbeat_deadline()
        state = self.client._state_machine.state
        if (
            isinstance(state, (Connected, ConnectedReadOnly))
            and state.ping_outstanding
        ):
            ping_rem = max(
                0.0,
                (self._last_send + self._read_timeout / 2.0 - jitter_time)
                - now,
            )
            if self._last_recv > 0.0:
                recv_rem = max(
                    0.0, (self._last_recv + self._read_timeout) - now
                )
                return min(ping_rem, recv_rem), deadline
            return ping_rem, deadline

        timeout = max(deadline - now, jitter_time)
        if (
            self._last_recv > 0.0
            and isinstance(state, (Connected, ConnectedReadOnly))
            and (self._requests.pending or self._requests.auth_pending)
        ):
            recv_rem = max(0.0, (self._last_recv + self._read_timeout) - now)
            if recv_rem < timeout:
                timeout = recv_rem
        return timeout, deadline

    def _check_session_health(
        self,
        deadline: float | None = None,
        now: float | None = None,
    ) -> None:
        """Send heartbeat pings and drop half-open connections past timeout."""
        state = self.client._state_machine.state
        if not isinstance(state, (Connected, ConnectedReadOnly)):
            return
        if now is None:
            now = time.monotonic()
        if (
            self._last_recv > 0.0
            and (
                state.ping_outstanding
                or self._requests.pending
                or self._requests.auth_pending
            )
            and now - self._last_recv >= self._read_timeout
        ):
            raise self.handler.timeout_exception(
                "socket time-out: no response from server"
            )
        if not state.ping_outstanding and self._last_send > 0.0:
            fresh_deadline, _ = self._get_heartbeat_deadline()
            if now >= fresh_deadline:
                self._emit_event(HEARTBEAT_DEADLINE_REACHED)

    def _dispatch_ready_events(
        self, ready: AbstractSet[str], deadline: float | None
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
                if self._is_session_active:
                    self._drain_wakeup()
                    self._send_request(self._connect_timeout)
                    self._check_session_health()
                    return True
                else:
                    # WAKEUP_SIGNALED runs DrainWakeupPipe via state machine.
                    self._emit_event(WAKEUP_SIGNALED)
                    return True

        if self._is_session_active:
            self._check_session_health()
        elif (
            self._phase_deadline is not None
            and time.monotonic() >= self._phase_deadline
        ):
            self._emit_event(POLL_TIMEOUT_EXPIRED)

        return False

    def _poll_step(self) -> None:
        """Execute a single cycle of the selector I/O loop."""
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
            if self._wakeup_signaled:
                self._drain_wakeup()
            self._send_request(self._connect_timeout)

        if self._frame_decoder.frame_ready:
            self._replies_since_select += 1
            should_yield = (
                self._replies_since_select >= self._REPLIES_PER_YIELD
            )
            if should_yield:
                self._replies_since_select = 0
                self._yield_control()
            if self._is_session_active:
                self._read_socket()
                if (
                    should_yield
                    and self.client._state_machine.state.is_connected
                ):
                    self._check_session_health()
            else:
                self._emit_event(SOCKET_READABLE)
            return

        self._replies_since_select = 0
        timeout, deadline = self._compute_poll_timeout()
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
            self._ssl_socket = (
                cast("ssl.SSLSocket", self._socket)
                if hasattr(self._socket, "pending")
                else None
            )
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

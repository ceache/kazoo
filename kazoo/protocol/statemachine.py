"""Pure Functional Dataclass State Machine for KazooClient."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, TypeAlias

from kazoo.exceptions import (
    AuthFailedError,
    ConnectionClosedError,
    InvalidStateTransitionError,
)
from kazoo.loggingsupport import BLATHER
from kazoo.protocol.serialization import Connect
from kazoo.protocol.states import KazooState, KeeperState

if TYPE_CHECKING:
    from kazoo.interfaces import IHandler


# =====================================================================
# 1. States (Immutable Dataclasses representing all valid configurations)
# =====================================================================


class AuthStage(Enum):
    """Sub-stages of session authentication during handshake."""

    SASL = "sasl"
    INITIAL_AUTH = "initial_auth"


@dataclass(frozen=True, slots=True)
class Inactive:
    """Client is explicitly stopped or not yet started."""

    keeper_state: KeeperState = KeeperState.CLOSED
    kazoo_state: KazooState = KazooState.LOST
    is_terminal: bool = False
    is_connected: bool = False
    is_connecting_allowed: bool = False


@dataclass(frozen=True, slots=True)
class Connecting:
    """Initial connection attempt before a live session is established."""

    host: str | None = None
    port: int | None = None
    attempt: int = 0
    keeper_state: KeeperState = KeeperState.CONNECTING
    kazoo_state: KazooState = KazooState.LOST
    is_terminal: bool = False
    is_connected: bool = False
    is_connecting_allowed: bool = True


@dataclass(frozen=True, slots=True)
class Handshaking:
    """Socket is connected; ZooKeeper Connect request in-flight."""

    host: str
    port: int
    session_id: int | None = None
    read_only: bool = False
    attempt: int = 0
    has_sasl: bool = False
    auth_data: tuple[tuple[str, str], ...] = ()
    num_hosts: int = 1
    keeper_state: KeeperState = KeeperState.CONNECTING
    kazoo_state: KazooState = KazooState.LOST
    is_terminal: bool = False
    is_connected: bool = False
    is_connecting_allowed: bool = True


@dataclass(frozen=True, slots=True)
class Authenticating:
    """TCP and ZK Connect succeeded; performing SASL or initial Auth."""

    stage: AuthStage = AuthStage.SASL
    host: str | None = None
    port: int | None = None
    session_id: int | None = None
    read_timeout: float = 10.0
    connect_timeout: float = 10.0
    read_only: bool = False
    pending_auth: tuple[tuple[str, str], ...] = ()
    keeper_state: KeeperState = KeeperState.CONNECTING
    kazoo_state: KazooState = KazooState.LOST
    is_terminal: bool = False
    is_connected: bool = False
    is_connecting_allowed: bool = True


@dataclass(frozen=True, slots=True)
class Connected:
    """Active read-write ZooKeeper session."""

    host: str | None = None
    port: int | None = None
    session_id: int | None = None
    read_timeout: float = 10.0
    connect_timeout: float = 10.0
    ping_outstanding: bool = False
    keeper_state: KeeperState = KeeperState.CONNECTED
    kazoo_state: KazooState = KazooState.CONNECTED
    is_terminal: bool = False
    is_connected: bool = True
    is_connecting_allowed: bool = True


@dataclass(frozen=True, slots=True)
class ConnectedReadOnly:
    """Active read-only ZooKeeper session."""

    host: str | None = None
    port: int | None = None
    session_id: int | None = None
    read_timeout: float = 10.0
    connect_timeout: float = 10.0
    ping_outstanding: bool = False
    keeper_state: KeeperState = KeeperState.CONNECTED_RO
    kazoo_state: KazooState = KazooState.CONNECTED
    is_terminal: bool = False
    is_connected: bool = True
    is_connecting_allowed: bool = True


@dataclass(frozen=True, slots=True)
class Suspended:
    """Connection dropped while a session was active; attempting reconnect."""

    host: str | None = None
    port: int | None = None
    session_id: int | None = None
    attempt: int = 0
    keeper_state: KeeperState = KeeperState.CONNECTING
    kazoo_state: KazooState = KazooState.SUSPENDED
    is_terminal: bool = False
    is_connected: bool = False
    is_connecting_allowed: bool = True


@dataclass(frozen=True, slots=True)
class ExpiredSession:
    """Session was invalidated by the ZooKeeper ensemble."""

    session_id: int | None = None
    keeper_state: KeeperState = KeeperState.EXPIRED_SESSION
    kazoo_state: KazooState = KazooState.LOST
    is_terminal: bool = False
    is_connected: bool = False
    is_connecting_allowed: bool = True


@dataclass(frozen=True, slots=True)
class AuthFailed:
    """Authentication (SASL or Auth packet) permanently failed."""

    error: Exception | None = None
    keeper_state: KeeperState = KeeperState.AUTH_FAILED
    kazoo_state: KazooState = KazooState.LOST
    is_terminal: bool = True
    is_connected: bool = False
    is_connecting_allowed: bool = False


ClientState: TypeAlias = (
    Inactive
    | Connecting
    | Handshaking
    | Authenticating
    | Connected
    | ConnectedReadOnly
    | Suspended
    | ExpiredSession
    | AuthFailed
)


# =====================================================================
# 2. Events (Immutable Dataclasses representing all inputs to the machine)
# =====================================================================


@dataclass(frozen=True, slots=True)
class StartRequested:
    """Emitted when client/connection startup is explicitly requested."""


@dataclass(frozen=True, slots=True)
class ConnectAttemptStarted:
    """Emitted when ConnectionHandler begins a connection attempt to a host."""

    host: str
    port: int


@dataclass(frozen=True, slots=True)
class SocketConnected:
    """Emitted when transport socket connection is established."""

    host: str
    port: int = 2181
    num_hosts: int = 1
    session_id: int = 0
    session_passwd: bytes = b""
    session_timeout: int = 10000
    last_zxid: int = 0
    read_only: bool = False
    has_sasl: bool = False
    auth_data: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ConnectResponseReceived:
    """Emitted when Connect response packet is read from the server."""

    connect_result: Connect
    zxid: int | None = None


@dataclass(frozen=True, slots=True)
class SASLChallengeReceived:
    """Emitted when a SASL challenge packet is read from the server."""

    challenge: bytes | None = None


@dataclass(frozen=True, slots=True)
class SASLCompleted:
    """Emitted when the SASL handshake finishes all challenge steps."""


@dataclass(frozen=True, slots=True)
class AuthPacketCompleted:
    """Emitted when an XID.AUTH response with err=0 is received."""


@dataclass(frozen=True, slots=True)
class ConnectionLostEvent:
    """Emitted when an active socket connection is dropped or times out."""

    reason: str = ""
    keeper_state: KeeperState = KeeperState.CONNECTING


@dataclass(frozen=True, slots=True)
class SessionExpiredEvent:
    """Emitted when the server reports that the session has expired."""

    session_id: int | None = None


@dataclass(frozen=True, slots=True)
class AuthFailedEvent:
    """Emitted when SASL or Auth packet authentication fails."""

    error: Exception | None = None


@dataclass(frozen=True, slots=True)
class SessionClosedEvent:
    """Emitted when the session is explicitly closed."""


@dataclass(frozen=True, slots=True)
class CloseRequested:
    """Emitted when a graceful session stop is requested by the caller."""


@dataclass(frozen=True, slots=True)
class RWServerFoundEvent:
    """Emitted in read-only mode when a read-write server is discovered."""

    server: tuple[str, int] | None = None


@dataclass(frozen=True, slots=True)
class SocketReadable:
    """Emitted by the selector loop when the ZK socket is readable."""


@dataclass(frozen=True, slots=True)
class WakeUpSignaled:
    """Emitted by the selector loop when the wakeup pipe is readable."""


@dataclass(frozen=True, slots=True)
class PollTimeoutExpired:
    """Emitted when selector.select(timeout) returns no ready events."""


@dataclass(frozen=True, slots=True)
class HeartbeatDeadlineReached:
    """Emitted when the heartbeat ping deadline is reached."""


@dataclass(frozen=True, slots=True)
class PingReceived:
    """Emitted when an XID.PING response header is read from the server."""


ClientEvent: TypeAlias = (
    StartRequested
    | ConnectAttemptStarted
    | SocketConnected
    | ConnectResponseReceived
    | SASLChallengeReceived
    | SASLCompleted
    | AuthPacketCompleted
    | ConnectionLostEvent
    | SessionExpiredEvent
    | AuthFailedEvent
    | SessionClosedEvent
    | CloseRequested
    | RWServerFoundEvent
    | SocketReadable
    | WakeUpSignaled
    | PollTimeoutExpired
    | HeartbeatDeadlineReached
    | PingReceived
)


# =====================================================================
# 3. Side Effects (Descriptive commands returned by transition())
# =====================================================================


@dataclass(frozen=True, slots=True)
class LogStateTransition:
    """Log a message at the given logging level."""

    level: int
    message: str
    args: tuple[Any, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolveConnectResult:
    """Resolve or fail the pending start_async() IAsyncResult."""

    success: bool
    exception: Exception | None = None


@dataclass(frozen=True, slots=True)
class EmitKazooStateChange:
    """Fire registered KazooState listeners if the KazooState changed."""

    new_state: KazooState


@dataclass(frozen=True, slots=True)
class FailPendingRequests:
    """Fail all pending and queued requests with the appropriate exception."""

    keeper_state: KeeperState


@dataclass(frozen=True, slots=True)
class InvalidateWatchers:
    """Clear registered watchers and notify them with a NONE WatchedEvent."""


@dataclass(frozen=True, slots=True)
class ResetSessionData:
    """Reset client queues, watchers, session id/password, and zxid."""


@dataclass(frozen=True, slots=True)
class DrainWakeupPipe:
    """Drain stray wakeup bytes from the wakeup pipe without unregistering."""


@dataclass(frozen=True, slots=True)
class StartSASLHandshake:
    """Initialize SASLExchange and send the first SASL token."""

    host: str


@dataclass(frozen=True, slots=True)
class StepSASLHandshake:
    """Process a server SASL challenge and send the next SASL token."""

    challenge: bytes | None = None


@dataclass(frozen=True, slots=True)
class SendAuthPacket:
    """Submit a ZooKeeper Auth packet with xid=XID.AUTH."""

    scheme: str
    credential: str


@dataclass(frozen=True, slots=True)
class ReadSocketPacket:
    """Read and process the next packet from the ZooKeeper socket."""


@dataclass(frozen=True, slots=True)
class SendHeartbeatPing:
    """Send a heartbeat Ping request (and check RW server if in RO mode)."""


@dataclass(frozen=True, slots=True)
class CloseSocket:
    """Close the transport socket and unregister from selector."""

    reason: str


@dataclass(frozen=True, slots=True)
class SessionEstablished:
    """Instruct connection handler that the session is established."""


@dataclass(frozen=True, slots=True)
class StopConnectionLoop:
    """Instruct the connection handler loop to permanently stop connecting."""


@dataclass(frozen=True, slots=True)
class SendConnectPacket:
    """Submit initial ZooKeeper Connect request packet to socket."""

    connect: Connect
    timeout: float


@dataclass(frozen=True, slots=True)
class ReadConnectResponsePacket:
    """Read length-prefixed Connect response packet from readable socket."""


@dataclass(frozen=True, slots=True)
class UpdateSessionData:
    """Update client session id, password, protocol version, and zxid."""

    session_id: int
    session_passwd: bytes
    protocol_version: int
    last_zxid: int | None = None


@dataclass(frozen=True, slots=True)
class ConfigureTimeouts:
    """Configure negotiated read and connect timeouts."""

    read_timeout: float
    connect_timeout: float


@dataclass(frozen=True, slots=True)
class ConfigureReadOnlyMode:
    """Enable or disable read-only server pinger mode."""

    read_only: bool


@dataclass(frozen=True, slots=True)
class EnqueueCloseRequest:
    """Enqueue a Close request to the send queue and wake the connection."""


SideEffect: TypeAlias = (
    LogStateTransition
    | ResolveConnectResult
    | EmitKazooStateChange
    | FailPendingRequests
    | InvalidateWatchers
    | ResetSessionData
    | DrainWakeupPipe
    | StartSASLHandshake
    | StepSASLHandshake
    | SendAuthPacket
    | ReadSocketPacket
    | SendHeartbeatPing
    | CloseSocket
    | SessionEstablished
    | StopConnectionLoop
    | SendConnectPacket
    | ReadConnectResponsePacket
    | UpdateSessionData
    | ConfigureTimeouts
    | ConfigureReadOnlyMode
    | EnqueueCloseRequest
)

# Pre-allocated hot-path event and effect singletons to avoid per-poll
# heap allocations in the steady-state I/O loop.
SOCKET_READABLE = SocketReadable()
WAKEUP_SIGNALED = WakeUpSignaled()
POLL_TIMEOUT_EXPIRED = PollTimeoutExpired()
HEARTBEAT_DEADLINE_REACHED = HeartbeatDeadlineReached()
PING_RECEIVED = PingReceived()

_READ_SOCKET_PACKET = ReadSocketPacket()
_READ_CONNECT_RESPONSE_PACKET = ReadConnectResponsePacket()
_DRAIN_WAKEUP_PIPE = DrainWakeupPipe()
_SEND_HEARTBEAT_PING = SendHeartbeatPing()
_ENQUEUE_CLOSE_REQUEST = EnqueueCloseRequest()

_EFFECTS_READ_SOCKET: list[SideEffect] = [_READ_SOCKET_PACKET]
_EFFECTS_READ_CONNECT: list[SideEffect] = [_READ_CONNECT_RESPONSE_PACKET]
_EFFECTS_DRAIN_WAKEUP: list[SideEffect] = [_DRAIN_WAKEUP_PIPE]
_EFFECTS_SEND_PING: list[SideEffect] = [_SEND_HEARTBEAT_PING]
_NO_EFFECTS: list[SideEffect] = []


# =====================================================================
# 4. Pure Transition Matrix
# =====================================================================


def _build_auth_exception(error: Exception | None) -> AuthFailedError:
    if isinstance(error, AuthFailedError):
        return error
    err = AuthFailedError("Authentication failed")
    if error is not None:
        err.__cause__ = error
    return err


def _suspend_effects(
    keeper_state: KeeperState,
    emit_kazoo_change: bool = True,
) -> list[SideEffect]:
    effects: list[SideEffect] = [
        LogStateTransition(logging.INFO, "Zookeeper connection lost"),
        FailPendingRequests(keeper_state),
    ]
    if emit_kazoo_change:
        effects.append(EmitKazooStateChange(KazooState.SUSPENDED))
    effects.append(InvalidateWatchers())
    return effects


def _reconnect_after_drop(
    state: Handshaking | Authenticating,
    keeper_state: KeeperState = KeeperState.CONNECTING,
) -> Suspended | Connecting:
    if state.kazoo_state == KazooState.SUSPENDED:
        return Suspended(
            host=state.host,
            port=state.port,
            session_id=state.session_id,
            attempt=1,
            keeper_state=keeper_state,
            kazoo_state=KazooState.SUSPENDED,
            is_connecting_allowed=state.is_connecting_allowed,
        )
    return Connecting(
        host=state.host,
        port=state.port,
        attempt=1,
        is_connecting_allowed=state.is_connecting_allowed,
    )


def _finalize_connected(
    host: str | None,
    port: int | None,
    session_id: int | None,
    read_timeout: float,
    connect_timeout: float,
    read_only: bool,
) -> tuple[ClientState, list[SideEffect]]:
    next_state: ClientState
    if read_only:
        next_state = ConnectedReadOnly(
            host=host,
            port=port,
            session_id=session_id,
            read_timeout=read_timeout,
            connect_timeout=connect_timeout,
        )
    else:
        next_state = Connected(
            host=host,
            port=port,
            session_id=session_id,
            read_timeout=read_timeout,
            connect_timeout=connect_timeout,
        )
    effects: list[SideEffect] = [
        LogStateTransition(
            logging.INFO,
            "Zookeeper connection established, state: %s",
            (next_state.keeper_state,),
        ),
        LogStateTransition(
            BLATHER,
            "Session created, session_id: %r\n"
            "    connect timeout: %ss\n"
            "    read timeout: %ss",
            (session_id, connect_timeout, read_timeout),
        ),
        ResolveConnectResult(success=True),
        EmitKazooStateChange(KazooState.CONNECTED),
        SessionEstablished(),
    ]
    return next_state, effects


def transition(
    state: ClientState, event: ClientEvent
) -> tuple[ClientState, list[SideEffect]]:
    """Pure state transition function. Zero I/O happens inside here."""
    match (state, event):
        # -----------------------------------------------------------------
        # 1. Steady-State Hot-Loop Events (Evaluated first for fast jumps)
        # -----------------------------------------------------------------
        case (Authenticating(), SocketReadable()):
            return state, _EFFECTS_READ_SOCKET

        case (Handshaking(), SocketReadable()):
            return state, _EFFECTS_READ_CONNECT

        case (
            Inactive()
            | Connecting()
            | Suspended()
            | AuthFailed()
            | ExpiredSession(),
            SocketReadable(),
        ):
            return state, _NO_EFFECTS

        case (
            Inactive()
            | Connecting()
            | Handshaking()
            | Authenticating()
            | Suspended()
            | AuthFailed()
            | ExpiredSession(),
            WakeUpSignaled(),
        ):
            return state, _EFFECTS_DRAIN_WAKEUP

        case (
            Connected() | ConnectedReadOnly(),
            PingReceived(),
        ):
            if not state.ping_outstanding:
                return state, _NO_EFFECTS
            return replace(state, ping_outstanding=False), _NO_EFFECTS

        case (
            _,
            PingReceived(),
        ):
            return state, _NO_EFFECTS

        case (
            Connected() | ConnectedReadOnly(),
            HeartbeatDeadlineReached(),
        ):
            return (
                replace(state, ping_outstanding=True),
                _EFFECTS_SEND_PING,
            )

        case (
            Inactive()
            | Connecting()
            | Handshaking()
            | Authenticating()
            | Suspended()
            | ExpiredSession()
            | AuthFailed(),
            HeartbeatDeadlineReached(),
        ):
            return state, _NO_EFFECTS

        case (
            Connected(ping_outstanding=True)
            | ConnectedReadOnly(ping_outstanding=True),
            PollTimeoutExpired(),
        ):
            return (
                Suspended(
                    host=state.host,
                    port=state.port,
                    session_id=state.session_id,
                    is_connecting_allowed=state.is_connecting_allowed,
                ),
                [
                    CloseSocket(
                        reason="outstanding heartbeat ping not received"
                    ),
                    LogStateTransition(
                        logging.WARNING,
                        "Connection dropped: %s",
                        ("outstanding heartbeat ping not received",),
                    ),
                    *_suspend_effects(KeeperState.CONNECTING),
                ],
            )

        case (
            Connected() | ConnectedReadOnly(),
            PollTimeoutExpired(),
        ):
            return state, []

        case (
            Authenticating(),
            PollTimeoutExpired(),
        ):
            return _reconnect_after_drop(state), [
                CloseSocket(reason="socket time-out during authentication"),
                LogStateTransition(
                    logging.WARNING,
                    "Connection time-out: %s",
                    ("socket time-out during authentication",),
                ),
            ]

        case (
            Handshaking(),
            PollTimeoutExpired(),
        ):
            return _reconnect_after_drop(state), [
                CloseSocket(reason="socket time-out during handshake"),
                LogStateTransition(
                    logging.WARNING,
                    "Connection time-out: %s",
                    ("socket time-out during handshake",),
                ),
            ]

        case (
            Inactive()
            | Connecting()
            | Suspended()
            | ExpiredSession()
            | AuthFailed(),
            PollTimeoutExpired(),
        ):
            return state, []

        # -----------------------------------------------------------------
        # 2. Lifecycle & Connection Initialization Events
        # -----------------------------------------------------------------
        case (
            Inactive() | ExpiredSession() | AuthFailed(),
            StartRequested(),
        ):
            return Connecting(attempt=0), []

        case (
            Connecting()
            | Handshaking()
            | Authenticating()
            | Connected()
            | ConnectedReadOnly()
            | Suspended(),
            StartRequested(),
        ):
            return state, []

        # Inactive absorbs all other events not explicitly handled in
        # section 1 above (e.g. SocketReadable and WakeUpSignaled ->
        # DrainWakeupPipe remain active).
        case (Inactive(), _):
            return state, []

        case (
            state,
            ConnectAttemptStarted(),
        ) if not state.is_connecting_allowed:
            return state, [
                LogStateTransition(BLATHER, "Skipping state change")
            ]

        case (
            ExpiredSession(),
            ConnectAttemptStarted(host=host, port=port),
        ):
            return (
                Connecting(host=host, port=port, attempt=1),
                [LogStateTransition(BLATHER, "Skipping state change")],
            )

        case (
            Connecting(attempt=attempt),
            ConnectAttemptStarted(host=host, port=port),
        ):
            if attempt == 0:
                return (
                    Connecting(host=host, port=port, attempt=1),
                    [LogStateTransition(BLATHER, "Skipping state change")],
                )
            return (
                Connecting(host=host, port=port, attempt=attempt + 1),
                [],
            )

        case (
            Suspended(session_id=session_id, attempt=attempt),
            ConnectAttemptStarted(host=host, port=port),
        ):
            return (
                Suspended(
                    host=host,
                    port=port,
                    session_id=session_id,
                    attempt=attempt + 1,
                ),
                [],
            )

        case (
            Connected(
                session_id=session_id,
                is_connecting_allowed=allowing,
            )
            | ConnectedReadOnly(
                session_id=session_id,
                is_connecting_allowed=allowing,
            ),
            ConnectAttemptStarted(host=host, port=port),
        ):
            next_state: ClientState = Suspended(
                host=host,
                port=port,
                session_id=session_id,
                attempt=1,
                is_connecting_allowed=allowing,
            )
            return next_state, _suspend_effects(KeeperState.CONNECTING)

        case (
            Authenticating() | Handshaking(),
            ConnectAttemptStarted(host=host, port=port),
        ):
            return (
                replace(_reconnect_after_drop(state), host=host, port=port),
                [],
            )

        # -----------------------------------------------------------------
        # 3. SocketConnected & Handshake Negotiation
        # -----------------------------------------------------------------
        case (
            state,
            SocketConnected(),
        ) if not state.is_connecting_allowed:
            return state, []

        case (
            Connecting() | Suspended() | Handshaking(),
            SocketConnected(
                host=host,
                port=port,
                num_hosts=num_hosts,
                session_id=session_id,
                session_passwd=session_passwd,
                session_timeout=session_timeout,
                last_zxid=last_zxid,
                read_only=read_only,
                has_sasl=has_sasl,
                auth_data=auth_data,
            ),
        ):
            connect_req = Connect(
                0,
                last_zxid,
                session_timeout,
                session_id,
                session_passwd,
                read_only,
            )
            connect_timeout = (session_timeout / 1000.0) / num_hosts
            next_state = Handshaking(
                host=host,
                port=port,
                session_id=session_id,
                read_only=read_only,
                attempt=state.attempt or 1,
                has_sasl=has_sasl,
                auth_data=auth_data,
                num_hosts=num_hosts,
                kazoo_state=state.kazoo_state,
            )
            return next_state, [
                SendConnectPacket(
                    connect=connect_req, timeout=connect_timeout
                ),
            ]

        case (
            AuthFailed() | Handshaking(is_connecting_allowed=False),
            ConnectResponseReceived(),
        ):
            return state, []

        case (
            Handshaking(
                host=host,
                port=port,
                has_sasl=has_sasl,
                auth_data=auth_data,
                num_hosts=num_hosts,
            ),
            ConnectResponseReceived(connect_result=connect_result, zxid=zxid),
        ):
            if connect_result.time_out <= 0:
                expired_connect_effects: list[SideEffect] = [
                    LogStateTransition(
                        logging.WARNING,
                        "Zookeeper session closed, state: %s",
                        (KeeperState.EXPIRED_SESSION,),
                    ),
                    EmitKazooStateChange(KazooState.LOST),
                    FailPendingRequests(KeeperState.EXPIRED_SESSION),
                    ResetSessionData(),
                    CloseSocket("Session has expired"),
                ]
                return (
                    ExpiredSession(session_id=connect_result.session_id),
                    expired_connect_effects,
                )

            negotiated_session_timeout = connect_result.time_out / 1000.0
            connect_timeout = negotiated_session_timeout / num_hosts
            read_timeout = negotiated_session_timeout * 2.0 / 3.0
            conn_session_id = connect_result.session_id
            eff_ro = bool(connect_result.read_only)

            common_effects: list[SideEffect] = [
                UpdateSessionData(
                    session_id=conn_session_id,
                    session_passwd=connect_result.passwd,
                    protocol_version=connect_result.protocol_version,
                    last_zxid=zxid,
                ),
                ConfigureTimeouts(
                    read_timeout=read_timeout,
                    connect_timeout=connect_timeout,
                ),
                ConfigureReadOnlyMode(read_only=eff_ro),
            ]

            if has_sasl:
                return (
                    Authenticating(
                        stage=AuthStage.SASL,
                        host=host,
                        port=port,
                        session_id=conn_session_id,
                        read_timeout=read_timeout,
                        connect_timeout=connect_timeout,
                        read_only=eff_ro,
                        pending_auth=auth_data,
                        kazoo_state=state.kazoo_state,
                    ),
                    common_effects + [StartSASLHandshake(host=host)],
                )

            if auth_data:
                first_scheme, first_cred = auth_data[0]
                return (
                    Authenticating(
                        stage=AuthStage.INITIAL_AUTH,
                        host=host,
                        port=port,
                        session_id=conn_session_id,
                        read_timeout=read_timeout,
                        connect_timeout=connect_timeout,
                        read_only=eff_ro,
                        pending_auth=auth_data[1:],
                        kazoo_state=state.kazoo_state,
                    ),
                    common_effects
                    + [
                        SendAuthPacket(
                            scheme=first_scheme, credential=first_cred
                        ),
                    ],
                )

            finalize_state, finalize_effects = _finalize_connected(
                host=host,
                port=port,
                session_id=conn_session_id,
                read_timeout=read_timeout,
                connect_timeout=connect_timeout,
                read_only=eff_ro,
            )
            return finalize_state, common_effects + finalize_effects

        case (
            Authenticating(stage=AuthStage.SASL),
            SASLChallengeReceived(challenge=challenge),
        ):
            return state, [StepSASLHandshake(challenge=challenge)]

        case (
            Authenticating(
                stage=AuthStage.SASL,
                host=host,
                port=port,
                session_id=session_id,
                read_timeout=read_timeout,
                connect_timeout=connect_timeout,
                read_only=read_only,
                pending_auth=pending_auth,
            ),
            SASLCompleted(),
        ):
            if pending_auth:
                first_scheme, first_cred = pending_auth[0]
                return (
                    replace(
                        state,
                        stage=AuthStage.INITIAL_AUTH,
                        pending_auth=pending_auth[1:],
                    ),
                    [
                        SendAuthPacket(
                            scheme=first_scheme, credential=first_cred
                        )
                    ],
                )
            return _finalize_connected(
                host=host,
                port=port,
                session_id=session_id,
                read_timeout=read_timeout,
                connect_timeout=connect_timeout,
                read_only=read_only,
            )

        case (
            Authenticating(
                stage=AuthStage.INITIAL_AUTH,
                host=host,
                port=port,
                session_id=session_id,
                read_timeout=read_timeout,
                connect_timeout=connect_timeout,
                read_only=read_only,
                pending_auth=pending_auth,
            ),
            AuthPacketCompleted(),
        ):
            if pending_auth:
                first_scheme, first_cred = pending_auth[0]
                return (
                    replace(state, pending_auth=pending_auth[1:]),
                    [
                        SendAuthPacket(
                            scheme=first_scheme, credential=first_cred
                        )
                    ],
                )
            return _finalize_connected(
                host=host,
                port=port,
                session_id=session_id,
                read_timeout=read_timeout,
                connect_timeout=connect_timeout,
                read_only=read_only,
            )

        case (
            Connected() | ConnectedReadOnly(),
            AuthPacketCompleted(),
        ):
            return state, []

        # -----------------------------------------------------------------
        # 5. Teardown, Failure, & Reconnection Events
        # -----------------------------------------------------------------
        case (AuthFailed(), AuthFailedEvent()):
            return state, []

        case (_, AuthFailedEvent(error=error)):
            auth_exc = _build_auth_exception(error)
            auth_failed_effects: list[SideEffect] = [
                LogStateTransition(
                    logging.INFO,
                    "Zookeeper session closed, state: %s",
                    (KeeperState.AUTH_FAILED,),
                ),
                ResolveConnectResult(success=False, exception=auth_exc),
                EmitKazooStateChange(KazooState.LOST),
                FailPendingRequests(KeeperState.AUTH_FAILED),
                ResetSessionData(),
                StopConnectionLoop(),
            ]
            return AuthFailed(error=error), auth_failed_effects

        case (ExpiredSession(), SessionExpiredEvent()):
            return state, []

        case (_, SessionExpiredEvent(session_id=session_id)):
            session_expired_effects: list[SideEffect] = [
                LogStateTransition(
                    logging.INFO,
                    "Zookeeper session closed, state: %s",
                    (KeeperState.EXPIRED_SESSION,),
                ),
                EmitKazooStateChange(KazooState.LOST),
                FailPendingRequests(KeeperState.EXPIRED_SESSION),
                ResetSessionData(),
            ]
            return (
                ExpiredSession(session_id=session_id),
                session_expired_effects,
            )

        case (
            Connected(is_connecting_allowed=False)
            | ConnectedReadOnly(is_connecting_allowed=False),
            CloseRequested(),
        ):
            return state, []

        case (
            Connected() | ConnectedReadOnly(),
            CloseRequested(),
        ):
            return (
                replace(state, is_connecting_allowed=False),
                [_ENQUEUE_CLOSE_REQUEST],
            )

        case (_, SessionClosedEvent() | CloseRequested()):
            closed_effects: list[SideEffect] = [
                LogStateTransition(
                    logging.INFO,
                    "Zookeeper session closed, state: %s",
                    (KeeperState.CLOSED,),
                ),
                ResolveConnectResult(
                    success=False,
                    exception=ConnectionClosedError("Connection closed"),
                ),
                EmitKazooStateChange(KazooState.LOST),
                FailPendingRequests(KeeperState.CLOSED),
                ResetSessionData(),
                StopConnectionLoop(),
            ]
            return Inactive(), closed_effects

        case (
            Connected(
                host=host,
                port=port,
                session_id=session_id,
                is_connecting_allowed=allowing,
            )
            | ConnectedReadOnly(
                host=host,
                port=port,
                session_id=session_id,
                is_connecting_allowed=allowing,
            ),
            ConnectionLostEvent(keeper_state=k_state),
        ):
            next_state = Suspended(
                host=host,
                port=port,
                session_id=session_id,
                attempt=1,
                keeper_state=k_state,
                is_connecting_allowed=allowing,
            )
            return next_state, _suspend_effects(k_state)

        case (
            Authenticating(stage=AuthStage.SASL),
            ConnectionLostEvent(keeper_state=k_state, reason=reason),
        ):
            return _reconnect_after_drop(state, k_state), [
                LogStateTransition(
                    logging.WARNING,
                    "Connection dropped in SASL: %s",
                    (reason,),
                )
            ]

        case (
            Authenticating() | Handshaking(),
            ConnectionLostEvent(keeper_state=k_state),
        ):
            return _reconnect_after_drop(state, k_state), []

        case (
            Connected(session_id=session_id)
            | ConnectedReadOnly(session_id=session_id),
            RWServerFoundEvent(server=server),
        ):
            rw_host = server[0] if server else None
            rw_port = server[1] if server else None
            next_state = Suspended(
                host=rw_host,
                port=rw_port,
                session_id=session_id,
                attempt=1,
            )
            return next_state, _suspend_effects(KeeperState.CONNECTING)

        case (
            Connecting()
            | Handshaking()
            | Authenticating()
            | Suspended()
            | ExpiredSession()
            | AuthFailed(),
            RWServerFoundEvent(),
        ):
            return state, []

        case (
            ExpiredSession() | AuthFailed(),
            ConnectionLostEvent(keeper_state=KeeperState.CONNECTING),
        ):
            return state, [
                LogStateTransition(BLATHER, "Skipping state change")
            ]

        case (
            Connecting() | Suspended(),
            ConnectionLostEvent(keeper_state=k_state),
        ) if state.keeper_state == k_state:
            return state, []

        case (_, ConnectionLostEvent(keeper_state=k_state)):
            next_state = Suspended(keeper_state=k_state)
            return next_state, _suspend_effects(
                k_state,
                emit_kazoo_change=(state.kazoo_state != KazooState.SUSPENDED),
            )

        # Illegal state transition
        case _:
            raise InvalidStateTransitionError(state, event)


# =====================================================================
# 5. Serialized State Machine Driver & Compatibility Helpers
# =====================================================================


class StateMachineDriver:
    """Thread/Greenlet-safe serialized event driver for ClientState."""

    def __init__(
        self,
        handler: IHandler,
        effect_executor: Callable[[SideEffect], None],
        initial_state: ClientState | None = None,
    ) -> None:
        self.state: ClientState = initial_state or Inactive()
        self._effect_executor = effect_executor
        self._event_queue: deque[ClientEvent] = deque()
        self._dispatch_lock = handler.rlock_object()
        self._processing_events = False

    def send_event(self, event: ClientEvent) -> None:
        """Process transitions sequentially with an inline fast-path.

        The machine state is updated *before* the transition's effects execute,
        so an effect that inspects self.state sees the post-transition state.
        Holding an RLock across the outermost dispatch ensures cross-thread
        callers wait synchronously rather than returning early or having their
        events cleared on exception, while same-thread re-entrant calls queue
        onto ``_event_queue``. If an effect raises, the remaining effects of
        that transition and any same-thread re-entrant events are discarded;
        the caller is expected to drop the connection.
        """
        with self._dispatch_lock:
            if self._processing_events:
                self._event_queue.append(event)
                return
            self._processing_events = True

            current_event: ClientEvent | None = event
            try:
                while current_event is not None:
                    next_state, effects = transition(self.state, current_event)
                    self.state = next_state
                    for effect in effects:
                        self._effect_executor(effect)

                    if self._event_queue:
                        current_event = self._event_queue.popleft()
                    else:
                        current_event = None
            finally:
                if current_event is not None:
                    self._event_queue.clear()
                self._processing_events = False

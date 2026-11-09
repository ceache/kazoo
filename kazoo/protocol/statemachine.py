"""Pure Functional Dataclass State Machine for KazooClient."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
import logging
import selectors
from typing import Any, Literal, TYPE_CHECKING, TypeAlias

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
    wakeup_interested: bool = False
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
    wakeup_interested: bool = False
    is_terminal: bool = False
    is_connected: bool = False
    is_connecting_allowed: bool = True


@dataclass(frozen=True, slots=True)
class Handshaking:
    """Socket is connected; ZooKeeper Connect request in-flight."""

    host: str | None = None
    port: int | None = None
    session_id: int | None = None
    read_only: bool = False
    attempt: int = 0
    has_sasl: bool = False
    auth_data: tuple[tuple[str, str], ...] = ()
    num_hosts: int = 1
    keeper_state: KeeperState = KeeperState.CONNECTING
    kazoo_state: KazooState = KazooState.LOST
    wakeup_interested: bool = False
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
    wakeup_interested: bool = False
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
    wakeup_interested: bool = True
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
    wakeup_interested: bool = True
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
    wakeup_interested: bool = False
    is_terminal: bool = False
    is_connected: bool = False
    is_connecting_allowed: bool = True


@dataclass(frozen=True, slots=True)
class ExpiredSession:
    """Session was invalidated by the ZooKeeper ensemble."""

    session_id: int | None = None
    keeper_state: KeeperState = KeeperState.EXPIRED_SESSION
    kazoo_state: KazooState = KazooState.LOST
    wakeup_interested: bool = False
    is_terminal: bool = False
    is_connected: bool = False
    is_connecting_allowed: bool = True


@dataclass(frozen=True, slots=True)
class AuthFailed:
    """Authentication (SASL or Auth packet) permanently failed."""

    error: Exception | None = None
    keeper_state: KeeperState = KeeperState.AUTH_FAILED
    kazoo_state: KazooState = KazooState.LOST
    wakeup_interested: bool = False
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

    host: str | None = None
    port: int | None = None


@dataclass(frozen=True, slots=True)
class HandshakeCompleted:
    """Emitted when the ZooKeeper Connect handshake response succeeds."""

    host: str | None = None
    port: int | None = None
    session_id: int | None = None
    read_timeout: float = 10.0
    connect_timeout: float = 10.0
    read_only: bool = False
    has_sasl: bool = False
    auth_data: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class SocketConnected:
    """Emitted when transport socket connection is established."""

    host: str = "127.0.0.1"
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


@dataclass(frozen=True, slots=True)
class UpdateSendCapacity:
    """Emitted before polling to update wakeup pipe interest."""

    can_send: bool


ClientEvent: TypeAlias = (
    StartRequested
    | ConnectAttemptStarted
    | HandshakeCompleted
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
    | UpdateSendCapacity
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
class UpdateIOInterest:
    """Update the selector interest mask for a registered file object."""

    interest_mask: int
    target: Literal["zk_socket", "wakeup"] = "wakeup"


@dataclass(frozen=True, slots=True)
class DrainWakeupPipe:
    """Disable wakeup pipe selector interest and drain stray wakeup bytes."""


@dataclass(frozen=True, slots=True)
class StartSASLHandshake:
    """Initialize SASLExchange and send the first SASL token."""

    host: str | None = None


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
class SendQueuedRequest:
    """Dequeue and transmit the next client request over the socket."""


@dataclass(frozen=True, slots=True)
class SendHeartbeatPing:
    """Send a heartbeat Ping request (and check RW server if in RO mode)."""


@dataclass(frozen=True, slots=True)
class TriggerConnectionDrop:
    """Raise ConnectionDropped with the given reason."""

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


SideEffect: TypeAlias = (
    LogStateTransition
    | ResolveConnectResult
    | EmitKazooStateChange
    | FailPendingRequests
    | InvalidateWatchers
    | ResetSessionData
    | UpdateIOInterest
    | DrainWakeupPipe
    | StartSASLHandshake
    | StepSASLHandshake
    | SendAuthPacket
    | ReadSocketPacket
    | SendQueuedRequest
    | SendHeartbeatPing
    | TriggerConnectionDrop
    | SessionEstablished
    | StopConnectionLoop
    | SendConnectPacket
    | ReadConnectResponsePacket
    | UpdateSessionData
    | ConfigureTimeouts
    | ConfigureReadOnlyMode
)

# Pre-allocated hot-path event and effect singletons to avoid per-poll
# heap allocations in the steady-state I/O loop.
SOCKET_READABLE = SocketReadable()
WAKEUP_SIGNALED = WakeUpSignaled()
POLL_TIMEOUT_EXPIRED = PollTimeoutExpired()
HEARTBEAT_DEADLINE_REACHED = HeartbeatDeadlineReached()
PING_RECEIVED = PingReceived()
UPDATE_SEND_CAPACITY_TRUE = UpdateSendCapacity(can_send=True)
UPDATE_SEND_CAPACITY_FALSE = UpdateSendCapacity(can_send=False)

_READ_SOCKET_PACKET = ReadSocketPacket()
_READ_CONNECT_RESPONSE_PACKET = ReadConnectResponsePacket()
_SEND_QUEUED_REQUEST = SendQueuedRequest()
_DRAIN_WAKEUP_PIPE = DrainWakeupPipe()
_SEND_HEARTBEAT_PING = SendHeartbeatPing()
_WAKEUP_INTEREST_READ = UpdateIOInterest(
    interest_mask=selectors.EVENT_READ, target="wakeup"
)
_WAKEUP_INTEREST_NONE = UpdateIOInterest(interest_mask=0, target="wakeup")

_EFFECTS_READ_SOCKET: list[SideEffect] = [_READ_SOCKET_PACKET]
_EFFECTS_READ_CONNECT: list[SideEffect] = [_READ_CONNECT_RESPONSE_PACKET]
_EFFECTS_SEND_REQUEST: list[SideEffect] = [_SEND_QUEUED_REQUEST]
_EFFECTS_DRAIN_WAKEUP: list[SideEffect] = [_DRAIN_WAKEUP_PIPE]
_EFFECTS_SEND_PING: list[SideEffect] = [_SEND_HEARTBEAT_PING]
_EFFECTS_ENABLE_WAKEUP: list[SideEffect] = [_WAKEUP_INTEREST_READ]
_EFFECTS_DISABLE_WAKEUP: list[SideEffect] = [_WAKEUP_INTEREST_NONE]
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
    disable_wakeup: bool = False,
    emit_kazoo_change: bool = True,
) -> list[SideEffect]:
    effects: list[SideEffect] = []
    if disable_wakeup:
        effects.append(UpdateIOInterest(interest_mask=0, target="wakeup"))
    effects.extend(
        [
            LogStateTransition(logging.INFO, "Zookeeper connection lost"),
            FailPendingRequests(keeper_state),
        ]
    )
    if emit_kazoo_change:
        effects.append(EmitKazooStateChange(KazooState.SUSPENDED))
    effects.append(InvalidateWatchers())
    return effects


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
        UpdateIOInterest(interest_mask=selectors.EVENT_READ, target="wakeup"),
        LogStateTransition(
            logging.INFO,
            "Zookeeper connection established, state: %s",
            (next_state.keeper_state,),
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
        case (
            Connected()
            | ConnectedReadOnly()
            | Authenticating()
            | AuthFailed()
            | ExpiredSession(),
            SocketReadable(),
        ):
            return state, _EFFECTS_READ_SOCKET

        case (Handshaking(), SocketReadable()):
            return state, _EFFECTS_READ_CONNECT

        case (
            Inactive() | Connecting() | Suspended(),
            SocketReadable(),
        ):
            return state, _NO_EFFECTS

        case (
            Connected()
            | ConnectedReadOnly()
            | AuthFailed()
            | ExpiredSession(),
            WakeUpSignaled(),
        ):
            return state, _EFFECTS_SEND_REQUEST

        case (
            Inactive()
            | Connecting()
            | Handshaking()
            | Authenticating()
            | Suspended(),
            WakeUpSignaled(),
        ):
            return state, _EFFECTS_DRAIN_WAKEUP

        case (
            Connected(wakeup_interested=wakeup_interested)
            | ConnectedReadOnly(wakeup_interested=wakeup_interested),
            UpdateSendCapacity(can_send=can_send),
        ):
            if can_send == wakeup_interested:
                return state, _NO_EFFECTS
            return (
                replace(state, wakeup_interested=can_send),
                (
                    _EFFECTS_ENABLE_WAKEUP
                    if can_send
                    else _EFFECTS_DISABLE_WAKEUP
                ),
            )

        case (
            Inactive()
            | Connecting()
            | Handshaking()
            | Authenticating()
            | Suspended()
            | ExpiredSession()
            | AuthFailed(),
            UpdateSendCapacity(),
        ):
            return state, _NO_EFFECTS

        case (
            Connected() | ConnectedReadOnly(),
            PingReceived(),
        ):
            if not state.ping_outstanding:
                return state, _NO_EFFECTS
            return replace(state, ping_outstanding=False), _NO_EFFECTS

        case (
            Inactive() | Suspended() | ExpiredSession() | AuthFailed(),
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
                replace(state, ping_outstanding=False),
                [
                    TriggerConnectionDrop(
                        reason="outstanding heartbeat ping not received"
                    )
                ],
            )

        case (
            Connected() | ConnectedReadOnly(),
            PollTimeoutExpired(),
        ):
            return state, []

        case (
            Handshaking(),
            PollTimeoutExpired(),
        ):
            return (
                state,
                [
                    TriggerConnectionDrop(
                        reason="socket time-out during handshake"
                    )
                ],
            )

        case (
            Authenticating(),
            PollTimeoutExpired(),
        ):
            return (
                state,
                [
                    TriggerConnectionDrop(
                        reason="socket time-out during authentication"
                    )
                ],
            )

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
                wakeup_interested=wakeup_interested,
                is_connecting_allowed=allowing,
            )
            | ConnectedReadOnly(
                session_id=session_id,
                wakeup_interested=wakeup_interested,
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
            return next_state, _suspend_effects(
                KeeperState.CONNECTING,
                disable_wakeup=wakeup_interested,
            )

        case (
            Authenticating(
                session_id=session_id,
                kazoo_state=kazoo_state,
            )
            | Handshaking(
                session_id=session_id,
                kazoo_state=kazoo_state,
            ),
            ConnectAttemptStarted(host=host, port=port),
        ):
            if kazoo_state == KazooState.SUSPENDED:
                return (
                    Suspended(
                        host=host,
                        port=port,
                        session_id=session_id,
                        attempt=1,
                    ),
                    [],
                )
            return (
                Connecting(host=host, port=port, attempt=1),
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
                attempt=getattr(state, "attempt", 1) or 1,
                has_sasl=has_sasl,
                auth_data=auth_data,
                num_hosts=num_hosts,
                kazoo_state=state.kazoo_state,
                wakeup_interested=False,
            )
            return next_state, [
                SendConnectPacket(
                    connect=connect_req, timeout=connect_timeout
                ),
            ]

        case (
            state,
            ConnectResponseReceived(),
        ) if not state.is_connecting_allowed:
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
                ]
                if state.kazoo_state != KazooState.LOST:
                    expired_connect_effects.append(
                        EmitKazooStateChange(KazooState.LOST)
                    )
                expired_connect_effects.extend(
                    [
                        FailPendingRequests(KeeperState.EXPIRED_SESSION),
                        ResetSessionData(),
                        TriggerConnectionDrop("Session has expired"),
                    ]
                )
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
                        wakeup_interested=False,
                    ),
                    common_effects
                    + [
                        UpdateIOInterest(interest_mask=0, target="wakeup"),
                        StartSASLHandshake(host=host or "127.0.0.1"),
                    ],
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
                        wakeup_interested=False,
                    ),
                    common_effects
                    + [
                        UpdateIOInterest(interest_mask=0, target="wakeup"),
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

        # -----------------------------------------------------------------
        # 4. HandshakeCompleted & Authentication Progression
        # -----------------------------------------------------------------
        case (
            Connected(),
            HandshakeCompleted(
                host=None,
                session_id=None,
                read_only=False,
                has_sasl=False,
                auth_data=(),
            ),
        ):
            return state, []

        case (
            ConnectedReadOnly(),
            HandshakeCompleted(
                host=None,
                session_id=None,
                read_only=True,
                has_sasl=False,
                auth_data=(),
            ),
        ):
            return state, []

        case (
            _,
            HandshakeCompleted(
                host=host,
                port=port,
                session_id=hs_session_id,
                read_timeout=read_timeout,
                connect_timeout=connect_timeout,
                read_only=read_only,
                has_sasl=has_sasl,
                auth_data=auth_data,
            ),
        ):
            eff_host = host or getattr(state, "host", None)
            eff_port = port or getattr(state, "port", None)
            hs_sid: int | None = (
                hs_session_id
                if hs_session_id is not None
                else getattr(state, "session_id", None)
            )
            if has_sasl:
                return (
                    Authenticating(
                        stage=AuthStage.SASL,
                        host=eff_host,
                        port=eff_port,
                        session_id=hs_sid,
                        read_timeout=read_timeout,
                        connect_timeout=connect_timeout,
                        read_only=read_only,
                        pending_auth=auth_data,
                        kazoo_state=state.kazoo_state,
                        wakeup_interested=False,
                    ),
                    [
                        UpdateIOInterest(interest_mask=0, target="wakeup"),
                        StartSASLHandshake(host=eff_host),
                    ],
                )
            if auth_data:
                first_scheme, first_cred = auth_data[0]
                return (
                    Authenticating(
                        stage=AuthStage.INITIAL_AUTH,
                        host=eff_host,
                        port=eff_port,
                        session_id=hs_sid,
                        read_timeout=read_timeout,
                        connect_timeout=connect_timeout,
                        read_only=read_only,
                        pending_auth=auth_data[1:],
                        kazoo_state=state.kazoo_state,
                        wakeup_interested=False,
                    ),
                    [
                        UpdateIOInterest(interest_mask=0, target="wakeup"),
                        SendAuthPacket(
                            scheme=first_scheme, credential=first_cred
                        ),
                    ],
                )
            return _finalize_connected(
                host=eff_host,
                port=eff_port,
                session_id=hs_sid,
                read_timeout=read_timeout,
                connect_timeout=connect_timeout,
                read_only=read_only,
            )

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
            Connected() | ConnectedReadOnly() | Inactive(),
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
            ]
            if state.kazoo_state != KazooState.LOST:
                auth_failed_effects.append(
                    EmitKazooStateChange(KazooState.LOST)
                )
            auth_failed_effects.extend(
                [
                    FailPendingRequests(KeeperState.AUTH_FAILED),
                    ResetSessionData(),
                    StopConnectionLoop(),
                ]
            )
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
            ]
            if state.kazoo_state != KazooState.LOST:
                session_expired_effects.append(
                    EmitKazooStateChange(KazooState.LOST)
                )
            session_expired_effects.extend(
                [
                    FailPendingRequests(KeeperState.EXPIRED_SESSION),
                    ResetSessionData(),
                ]
            )
            return (
                ExpiredSession(session_id=session_id),
                session_expired_effects,
            )

        case (
            Connected(wakeup_interested=wakeup_interested)
            | ConnectedReadOnly(wakeup_interested=wakeup_interested),
            CloseRequested(),
        ):
            close_req_effects: list[SideEffect] = []
            if not wakeup_interested:
                close_req_effects.append(
                    UpdateIOInterest(
                        interest_mask=selectors.EVENT_READ, target="wakeup"
                    )
                )
            return (
                replace(
                    state,
                    is_connecting_allowed=False,
                    wakeup_interested=True,
                ),
                close_req_effects,
            )

        case (Inactive(), SessionClosedEvent() | CloseRequested()):
            return state, []

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
            ]
            if state.kazoo_state != KazooState.LOST:
                closed_effects.append(EmitKazooStateChange(KazooState.LOST))
            closed_effects.extend(
                [
                    FailPendingRequests(KeeperState.CLOSED),
                    ResetSessionData(),
                    StopConnectionLoop(),
                ]
            )
            return Inactive(), closed_effects

        case (
            Connected(
                host=host,
                port=port,
                session_id=session_id,
                wakeup_interested=wakeup_interested,
                is_connecting_allowed=allowing,
            )
            | ConnectedReadOnly(
                host=host,
                port=port,
                session_id=session_id,
                wakeup_interested=wakeup_interested,
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
            return next_state, _suspend_effects(
                k_state, disable_wakeup=wakeup_interested
            )

        case (
            Authenticating(stage=AuthStage.SASL),
            ConnectionLostEvent(reason=reason),
        ):
            auth_exc = AuthFailedError("Connection dropped in SASL")
            sasl_lost_effects: list[SideEffect] = [
                LogStateTransition(
                    logging.WARNING,
                    "AUTH_FAILED closing (connection dropped in SASL): %s",
                    (reason,),
                ),
                ResolveConnectResult(success=False, exception=auth_exc),
            ]
            if state.kazoo_state != KazooState.LOST:
                sasl_lost_effects.append(EmitKazooStateChange(KazooState.LOST))
            sasl_lost_effects.extend(
                [
                    FailPendingRequests(KeeperState.AUTH_FAILED),
                    ResetSessionData(),
                    StopConnectionLoop(),
                ]
            )
            return AuthFailed(error=auth_exc), sasl_lost_effects

        case (
            Authenticating(
                stage=AuthStage.INITIAL_AUTH,
                host=host,
                port=port,
                session_id=session_id,
                kazoo_state=kazoo_state,
            )
            | Handshaking(
                host=host,
                port=port,
                session_id=session_id,
                kazoo_state=kazoo_state,
            ),
            ConnectionLostEvent(keeper_state=k_state),
        ):
            if kazoo_state == KazooState.SUSPENDED:
                return (
                    Suspended(
                        host=host,
                        port=port,
                        session_id=session_id,
                        attempt=1,
                        keeper_state=k_state,
                    ),
                    [],
                )
            return Connecting(host=host, port=port, attempt=1), []

        case (
            Connected(
                session_id=session_id,
                wakeup_interested=wakeup_interested,
            )
            | ConnectedReadOnly(
                session_id=session_id,
                wakeup_interested=wakeup_interested,
            ),
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
            return next_state, _suspend_effects(
                KeeperState.CONNECTING, disable_wakeup=wakeup_interested
            )

        case (
            Inactive()
            | Connecting()
            | Handshaking()
            | Authenticating()
            | Suspended()
            | ExpiredSession()
            | AuthFailed(),
            RWServerFoundEvent(),
        ):
            return state, []

        case (
            Inactive() | ExpiredSession() | AuthFailed(),
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
        self._dispatch_lock = handler.lock_object()
        self._processing_events = False

    def send_event(self, event: ClientEvent) -> None:
        """Process transitions sequentially with an inline fast-path."""
        with self._dispatch_lock:
            if self._processing_events:
                self._event_queue.append(event)
                return
            self._processing_events = True

        try:
            current_event: ClientEvent | None = event
            while current_event is not None:
                next_state, effects = transition(self.state, current_event)
                self.state = next_state
                for effect in effects:
                    self._effect_executor(effect)

                with self._dispatch_lock:
                    if self._event_queue:
                        current_event = self._event_queue.popleft()
                    else:
                        self._processing_events = False
                        current_event = None
        except BaseException:
            # If an effect raised (e.g. TriggerConnectionDrop), drain any
            # lifecycle events (such as SessionClosedEvent) queued while
            # the effect was executing before releasing _processing_events.
            try:
                while True:
                    with self._dispatch_lock:
                        if not self._event_queue:
                            break
                        queued_event = self._event_queue.popleft()
                    next_state, effects = transition(self.state, queued_event)
                    self.state = next_state
                    for effect in effects:
                        self._effect_executor(effect)
            finally:
                with self._dispatch_lock:
                    self._processing_events = False
            raise

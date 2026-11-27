from __future__ import annotations

import logging
import socket
import types
from dataclasses import FrozenInstanceError
from typing import Any, get_args
from unittest.mock import MagicMock

import pytest

from kazoo.client import KazooClient
from kazoo.exceptions import (
    AuthFailedError,
    ConfigurationError,
    ConnectionClosedError,
    ConnectionDropped,
    InvalidStateTransitionError,
    SASLException,
)
from kazoo.handlers.threading import SequentialThreadingHandler
from kazoo.loggingsupport import BLATHER
from kazoo.protocol import sasl as sasl_mod
from kazoo.protocol.connection import (
    CHANGED_EVENT,
    CHILD_EVENT,
    DELETED_EVENT,
    FrameDecoder,
    RequestManager,
    WatcherManager,
)
from kazoo.protocol.sasl import SASLConfig, SASLExchange
from kazoo.protocol.serialization import (
    SASL,
    AddWatch,
    Auth,
    AuthResponse,
    Connect,
    GetChildren,
    GetData,
    PingResponse,
    RemoveWatches,
    ReplyHeader,
    ReservedXID,
    ResponsePacket,
    SASLResponse,
    _write_buffer,
    int_int_long_struct,
    int_struct,
    reply_header_struct,
)
from kazoo.protocol.statemachine import (
    Authenticating,
    AuthFailed,
    AuthFailedEvent,
    AuthPacketCompleted,
    AuthStage,
    ClientEvent,
    ClientState,
    CloseRequested,
    CloseSocket,
    ConfigureReadOnlyMode,
    ConfigureTimeouts,
    ConnectAttemptStarted,
    Connected,
    ConnectedReadOnly,
    Connecting,
    ConnectionLostEvent,
    ConnectResponseReceived,
    DrainWakeupPipe,
    EmitKazooStateChange,
    EnqueueCloseRequest,
    ExpiredSession,
    FailPendingRequests,
    Handshaking,
    HeartbeatDeadlineReached,
    Inactive,
    InvalidateWatchers,
    LogStateTransition,
    PingReceived,
    PollTimeoutExpired,
    ReadConnectResponsePacket,
    ReadSocketPacket,
    ResetSessionData,
    ResolveConnectResult,
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
    SocketReadable,
    StartRequested,
    StartSASLHandshake,
    StateMachineDriver,
    StepSASLHandshake,
    StopConnectionLoop,
    Suspended,
    UpdateSessionData,
    WakeUpSignaled,
    transition,
)
from kazoo.protocol.states import (
    AddWatchMode,
    KazooState,
    KeeperState,
    WatcherType,
)


class TestDataclassImmutability:
    def test_states_are_frozen(self) -> None:
        states = [
            Inactive(),
            Connecting(host="127.0.0.1", port=2181),
            Handshaking(host="127.0.0.1", port=2181, session_id=123),
            Authenticating(
                stage=AuthStage.SASL,
                host="127.0.0.1",
                port=2181,
                session_id=123,
            ),
            Authenticating(
                stage=AuthStage.INITIAL_AUTH,
                host="127.0.0.1",
                port=2181,
                session_id=123,
            ),
            Connected(host="127.0.0.1", port=2181, session_id=123),
            ConnectedReadOnly(host="127.0.0.1", port=2181, session_id=123),
            Suspended(host="127.0.0.1", port=2181, session_id=123),
            ExpiredSession(session_id=123),
            AuthFailed(),
        ]
        for state in states:
            with pytest.raises(FrozenInstanceError):
                state.keeper_state = KeeperState.CLOSED  # type: ignore[misc]

    def test_events_and_effects_are_frozen(self) -> None:
        ev = ConnectAttemptStarted(host="127.0.0.1", port=2181)
        with pytest.raises(FrozenInstanceError):
            ev.host = "other"  # type: ignore[misc]

        eff = EmitKazooStateChange(KazooState.CONNECTED)
        with pytest.raises(FrozenInstanceError):
            eff.new_state = KazooState.LOST  # type: ignore[misc]


class TestPureTransitionMatrix:
    def test_inactive_ignores_connect_attempt_without_start_requested(
        self,
    ) -> None:
        state = Inactive()
        next_state, effects = transition(
            state, ConnectAttemptStarted(host="127.0.0.1", port=2181)
        )
        assert next_state == Inactive()
        assert effects == ()

    def test_start_requested_then_connect_attempt_started(self) -> None:
        state = Inactive()
        s1, eff1 = transition(state, StartRequested())
        assert s1 == Connecting(attempt=0)
        assert eff1 == ()

        s2, eff2 = transition(
            s1, ConnectAttemptStarted(host="127.0.0.1", port=2181)
        )
        assert s2 == Connecting(host="127.0.0.1", port=2181, attempt=1)
        assert eff2 == (LogStateTransition(BLATHER, "Skipping state change"),)

    def test_connecting_increments_attempt_without_effects(self) -> None:
        state = Connecting(host="10.0.0.1", port=2181, attempt=1)
        next_state, effects = transition(
            state, ConnectAttemptStarted(host="10.0.0.2", port=2181)
        )
        assert next_state == Connecting(host="10.0.0.2", port=2181, attempt=2)
        assert effects == ()

    def test_socket_connected_transitions_to_handshaking_and_sends_packet(
        self,
    ) -> None:
        state = Connecting(host="127.0.0.1", port=2181, attempt=1)
        next_state, effects = transition(
            state,
            SocketConnected(
                host="127.0.0.1",
                port=2181,
                num_hosts=2,
                session_id=123,
                session_passwd=b"pass",
                session_timeout=10000,
                last_zxid=50,
                read_only=False,
                has_sasl=False,
                auth_data=(),
            ),
        )
        assert next_state == Handshaking(
            host="127.0.0.1",
            port=2181,
            session_id=123,
            read_only=False,
            attempt=1,
            has_sasl=False,
            auth_data=(),
            num_hosts=2,
            kazoo_state=KazooState.LOST,
        )
        expected_connect = Connect(0, 50, 10000, 123, b"pass", False)
        assert effects == (
            SendConnectPacket(connect=expected_connect, timeout=5.0),
        )

    def test_handshaking_readable_yields_read_connect_response_packet(
        self,
    ) -> None:
        state = Handshaking(host="127.0.0.1", port=2181)
        next_state, effects = transition(state, SocketReadable())
        assert next_state == state
        assert effects == (ReadConnectResponsePacket(),)

    def test_handshaking_connect_response_success_transitions_to_connected(
        self,
    ) -> None:
        state = Handshaking(
            host="127.0.0.1",
            port=2181,
            num_hosts=2,
        )
        connect_res = Connect(
            protocol_version=0,
            last_zxid_seen=0,
            time_out=30000,
            session_id=999,
            passwd=b"newpass",
            read_only=False,
        )
        next_state, effects = transition(
            state, ConnectResponseReceived(connect_result=connect_res)
        )
        assert next_state == Connected(
            host="127.0.0.1",
            port=2181,
            session_id=999,
            read_timeout=20.0,
            connect_timeout=15.0,
        )
        assert effects == (
            UpdateSessionData(
                session_id=999,
                session_passwd=b"newpass",
                protocol_version=0,
            ),
            ConfigureTimeouts(
                read_timeout=20.0,
                connect_timeout=15.0,
            ),
            ConfigureReadOnlyMode(read_only=False),
            LogStateTransition(
                logging.INFO,
                "Zookeeper connection established, state: %s",
                (KeeperState.CONNECTED,),
            ),
            LogStateTransition(
                BLATHER,
                "Session created, session_id: %r\n"
                "    connect timeout: %ss\n"
                "    read timeout: %ss",
                (999, 15.0, 20.0),
            ),
            ResolveConnectResult(success=True),
            EmitKazooStateChange(KazooState.CONNECTED),
            SessionEstablished(),
        )

    def test_handshaking_connect_response_expired(self) -> None:
        state = Handshaking(
            host="127.0.0.1",
            port=2181,
            num_hosts=1,
            kazoo_state=KazooState.SUSPENDED,
        )
        connect_res = Connect(
            protocol_version=0,
            last_zxid_seen=0,
            time_out=0,
            session_id=999,
            passwd=b"",
            read_only=False,
        )
        next_state, effects = transition(
            state, ConnectResponseReceived(connect_result=connect_res)
        )
        assert next_state == ExpiredSession(session_id=999)
        assert effects == (
            LogStateTransition(
                logging.WARNING,
                "Zookeeper session closed, state: %s",
                (KeeperState.EXPIRED_SESSION,),
            ),
            EmitKazooStateChange(KazooState.LOST),
            FailPendingRequests(KeeperState.EXPIRED_SESSION),
            ResetSessionData(),
            CloseSocket("Session has expired"),
        )

    def test_handshaking_connect_response_expired_initial_connect(
        self,
    ) -> None:
        state = Handshaking(
            host="127.0.0.1",
            port=2181,
            num_hosts=1,
            kazoo_state=KazooState.LOST,
        )
        connect_res = Connect(
            protocol_version=0,
            last_zxid_seen=0,
            time_out=0,
            session_id=999,
            passwd=b"",
            read_only=False,
        )
        next_state, effects = transition(
            state, ConnectResponseReceived(connect_result=connect_res)
        )
        assert next_state == ExpiredSession(session_id=999)
        assert effects == (
            LogStateTransition(
                logging.WARNING,
                "Zookeeper session closed, state: %s",
                (KeeperState.EXPIRED_SESSION,),
            ),
            EmitKazooStateChange(KazooState.LOST),
            FailPendingRequests(KeeperState.EXPIRED_SESSION),
            ResetSessionData(),
            CloseSocket("Session has expired"),
        )

    def test_handshaking_connect_response_with_sasl(self) -> None:
        state = Handshaking(
            host="127.0.0.1",
            port=2181,
            num_hosts=1,
            has_sasl=True,
            auth_data=(("digest", "u:p"),),
        )
        connect_res = Connect(
            protocol_version=0,
            last_zxid_seen=0,
            time_out=15000,
            session_id=555,
            passwd=b"pwd",
            read_only=False,
        )
        next_state, effects = transition(
            state, ConnectResponseReceived(connect_result=connect_res)
        )
        assert next_state == Authenticating(
            stage=AuthStage.SASL,
            host="127.0.0.1",
            port=2181,
            session_id=555,
            read_timeout=10.0,
            connect_timeout=15.0,
            read_only=False,
            pending_auth=(("digest", "u:p"),),
        )
        assert effects == (
            UpdateSessionData(
                session_id=555,
                session_passwd=b"pwd",
                protocol_version=0,
            ),
            ConfigureTimeouts(read_timeout=10.0, connect_timeout=15.0),
            ConfigureReadOnlyMode(read_only=False),
            StartSASLHandshake(host="127.0.0.1"),
        )

    def test_handshaking_connect_response_with_auth(self) -> None:
        state = Handshaking(
            host="127.0.0.1",
            port=2181,
            num_hosts=1,
            has_sasl=False,
            auth_data=(("digest", "u1:p1"), ("digest", "u2:p2")),
        )
        connect_res = Connect(
            protocol_version=0,
            last_zxid_seen=0,
            time_out=15000,
            session_id=555,
            passwd=b"pwd",
            read_only=False,
        )
        next_state, effects = transition(
            state, ConnectResponseReceived(connect_result=connect_res)
        )
        assert next_state == Authenticating(
            stage=AuthStage.INITIAL_AUTH,
            host="127.0.0.1",
            port=2181,
            session_id=555,
            read_timeout=10.0,
            connect_timeout=15.0,
            read_only=False,
            pending_auth=(("digest", "u2:p2"),),
        )
        assert effects == (
            UpdateSessionData(
                session_id=555,
                session_passwd=b"pwd",
                protocol_version=0,
            ),
            ConfigureTimeouts(read_timeout=10.0, connect_timeout=15.0),
            ConfigureReadOnlyMode(read_only=False),
            SendAuthPacket(scheme="digest", credential="u1:p1"),
        )

    def test_handshaking_poll_timeout_drops_connection(self) -> None:
        state = Handshaking(host="127.0.0.1", port=2181)
        next_state, effects = transition(state, PollTimeoutExpired())
        assert next_state == Connecting(
            host="127.0.0.1", port=2181, attempt=1, is_connecting_allowed=True
        )
        assert effects == (
            CloseSocket(reason="socket time-out during handshake"),
            LogStateTransition(
                logging.WARNING,
                "Connection time-out: %s",
                ("socket time-out during handshake",),
            ),
        )

    def test_handshaking_connection_lost_transitions_to_connecting(
        self,
    ) -> None:
        state = Handshaking(
            host="127.0.0.1",
            port=2181,
            session_id=1,
            attempt=1,
            kazoo_state=KazooState.LOST,
        )
        next_state, effects = transition(
            state, ConnectionLostEvent(reason="drop")
        )
        assert next_state == Connecting(host="127.0.0.1", port=2181, attempt=1)
        assert effects == (CloseSocket(reason="drop"),)

    def test_authenticating_sasl_and_auth_packets_progression(self) -> None:
        state = Handshaking(
            host="127.0.0.1",
            port=2181,
            num_hosts=1,
            has_sasl=True,
            auth_data=(("digest", "u1:p1"), ("digest", "u2:p2")),
        )
        connect_res = Connect(
            protocol_version=0,
            last_zxid_seen=0,
            time_out=9000,
            session_id=999,
            passwd=b"pwd",
            read_only=False,
        )
        s1, eff1 = transition(
            state, ConnectResponseReceived(connect_result=connect_res)
        )
        assert s1 == Authenticating(
            stage=AuthStage.SASL,
            host="127.0.0.1",
            port=2181,
            session_id=999,
            read_timeout=6.0,
            connect_timeout=9.0,
            read_only=False,
            pending_auth=(("digest", "u1:p1"), ("digest", "u2:p2")),
        )
        assert eff1 == (
            UpdateSessionData(
                session_id=999,
                session_passwd=b"pwd",
                protocol_version=0,
            ),
            ConfigureTimeouts(read_timeout=6.0, connect_timeout=9.0),
            ConfigureReadOnlyMode(read_only=False),
            StartSASLHandshake(host="127.0.0.1"),
        )

        # Server challenge -> StepSASLHandshake
        s2, eff2 = transition(s1, SASLChallengeReceived(challenge=b"chal1"))
        assert s2 == s1
        assert eff2 == (StepSASLHandshake(challenge=b"chal1"),)

        # SASL completes -> enters INITIAL_AUTH stage and sends first Auth
        s3, eff3 = transition(s2, SASLCompleted())
        assert isinstance(s3, Authenticating)
        assert s3.stage == AuthStage.INITIAL_AUTH
        assert s3.pending_auth == (("digest", "u2:p2"),)
        assert eff3 == (SendAuthPacket(scheme="digest", credential="u1:p1"),)

        # First Auth packet reply arrives -> sends second pending Auth packet
        s4, eff4 = transition(s3, AuthPacketCompleted())
        assert isinstance(s4, Authenticating)
        assert s4.stage == AuthStage.INITIAL_AUTH
        assert s4.pending_auth == ()
        assert eff4 == (SendAuthPacket(scheme="digest", credential="u2:p2"),)

        # Second Auth packet reply arrives -> Connected!
        s5, eff5 = transition(s4, AuthPacketCompleted())
        assert s5 == Connected(
            host="127.0.0.1",
            port=2181,
            session_id=999,
            read_timeout=6.0,
            connect_timeout=9.0,
        )
        assert eff5 == (
            LogStateTransition(
                logging.INFO,
                "Zookeeper connection established, state: %s",
                (KeeperState.CONNECTED,),
            ),
            LogStateTransition(
                BLATHER,
                "Session created, session_id: %r\n"
                "    connect timeout: %ss\n"
                "    read timeout: %ss",
                (999, 9.0, 6.0),
            ),
            ResolveConnectResult(success=True),
            EmitKazooStateChange(KazooState.CONNECTED),
            SessionEstablished(),
        )

    def test_connected_to_suspended_on_connection_lost(
        self,
    ) -> None:
        state = Connected(host="127.0.0.1", port=2181, session_id=42)
        next_state, effects = transition(
            state, ConnectionLostEvent(reason="socket broken")
        )
        assert next_state == Suspended(
            host="127.0.0.1", port=2181, session_id=42, attempt=1
        )
        assert effects == (
            CloseSocket(reason="socket broken"),
            LogStateTransition(logging.INFO, "Zookeeper connection lost"),
            FailPendingRequests(KeeperState.CONNECTING),
            EmitKazooStateChange(KazooState.SUSPENDED),
            InvalidateWatchers(),
        )

    def test_connected_ro_to_suspended_on_rw_server_found(self) -> None:
        state = ConnectedReadOnly(host="127.0.0.1", port=2181, session_id=42)
        next_state, effects = transition(
            state, RWServerFoundEvent(server=("127.0.0.2", 2181))
        )
        assert next_state == Suspended(
            host="127.0.0.2", port=2181, session_id=42, attempt=1
        )
        assert effects == (
            CloseSocket(reason="Found RW server"),
            LogStateTransition(logging.INFO, "Zookeeper connection lost"),
            FailPendingRequests(KeeperState.CONNECTING),
            EmitKazooStateChange(KazooState.SUSPENDED),
            InvalidateWatchers(),
        )

    def test_auth_states_connect_attempt_started(self) -> None:
        for stage in (AuthStage.SASL, AuthStage.INITIAL_AUTH):
            # 1. kazoo_state == LOST -> Connecting
            s_lost = Authenticating(
                stage=stage,
                host="127.0.0.1",
                port=2181,
                session_id=1,
                kazoo_state=KazooState.LOST,
            )
            next_s1, eff1 = transition(
                s_lost, ConnectAttemptStarted(host="127.0.0.2", port=2182)
            )
            assert next_s1 == Connecting(
                host="127.0.0.2", port=2182, attempt=1
            )
            assert eff1 == ()

            # 2. kazoo_state == SUSPENDED -> Suspended
            s_susp = Authenticating(
                stage=stage,
                host="127.0.0.1",
                port=2181,
                session_id=1,
                kazoo_state=KazooState.SUSPENDED,
            )
            next_s2, eff2 = transition(
                s_susp, ConnectAttemptStarted(host="127.0.0.2", port=2182)
            )
            assert next_s2 == Suspended(
                host="127.0.0.2", port=2182, session_id=1, attempt=1
            )
            assert eff2 == ()

    def test_auth_sasl_connection_lost_transitions_to_suspended_when_suspended(
        self,
    ) -> None:
        state = Authenticating(
            stage=AuthStage.SASL,
            host="127.0.0.1",
            port=2181,
            session_id=1,
            kazoo_state=KazooState.SUSPENDED,
        )
        next_state, effects = transition(
            state, ConnectionLostEvent(reason="Connection reset by peer")
        )
        assert next_state == Suspended(
            host="127.0.0.1",
            port=2181,
            session_id=1,
            attempt=1,
            kazoo_state=KazooState.SUSPENDED,
        )
        assert effects == (
            CloseSocket(reason="Connection reset by peer"),
            LogStateTransition(
                logging.WARNING,
                "Connection dropped in SASL: %s",
                ("Connection reset by peer",),
            ),
        )

    def test_auth_sasl_connection_lost_transitions_to_connecting_when_lost(
        self,
    ) -> None:
        state = Authenticating(
            stage=AuthStage.SASL,
            host="127.0.0.1",
            port=2181,
            session_id=1,
            kazoo_state=KazooState.LOST,
        )
        next_state, effects = transition(
            state, ConnectionLostEvent(reason="Connection reset by peer")
        )
        assert next_state == Connecting(
            host="127.0.0.1",
            port=2181,
            attempt=1,
        )
        assert effects == (
            CloseSocket(reason="Connection reset by peer"),
            LogStateTransition(
                logging.WARNING,
                "Connection dropped in SASL: %s",
                ("Connection reset by peer",),
            ),
        )

    def test_auth_initial_connection_lost_transitions_to_connecting(
        self,
    ) -> None:
        state_lost = Authenticating(
            stage=AuthStage.INITIAL_AUTH,
            host="127.0.0.1",
            port=2181,
            session_id=1,
            kazoo_state=KazooState.LOST,
        )
        next_s1, eff1 = transition(
            state_lost, ConnectionLostEvent(reason="network drop")
        )
        assert next_s1 == Connecting(host="127.0.0.1", port=2181, attempt=1)
        assert eff1 == (CloseSocket(reason="network drop"),)

        state_susp = Authenticating(
            stage=AuthStage.INITIAL_AUTH,
            host="127.0.0.1",
            port=2181,
            session_id=1,
            kazoo_state=KazooState.SUSPENDED,
        )
        next_s2, eff2 = transition(
            state_susp, ConnectionLostEvent(reason="network drop")
        )
        assert next_s2 == Suspended(
            host="127.0.0.1", port=2181, session_id=1, attempt=1
        )
        assert eff2 == (CloseSocket(reason="network drop"),)

    def test_terminal_states_ignore_connection_lost(self) -> None:
        # Inactive is absorbing (0 effects)
        next_state, effects = transition(
            Inactive(), ConnectionLostEvent(reason="drop")
        )
        assert next_state == Inactive()
        assert effects == ()

        # Other terminal states skip state change with BLATHER log
        for term_state in (
            ExpiredSession(session_id=10),
            AuthFailed(error=AuthFailedError()),
        ):
            next_state, effects = transition(
                term_state, ConnectionLostEvent(reason="drop")
            )
            assert next_state == term_state
            assert effects == (
                LogStateTransition(BLATHER, "Skipping state change"),
            )

    def test_suspended_to_expired_session(self) -> None:
        state = Suspended(host="127.0.0.1", port=2181, session_id=42)
        next_state, effects = transition(
            state, SessionExpiredEvent(session_id=42)
        )
        assert next_state == ExpiredSession(session_id=42)
        assert effects == (
            CloseSocket(reason="Session has expired"),
            LogStateTransition(
                logging.INFO,
                "Zookeeper session closed, state: %s",
                (KeeperState.EXPIRED_SESSION,),
            ),
            EmitKazooStateChange(KazooState.LOST),
            FailPendingRequests(KeeperState.EXPIRED_SESSION),
            ResetSessionData(),
        )

    def test_connecting_to_auth_failed_preserves_cause(self) -> None:
        cause = RuntimeError("sasl failure")
        state = Connecting(host="127.0.0.1", port=2181)
        next_state, effects = transition(state, AuthFailedEvent(error=cause))
        assert next_state == AuthFailed(error=cause)
        resolve_effects = [
            e for e in effects if isinstance(e, ResolveConnectResult)
        ]
        assert len(resolve_effects) == 1
        assert resolve_effects[0].success is False
        assert isinstance(resolve_effects[0].exception, AuthFailedError)
        assert resolve_effects[0].exception.__cause__ is cause
        assert StopConnectionLoop() in effects

    def test_connected_to_inactive(self) -> None:
        state = Connected(host="127.0.0.1", port=2181, session_id=42)
        next_state, effects = transition(state, SessionClosedEvent())
        assert next_state == Inactive()
        assert effects == (
            CloseSocket(reason="Connection closed"),
            LogStateTransition(
                logging.INFO,
                "Zookeeper session closed, state: %s",
                (KeeperState.CLOSED,),
            ),
            ResolveConnectResult(
                success=False,
                exception=effects[2].exception,  # type: ignore[union-attr]
            ),
            EmitKazooStateChange(KazooState.LOST),
            FailPendingRequests(KeeperState.CLOSED),
            ResetSessionData(),
            StopConnectionLoop(),
        )
        assert isinstance(
            effects[2].exception,  # type: ignore[union-attr]
            ConnectionClosedError,
        )

    def test_inactive_session_closed_is_idempotent(self) -> None:
        state = Inactive()
        next_state, effects = transition(state, SessionClosedEvent())
        assert next_state == Inactive()
        assert effects == ()

    def test_close_requested_in_connected_forbids_reconnect_on_drop(
        self,
    ) -> None:
        state = Connected(
            host="127.0.0.1",
            port=2181,
            session_id=42,
        )
        s1, eff1 = transition(state, CloseRequested())
        assert isinstance(s1, Connected)
        assert s1.is_connecting_allowed is False
        assert eff1 == (EnqueueCloseRequest(),)

        # Second CloseRequested while closing must be a no-op
        s1_again, eff_again = transition(s1, CloseRequested())
        assert s1_again == s1
        assert eff_again == ()

        s2, _ = transition(s1, ConnectionLostEvent(reason="dropped"))
        assert isinstance(s2, Suspended)
        assert s2.is_connecting_allowed is False

        # Closing ConnectedReadOnly preserves is_connecting_allowed=False on
        # RWServerFoundEvent and SessionExpiredEvent.
        closing_ro = ConnectedReadOnly(
            host="127.0.0.1",
            port=2181,
            session_id=42,
            is_connecting_allowed=False,
        )
        s_rw, _ = transition(
            closing_ro, RWServerFoundEvent(server=("127.0.0.2", 2181))
        )
        assert isinstance(s_rw, Suspended)
        assert s_rw.is_connecting_allowed is False

        s_exp, _ = transition(closing_ro, SessionExpiredEvent(session_id=42))
        assert isinstance(s_exp, ExpiredSession)
        assert s_exp.is_connecting_allowed is False

        # Terminal AuthFailed stays AuthFailed on SessionExpiredEvent and
        # ConnectionLostEvent regardless of keeper_state.
        af = AuthFailed(error=AuthFailedError("bad"))
        assert transition(af, SessionExpiredEvent(session_id=42)) == (af, ())
        s_af, eff_af = transition(
            af,
            ConnectionLostEvent(
                reason="late", keeper_state=KeeperState.CLOSED
            ),
        )
        assert s_af == af
        assert eff_af == (
            LogStateTransition(BLATHER, "Skipping state change"),
        )

    def test_auth_failed_to_inactive(self) -> None:
        state = AuthFailed(error=AuthFailedError("Connection dropped in SASL"))
        next_state, effects = transition(state, SessionClosedEvent())
        assert next_state == Inactive()

    def test_socket_polling_events_in_connected_state(self) -> None:
        state = Connected(
            host="127.0.0.1",
            port=2181,
            session_id=42,
            read_timeout=8.0,
            connect_timeout=4.0,
        )

        # 1. SocketReadable in Connected raises InvalidStateTransitionError
        with pytest.raises(InvalidStateTransitionError):
            transition(state, SocketReadable())

        # 2. WakeUpSignaled in Connected raises InvalidStateTransitionError
        with pytest.raises(InvalidStateTransitionError):
            transition(state, WakeUpSignaled())

        # 3. HeartbeatDeadlineReached -> ping_outstanding=True
        s3, eff3 = transition(state, HeartbeatDeadlineReached())
        assert isinstance(s3, Connected)
        assert s3.ping_outstanding is True
        assert eff3 == (SendHeartbeatPing(),)

        # 4. PingReceived -> ping_outstanding=False
        s4, eff4 = transition(s3, PingReceived())
        assert isinstance(s4, Connected)
        assert s4.ping_outstanding is False
        assert eff4 == ()

        # 5. PollTimeoutExpired while ping_outstanding=True
        s5, eff5 = transition(s3, PollTimeoutExpired())
        assert isinstance(s5, Suspended)
        assert eff5 == (
            CloseSocket(reason="outstanding heartbeat ping not received"),
            LogStateTransition(
                logging.WARNING,
                "Connection dropped: %s",
                ("outstanding heartbeat ping not received",),
            ),
            LogStateTransition(logging.INFO, "Zookeeper connection lost"),
            FailPendingRequests(KeeperState.CONNECTING),
            EmitKazooStateChange(KazooState.SUSPENDED),
            InvalidateWatchers(),
        )

    def test_socket_polling_events_drain_during_terminal_state_shutdown(
        self,
    ) -> None:
        for terminal_state in (
            AuthFailed(),
            ExpiredSession(),
        ):
            s1, eff1 = transition(
                terminal_state,
                WakeUpSignaled(),
            )
            assert s1 == terminal_state
            assert eff1 == (DrainWakeupPipe(),)

            s2, eff2 = transition(
                terminal_state,
                SocketReadable(),
            )
            assert s2 == terminal_state
            assert eff2 == ()

        for in_flight_state in (
            Inactive(),
            Connecting(),
            Handshaking(host="127.0.0.1", port=2181),
            Authenticating(stage=AuthStage.SASL),
            Authenticating(stage=AuthStage.INITIAL_AUTH),
            Suspended(),
        ):
            s_inf, eff_inf = transition(
                in_flight_state,
                WakeUpSignaled(),
            )
            assert s_inf == in_flight_state
            assert eff_inf == (DrainWakeupPipe(),)


class _FakeSASLError(Exception):
    """Stands in for puresasl.SASLError."""


class _FakeSASLProtocolException(Exception):
    """Stands in for puresasl.SASLProtocolException.

    Like the real one, it is a sibling of SASLError, not a subclass.
    """


_FAKE_PURESASL = types.SimpleNamespace(
    SASLError=_FakeSASLError,
    SASLProtocolException=_FakeSASLProtocolException,
)


class _RaisingSASLClient:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    @property
    def complete(self) -> bool:
        return False

    def process(self, challenge: bytes | None = None) -> bytes | None:
        raise self.exc

    def dispose(self) -> None:
        pass


def _failing_sasl_exchange(exc: Exception) -> SASLExchange:
    cfg = SASLConfig(mechanism="PLAIN", username="u", password="p")
    return SASLExchange(
        cfg, "127.0.0.1", client_factory=lambda **kw: _RaisingSASLClient(exc)
    )


class TestSASLConfigAndExchange:
    def test_sasl_config_from_legacy_auth_data(self) -> None:
        auth_data = {("sasl", "alice:secret"), ("digest", "bob:pwd")}
        with pytest.deprecated_call():
            cfg, filtered = SASLConfig.from_client_options(None, auth_data)
        assert cfg == SASLConfig(
            mechanism="DIGEST-MD5",
            username="alice",
            password="secret",
            principal="zk-sasl-md5",
        )
        assert ("sasl", "alice:secret") not in filtered
        assert ("digest", "bob:pwd") in filtered
        assert cfg is not None
        assert cfg.effective_host("zk1.example.com") == "zk-sasl-md5"

    def test_sasl_config_conflict_raises(self) -> None:
        auth_data = {("sasl", "alice:secret")}
        with pytest.raises(ConfigurationError):
            SASLConfig.from_client_options(
                {"mechanism": "GSSAPI"},
                auth_data,
            )

    def test_sasl_exchange_lifecycle(self) -> None:
        class FakeSASLClient:
            def __init__(self, **kwargs: object) -> None:
                self.kwargs = kwargs
                self._complete = False
                self.disposed = False
                self.steps = 0

            @property
            def complete(self) -> bool:
                return self._complete

            def process(self, challenge: bytes | None = None) -> bytes | None:
                self.steps += 1
                if self.steps == 1:
                    return b"initial-token"
                self._complete = True
                return b"final-token"

            def dispose(self) -> None:
                self.disposed = True

        created: list[FakeSASLClient] = []

        def factory(**kwargs: object) -> FakeSASLClient:
            inst = FakeSASLClient(**kwargs)
            created.append(inst)
            return inst

        cfg = SASLConfig(mechanism="DIGEST-MD5", username="u", password="p")
        ex = SASLExchange(cfg, "127.0.0.1", client_factory=factory)
        assert ex.next_token() == b"initial-token"
        assert ex.is_complete is False
        assert ex.next_token(b"server-challenge") == b"final-token"
        assert ex.is_complete is True
        ex.dispose()
        assert created[0].disposed is True
        assert created[0].kwargs["host"] == "zk-sasl-md5"

    def test_sasl_config_gssapi_effective_host(self) -> None:
        cfg = SASLConfig(mechanism="GSSAPI", service="zookeeper")
        assert cfg.effective_host("zk1.example.com") == "zk1.example.com"
        cfg_custom = SASLConfig(
            mechanism="GSSAPI", principal="custom-host", service="zookeeper"
        )
        assert (
            cfg_custom.effective_host("zk1.example.com") == "zk1.example.com"
        )

    def test_sasl_config_from_client_options_validation(self) -> None:
        with pytest.raises(ConfigurationError, match="specify a mechanism"):
            SASLConfig.from_client_options({"mechanism": ""})
        cfg, _ = SASLConfig.from_client_options({"mechanism": "EXTERNAL"})
        assert cfg is not None and cfg.mechanism == "EXTERNAL"
        with pytest.raises(ConfigurationError, match="requires a password"):
            SASLConfig.from_client_options(
                {"mechanism": "DIGEST-MD5", "username": "alice"}
            )
        with pytest.raises(ConfigurationError, match="requires a username"):
            SASLConfig.from_client_options(
                {"mechanism": "PLAIN", "password": "pass"}
            )

    @pytest.mark.parametrize(
        "raised, expected_type, expected_match, wrapped",
        [
            (
                _FakeSASLProtocolException("bad packet"),
                AuthFailedError,
                "protocol error",
                True,
            ),
            (
                _FakeSASLError("internal lib error"),
                SASLException,
                "library error",
                True,
            ),
            (
                RuntimeError("something else"),
                SASLException,
                "SASL step failed",
                True,
            ),
            (
                AuthFailedError("direct auth fail"),
                AuthFailedError,
                "direct auth fail",
                False,
            ),
            (
                SASLException("direct sasl fail"),
                SASLException,
                "direct sasl fail",
                False,
            ),
        ],
    )
    def test_sasl_exchange_error_handling(
        self,
        monkeypatch: pytest.MonkeyPatch,
        raised: Exception,
        expected_type: type[Exception],
        expected_match: str,
        wrapped: bool,
    ) -> None:
        monkeypatch.setattr(sasl_mod, "PURESASL_AVAILABLE", True)
        monkeypatch.setattr(
            sasl_mod, "puresasl", _FAKE_PURESASL, raising=False
        )
        ex = _failing_sasl_exchange(raised)

        with pytest.raises(expected_type, match=expected_match) as exc_info:
            ex.next_token()

        if wrapped:
            assert exc_info.value.__cause__ is raised
        else:
            assert exc_info.value is raised

    def test_sasl_exchange_error_handling_without_puresasl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With a custom client_factory and no puresasl installed, the
        # puresasl name does not exist in the module and must not be touched.
        monkeypatch.setattr(sasl_mod, "PURESASL_AVAILABLE", False)
        monkeypatch.delattr(sasl_mod, "puresasl", raising=False)
        raised = RuntimeError("something else")
        ex = _failing_sasl_exchange(raised)

        with pytest.raises(
            SASLException, match="SASL step failed"
        ) as exc_info:
            ex.next_token()

        assert exc_info.value.__cause__ is raised


class TestClientStateMachineDriver:
    def test_client_initial_dataclass_state(self) -> None:
        client = KazooClient()
        assert client.state_machine == Inactive()
        assert client.client_state == KeeperState.CLOSED
        assert client.state == KazooState.LOST

    def test_reentrant_send_event_in_listener_does_not_deadlock(self) -> None:
        client = KazooClient()
        observed_states: list[KazooState] = []

        def reentrant_listener(state: KazooState) -> None:
            observed_states.append(state)
            if state == KazooState.CONNECTED:
                # Re-entrantly inject a ConnectionLostEvent inside the listener
                client.send_event(ConnectionLostEvent(reason="simulated drop"))

        client.add_listener(reentrant_listener)
        client._state_machine.state = Handshaking(
            host="127.0.0.1", port=2181, num_hosts=1
        )
        client.send_event(
            ConnectResponseReceived(
                connect_result=Connect(0, 0, 10000, 100, b"pwd", False)
            )
        )

        # Both transitions must have processed sequentially without deadlock
        assert observed_states == [
            KazooState.CONNECTED,
            KazooState.SUSPENDED,
        ]
        assert isinstance(client.state_machine, Suspended)
        assert client.state == KazooState.SUSPENDED
        assert not client.connected

    def test_cross_thread_close_waits_and_survives_effect_exception(
        self,
    ) -> None:
        """Cross-thread CloseRequested waits for effect and is not lost."""
        import threading

        handler = SequentialThreadingHandler()
        entered_effect = threading.Event()
        release_effect = threading.Event()
        events_executed: list[str] = []

        def executor(effect: SideEffect) -> None:
            if isinstance(effect, SendHeartbeatPing):
                events_executed.append("ping_start")
                entered_effect.set()
                release_effect.wait(timeout=2.0)
                raise ConnectionDropped("ping failed")
            if isinstance(effect, EnqueueCloseRequest):
                events_executed.append("close_enqueued")

        driver = StateMachineDriver(
            executor,
            lock=handler.rlock_object(),
            initial_state=Connected(host="127.0.0.1", port=2181),
        )

        ping_exc: list[BaseException] = []

        def run_ping() -> None:
            try:
                driver.send_event(HeartbeatDeadlineReached())
            except BaseException as exc:
                ping_exc.append(exc)

        t_ping = threading.Thread(target=run_ping)
        t_ping.start()
        assert entered_effect.wait(timeout=2.0)

        close_done = threading.Event()

        def run_close() -> None:
            driver.send_event(CloseRequested())
            close_done.set()

        t_close = threading.Thread(target=run_close)
        t_close.start()

        # Cross-thread CloseRequested must block until active effect finishes
        assert not close_done.wait(timeout=0.05)

        release_effect.set()
        t_ping.join(timeout=2.0)
        t_close.join(timeout=2.0)

        assert len(ping_exc) == 1
        assert isinstance(ping_exc[0], ConnectionDropped)
        assert close_done.is_set()
        assert events_executed == ["ping_start", "close_enqueued"]
        assert isinstance(driver.state, Connected)
        assert driver.state.is_connecting_allowed is False

    def test_wakeup_stays_registered_and_drains_during_handshaking(
        self,
    ) -> None:
        client = KazooClient()
        conn = client._connection
        s_zk_a, s_zk_b = socket.socketpair()
        s_wk_a, s_wk_b = socket.socketpair()
        try:
            s_wk_a.setblocking(False)
            conn._selector = client.handler.create_selector()
            conn._socket = s_zk_a
            conn._read_sock = s_wk_a
            conn._write_sock = s_wk_b
            conn._setup_selector()

            client.send_event(StartRequested())
            client.send_event(
                ConnectAttemptStarted(host="127.0.0.1", port=2181)
            )
            s_wk_b.send(b"\0")
            assert conn._select(timeout=0.0) == {"wakeup"}

            # Emitting WakeUpSignaled drains the stray byte while keeping
            # wakeup registered so client.stop() can always interrupt select().
            client.send_event(WakeUpSignaled())
            assert conn._wakeup_registered is True
            assert conn._select(timeout=0.0) == set()
        finally:
            conn._close_selector()
            s_zk_a.close()
            s_zk_b.close()
            s_wk_a.close()
            s_wk_b.close()

    def test_select_respects_ssl_pending_bytes(self) -> None:
        client = KazooClient()
        conn = client._connection
        s_zk_a, s_zk_b = socket.socketpair()
        s_wk_a, s_wk_b = socket.socketpair()
        try:

            class FakeSSLSocket:
                def fileno(self) -> int:
                    return s_zk_a.fileno()

                def pending(self) -> int:
                    return 64

            fake_ssl = FakeSSLSocket()
            conn._selector = client.handler.create_selector()
            conn._socket = fake_ssl  # type: ignore[assignment]
            conn._ssl_socket = fake_ssl  # type: ignore[assignment]
            conn._read_sock = s_wk_a
            conn._setup_selector()

            # Even with nothing written to s_zk_b, pending() > 0 makes
            # "zk_socket" ready immediately without waiting for timeout
            ready = conn._select(timeout=5.0)
            assert ready == {"zk_socket"}
        finally:
            conn._close_selector()
            s_zk_a.close()
            s_zk_b.close()
            s_wk_a.close()
            s_wk_b.close()

    def test_read_socket_auth_xid_resolves_pending_add_auth_promise_success(
        self,
    ) -> None:
        client = KazooClient()
        client._state_machine.state = Connected(host="127.0.0.1", port=2181)
        conn = client._connection
        async_result = client.handler.async_result()
        client._pending.append(
            (Auth(0, "digest", "u:p"), async_result, ReservedXID.AUTH)
        )

        conn._read_packet = (  # type: ignore[method-assign]
            lambda timeout=None, **kw: AuthResponse(zxid=10, err=0)
        )

        conn._read_socket()
        assert len(client._pending) == 0
        assert async_result.successful() is True
        assert async_result.get() is True

    def test_read_socket_auth_xid_resolves_pending_add_auth_promise_failure(
        self,
    ) -> None:
        client = KazooClient()
        client._state_machine.state = Authenticating(
            stage=AuthStage.INITIAL_AUTH, host="127.0.0.1", port=2181
        )
        conn = client._connection
        async_result = client.handler.async_result()
        client._pending.append(
            (Auth(0, "digest", "u:p"), async_result, ReservedXID.AUTH)
        )

        # err=-115 (AuthFailed)
        conn._read_packet = (  # type: ignore[method-assign]
            lambda timeout=None, **kw: AuthResponse(zxid=10, err=-115)
        )

        conn._read_socket()
        assert len(client._pending) == 0
        assert async_result.successful() is False
        with pytest.raises(AuthFailedError):
            async_result.get()
        assert isinstance(client.state_machine, AuthFailed)

    def test_sasl_connection_dropped_retries_next_host(
        self,
    ) -> None:
        client = KazooClient(
            sasl_options={
                "mechanism": "PLAIN",
                "username": "u",
                "password": "p",
            }
        )
        client._connection._start_sasl = (  # type: ignore[method-assign]
            lambda host: None
        )
        client._state_machine.state = Handshaking(
            host="127.0.0.1", port=2181, num_hosts=1, has_sasl=True
        )
        client.send_event(
            ConnectResponseReceived(
                connect_result=Connect(0, 0, 10000, 1, b"pwd", False)
            )
        )
        assert isinstance(client.state_machine, Authenticating)
        assert client.state_machine.stage == AuthStage.SASL
        client.send_event(
            ConnectionLostEvent(reason="Connection reset by peer")
        )
        assert isinstance(client.state_machine, Connecting)
        assert client.state_machine.is_connecting_allowed is True
        assert client.state_machine.is_terminal is False

    def test_handshake_timeout_suspended_reconnect_lost_delivery(
        self,
    ) -> None:
        client = KazooClient(hosts="127.0.0.1:2181")
        mock_sock = MagicMock()
        mock_sock.send.side_effect = lambda v: len(v)
        client._connection._socket = mock_sock
        seen: list[KazooState] = []
        client.add_listener(lambda s: seen.append(s))

        # Establish session
        client._state_machine.state = Handshaking(
            host="127.0.0.1", port=2181, num_hosts=1
        )
        client.send_event(
            ConnectResponseReceived(Connect(0, 0, 10000, 42, b"pwd", False))
        )
        assert client.state == KazooState.CONNECTED
        assert seen == [KazooState.CONNECTED]

        # Drop connection -> transitions to Suspended
        client.send_event(ConnectionLostEvent(reason="network partition"))
        assert client.state == KazooState.SUSPENDED
        assert seen == [KazooState.CONNECTED, KazooState.SUSPENDED]

        # Reconnect attempt starts, socket connects -> Handshaking with
        # kazoo_state=SUSPENDED
        client.send_event(ConnectAttemptStarted(host="127.0.0.1", port=2181))
        client._connection._socket = mock_sock
        client.send_event(
            SocketConnected(host="127.0.0.1", port=2181, session_id=42)
        )
        assert isinstance(client.state_machine, Handshaking)
        assert client.state == KazooState.SUSPENDED

        # Handshake times out: must remain Suspended, not silently drop to LOST
        client.send_event(PollTimeoutExpired())
        assert isinstance(client.state_machine, Suspended)
        assert client.state == KazooState.SUSPENDED
        assert seen == [KazooState.CONNECTED, KazooState.SUSPENDED]

        # Server subsequently reports session expired
        client.send_event(SessionExpiredEvent(session_id=42))
        assert isinstance(client.state_machine, ExpiredSession)
        assert client.state == KazooState.LOST
        # Exactly one LOST notification delivered
        assert seen == [
            KazooState.CONNECTED,
            KazooState.SUSPENDED,
            KazooState.LOST,
        ]

    def test_sasl_timeout_suspended_reconnect_lost_delivery(self) -> None:
        client = KazooClient(hosts="127.0.0.1:2181")
        mock_sock = MagicMock()
        mock_sock.send.side_effect = lambda v: len(v)
        client._connection._socket = mock_sock
        seen: list[KazooState] = []
        client.add_listener(lambda s: seen.append(s))
        client._state_machine.state = Handshaking(
            host="127.0.0.1", port=2181, session_id=42
        )
        client.send_event(
            ConnectResponseReceived(Connect(0, 0, 10000, 42, b"pwd", False))
        )
        assert client.state == KazooState.CONNECTED
        assert seen == [KazooState.CONNECTED]

        # Drop connection -> transitions to Suspended
        client.send_event(ConnectionLostEvent(reason="network partition"))
        assert client.state == KazooState.SUSPENDED
        assert seen == [KazooState.CONNECTED, KazooState.SUSPENDED]

        # Reconnect starts SASL negotiation -> Authenticating(SASL)
        # with kazoo_state=SUSPENDED
        client.send_event(ConnectAttemptStarted(host="127.0.0.1", port=2181))
        client._connection._socket = mock_sock
        client.send_event(
            SocketConnected(host="127.0.0.1", port=2181, session_id=42)
        )
        client._state_machine.state = Authenticating(
            stage=AuthStage.SASL,
            host="127.0.0.1",
            port=2181,
            session_id=42,
            kazoo_state=KazooState.SUSPENDED,
        )
        assert client.state == KazooState.SUSPENDED

        # SASL times out: must remain Suspended, not drop to LOST
        client.send_event(PollTimeoutExpired())
        assert isinstance(client.state_machine, Suspended)
        assert client.state == KazooState.SUSPENDED
        assert seen == [KazooState.CONNECTED, KazooState.SUSPENDED]

        # Server subsequently reports session expired
        client.send_event(SessionExpiredEvent(session_id=42))
        assert isinstance(client.state_machine, ExpiredSession)
        assert client.state == KazooState.LOST
        assert seen == [
            KazooState.CONNECTED,
            KazooState.SUSPENDED,
            KazooState.LOST,
        ]

    def test_driver_discards_queued_events_on_effect_failure(
        self,
    ) -> None:
        class BoomError(Exception):
            pass

        def reentrant_exploding_executor(effect: SideEffect) -> None:
            if isinstance(effect, CloseSocket):
                # Real re-entrant call queues the event because driver is
                # actively processing
                driver.send_event(
                    SASLChallengeReceived(challenge=b"challenge_data")
                )
            elif isinstance(effect, LogStateTransition):
                raise BoomError("Effect failed")

        handler = SequentialThreadingHandler()
        driver = StateMachineDriver(
            reentrant_exploding_executor, lock=handler.rlock_object()
        )
        driver.state = Handshaking(host="127.0.0.1", port=2181, num_hosts=1)

        # When effect raises BoomError, queued events must be discarded
        with pytest.raises(BoomError):
            driver.send_event(PollTimeoutExpired())

        assert len(driver._event_queue) == 0
        assert driver._processing_events is False

        # Next event succeeds without replaying the discarded event
        driver.send_event(ConnectionLostEvent())
        assert isinstance(driver.state, Connecting)

    def test_driver_without_lock_serializes_reentrant_events(self) -> None:
        """StateMachineDriver works with lock=None (no IHandler dependency)."""
        seen: list[str] = []

        def executor(effect: SideEffect) -> None:
            if isinstance(effect, SendConnectPacket):
                seen.append("connect")
                driver.send_event(ConnectionLostEvent(reason="reentrant"))
            elif isinstance(effect, CloseSocket):
                seen.append("close")

        driver = StateMachineDriver(executor, initial_state=Connecting())
        driver.send_event(SocketConnected(host="127.0.0.1", port=2181))
        assert seen == ["connect", "close"]
        assert isinstance(driver.state, Connecting)

    def test_inactive_absorbs_late_events(self) -> None:
        inactive = Inactive()
        for event in (
            SessionExpiredEvent(session_id=1),
            AuthFailedEvent(),
            SASLChallengeReceived(challenge=b"late"),
            SASLCompleted(),
            AuthPacketCompleted(),
            ConnectionLostEvent(),
            PingReceived(),
            PollTimeoutExpired(),
        ):
            next_state, effects = transition(inactive, event)
            assert isinstance(next_state, Inactive)
            assert effects == ()

        # WakeUpSignaled in Inactive drains wakeup pipe
        st_wake, eff_wake = transition(inactive, WakeUpSignaled())
        assert st_wake == inactive
        assert len(eff_wake) == 1
        assert isinstance(eff_wake[0], DrainWakeupPipe)

    def test_auth_failed_and_expired_session_readiness_effects(self) -> None:
        for state in (AuthFailed(), ExpiredSession(session_id=1)):
            next_state, read_effects = transition(state, SocketReadable())
            assert next_state == state
            assert read_effects == ()

            next_state, wake_effects = transition(state, WakeUpSignaled())
            assert next_state == state
            assert len(wake_effects) == 1
            assert isinstance(wake_effects[0], DrainWakeupPipe)

    def test_execute_effect_session_established_resets_retry(
        self,
    ) -> None:
        client = KazooClient()
        conn = client._connection
        mock_retry = MagicMock()
        conn._current_retry = mock_retry
        assert conn._is_session_active is False

        client._state_machine.state = Handshaking(
            host="127.0.0.1", port=2181, num_hosts=1
        )
        client.send_event(
            ConnectResponseReceived(
                connect_result=Connect(0, 0, 10000, 1, b"pwd", False)
            )
        )
        assert conn._is_session_active is True
        mock_retry.reset.assert_called_once()

    def test_execute_effect_stop_connection_loop(self) -> None:
        client = KazooClient()
        conn = client._connection
        client.send_event(StartRequested())
        assert conn._is_terminal is False

        client.send_event(SessionClosedEvent())
        assert conn._is_terminal is True

    def test_execute_effect_update_session_data(self) -> None:
        client = KazooClient()
        conn = client._connection
        conn.execute_effect(
            UpdateSessionData(
                session_id=888,
                session_passwd=b"mysecret",
                protocol_version=1,
            )
        )
        assert client._session_id == 888
        assert client._session_passwd == b"mysecret"
        assert client._protocol_version == 1

    def test_execute_effect_configure_timeouts(self) -> None:
        client = KazooClient()
        conn = client._connection
        conn.execute_effect(
            ConfigureTimeouts(read_timeout=8.0, connect_timeout=4.0)
        )
        assert conn._read_timeout == 8.0
        assert conn._connect_timeout == 4.0

    def test_execute_effect_configure_ro_mode(self) -> None:
        client = KazooClient()
        conn = client._connection
        assert bool(conn._ro_mode) is False
        conn.execute_effect(ConfigureReadOnlyMode(read_only=True))
        assert bool(conn._ro_mode) is True
        conn.execute_effect(ConfigureReadOnlyMode(read_only=False))
        assert bool(conn._ro_mode) is False


class TestFrameDecoderAndRequestManager:
    def test_frame_decoder_incremental_and_pipelined_frames(self) -> None:
        from collections import deque

        from kazoo.protocol.serialization import Delete

        pending: deque[tuple[object, object, int]] = deque(
            [
                (Delete("/1", -1), None, 1),
                (Delete("/1b", -1), None, 1),
                (Delete("/2", -1), None, 2),
            ]
        )
        decoder = FrameDecoder(pop_pending=pending.popleft)
        assert decoder.frame_ready is False

        # Build two ReplyHeader frames
        f1 = reply_header_struct.pack(1, 100, 0)
        f2 = reply_header_struct.pack(2, 101, 0)
        wire = int_struct.pack(len(f1)) + f1 + int_struct.pack(len(f2)) + f2

        # Feed 1 byte at a time for the first frame
        first_frame_wire = wire[: 4 + len(f1)]
        for b in first_frame_wire[:-1]:
            decoder._feed(bytes([b]))
            assert decoder.pop_packet() is None
        decoder._feed(first_frame_wire[-1:])
        p1 = decoder.pop_packet()
        assert isinstance(p1, ResponsePacket)
        assert p1.header == ReplyHeader(xid=1, zxid=100, err=0)
        assert decoder.pop_packet() is None

        # Feed both frames concatenated at once
        decoder._feed(wire)
        p_both_1 = decoder.pop_packet()
        p_both_2 = decoder.pop_packet()
        assert isinstance(p_both_1, ResponsePacket)
        assert isinstance(p_both_2, ResponsePacket)
        assert p_both_1.header == ReplyHeader(xid=1, zxid=100, err=0)
        assert p_both_2.header == ReplyHeader(xid=2, zxid=101, err=0)
        assert decoder.pop_packet() is None

    def test_frame_decoder_connect_and_typed_packets(self) -> None:
        from collections import deque

        pending: deque[tuple[object, object, int]] = deque(
            [(SASL(b"tok"), None, 3)]
        )
        decoder = FrameDecoder(pop_pending=pending.popleft)
        body = bytearray()
        body.extend(int_int_long_struct.pack(0, 30000, 12345))
        body.extend(_write_buffer(b"password12345678"))
        body.extend([0])
        wire = int_struct.pack(len(body)) + bytes(body)

        decoder._feed(wire[:3])
        assert decoder.pop_packet() is None
        decoder._feed(wire[3:])
        conn_res = decoder.pop_packet()
        assert isinstance(conn_res, Connect)
        assert conn_res.session_id == 12345

        # Ping (-2), Auth (-4), SASL (xid=3)
        f_ping = reply_header_struct.pack(-2, 200, 0)
        f_auth = reply_header_struct.pack(-4, 201, 0)
        f_sasl = reply_header_struct.pack(3, 202, 0) + _write_buffer(b"tok")
        for f in (f_ping, f_auth, f_sasl):
            decoder._feed(int_struct.pack(len(f)) + f)

        assert decoder.pop_packet() == PingResponse(zxid=200, err=0)
        assert decoder.pop_packet() == AuthResponse(zxid=201, err=0)
        assert decoder.pop_packet() == SASLResponse(
            xid=3, zxid=202, err=0, challenge=b"tok"
        )

        decoder.reset()
        with pytest.raises(ConnectionDropped, match="Invalid frame length"):
            decoder._feed(int_struct.pack(-5))

    def test_request_manager_lifecycle(self) -> None:
        mgr = RequestManager()
        mgr.reset_xid(0)
        assert mgr.next_xid() == 1
        assert mgr.next_xid() == 2

        r1, a1 = MagicMock(), MagicMock()
        mgr.enqueue(r1, a1)
        assert mgr.peek_queued() == (r1, a1)
        assert mgr.has_send_capacity(1) is True

        mgr.dequeue_to_pending(xid=10)
        assert mgr.has_send_capacity(1) is False
        assert mgr.pop_pending() == (r1, a1, 10)
        assert mgr.has_send_capacity(1) is True


_MATRIX_STATES: list[tuple[str, ClientState]] = [
    ("Inactive", Inactive()),
    ("Connecting", Connecting()),
    ("Handshaking", Handshaking(host="127.0.0.1", port=2181)),
    (
        "Handshaking[SUSPENDED]",
        Handshaking(
            host="127.0.0.1",
            port=2181,
            session_id=1,
            kazoo_state=KazooState.SUSPENDED,
        ),
    ),
    (
        "Authenticating.SASL",
        Authenticating(stage=AuthStage.SASL, host="127.0.0.1", port=2181),
    ),
    (
        "Authenticating.SASL[SUSPENDED]",
        Authenticating(
            stage=AuthStage.SASL,
            host="127.0.0.1",
            port=2181,
            session_id=1,
            kazoo_state=KazooState.SUSPENDED,
        ),
    ),
    (
        "Authenticating.INITIAL_AUTH",
        Authenticating(
            stage=AuthStage.INITIAL_AUTH, host="127.0.0.1", port=2181
        ),
    ),
    (
        "Authenticating.INITIAL_AUTH[SUSPENDED]",
        Authenticating(
            stage=AuthStage.INITIAL_AUTH,
            host="127.0.0.1",
            port=2181,
            session_id=1,
            kazoo_state=KazooState.SUSPENDED,
        ),
    ),
    (
        "Connected",
        Connected(host="127.0.0.1", port=2181, session_id=1),
    ),
    (
        "ConnectedReadOnly",
        ConnectedReadOnly(host="127.0.0.1", port=2181, session_id=1),
    ),
    ("Suspended", Suspended(host="127.0.0.1", port=2181, session_id=1)),
    ("ExpiredSession", ExpiredSession(session_id=1)),
    ("AuthFailed", AuthFailed()),
]

_MATRIX_EVENTS: list[tuple[str, ClientEvent]] = [
    ("StartRequested", StartRequested()),
    (
        "ConnectAttemptStarted",
        ConnectAttemptStarted(host="127.0.0.1", port=2181),
    ),
    ("SocketConnected", SocketConnected(host="127.0.0.1", port=2181)),
    (
        "ConnectResponseReceived",
        ConnectResponseReceived(
            connect_result=Connect(0, 0, 10000, 1, b"pwd", False)
        ),
    ),
    ("SASLChallengeReceived", SASLChallengeReceived(challenge=b"token")),
    ("SASLCompleted", SASLCompleted()),
    ("AuthPacketCompleted", AuthPacketCompleted()),
    ("ConnectionLostEvent", ConnectionLostEvent()),
    ("SessionExpiredEvent", SessionExpiredEvent(session_id=1)),
    ("AuthFailedEvent", AuthFailedEvent()),
    ("SessionClosedEvent", SessionClosedEvent()),
    ("CloseRequested", CloseRequested()),
    ("RWServerFoundEvent", RWServerFoundEvent(server=("127.0.0.1", 2182))),
    ("SocketReadable", SocketReadable()),
    ("WakeUpSignaled", WakeUpSignaled()),
    ("PollTimeoutExpired", PollTimeoutExpired()),
    ("HeartbeatDeadlineReached", HeartbeatDeadlineReached()),
    ("PingReceived", PingReceived()),
]

_SAME = "_SAME"
_RAISE = "_RAISE"

_EFF_SUSPEND = (
    LogStateTransition,
    FailPendingRequests,
    EmitKazooStateChange,
    InvalidateWatchers,
)
_EFF_CLOSE_SUSPEND = (CloseSocket,) + _EFF_SUSPEND
_EFF_FINALIZE = (
    LogStateTransition,
    LogStateTransition,
    ResolveConnectResult,
    EmitKazooStateChange,
    SessionEstablished,
)
_EFF_CONNECT_OK = (
    UpdateSessionData,
    ConfigureTimeouts,
    ConfigureReadOnlyMode,
) + _EFF_FINALIZE
_EFF_EXPIRED = (
    CloseSocket,
    LogStateTransition,
    EmitKazooStateChange,
    FailPendingRequests,
    ResetSessionData,
)
_EFF_AUTH_FAILED = (
    CloseSocket,
    LogStateTransition,
    ResolveConnectResult,
    EmitKazooStateChange,
    FailPendingRequests,
    ResetSessionData,
    StopConnectionLoop,
)
_EFF_CLOSED = (
    CloseSocket,
    LogStateTransition,
    ResolveConnectResult,
    EmitKazooStateChange,
    FailPendingRequests,
    ResetSessionData,
    StopConnectionLoop,
)
_EFF_POLL_TIMEOUT = (CloseSocket, LogStateTransition)

_EXPECTED_MATRIX: dict[
    str, dict[str, str | tuple[str | type[ClientState], tuple[type[Any], ...]]]
] = {
    "StartRequested": {
        "Inactive": (Connecting, ()),
        "Connecting": (_SAME, ()),
        "Handshaking": (_SAME, ()),
        "Handshaking[SUSPENDED]": (_SAME, ()),
        "Authenticating.SASL": (_SAME, ()),
        "Authenticating.SASL[SUSPENDED]": (_SAME, ()),
        "Authenticating.INITIAL_AUTH": (_SAME, ()),
        "Authenticating.INITIAL_AUTH[SUSPENDED]": (_SAME, ()),
        "Connected": (_SAME, ()),
        "ConnectedReadOnly": (_SAME, ()),
        "Suspended": (_SAME, ()),
        "ExpiredSession": (Connecting, ()),
        "AuthFailed": (Connecting, ()),
    },
    "ConnectAttemptStarted": {
        "Inactive": (_SAME, ()),
        "Connecting": (Connecting, (LogStateTransition,)),
        "Handshaking": (Connecting, ()),
        "Handshaking[SUSPENDED]": (Suspended, ()),
        "Authenticating.SASL": (Connecting, ()),
        "Authenticating.SASL[SUSPENDED]": (Suspended, ()),
        "Authenticating.INITIAL_AUTH": (Connecting, ()),
        "Authenticating.INITIAL_AUTH[SUSPENDED]": (Suspended, ()),
        "Connected": (Suspended, _EFF_SUSPEND),
        "ConnectedReadOnly": (Suspended, _EFF_SUSPEND),
        "Suspended": (Suspended, ()),
        "ExpiredSession": (Connecting, (LogStateTransition,)),
        "AuthFailed": (_SAME, (LogStateTransition,)),
    },
    "SocketConnected": {
        "Inactive": (_SAME, ()),
        "Connecting": (Handshaking, (SendConnectPacket,)),
        "Handshaking": (Handshaking, (SendConnectPacket,)),
        "Handshaking[SUSPENDED]": (Handshaking, (SendConnectPacket,)),
        "Authenticating.SASL": _RAISE,
        "Authenticating.SASL[SUSPENDED]": _RAISE,
        "Authenticating.INITIAL_AUTH": _RAISE,
        "Authenticating.INITIAL_AUTH[SUSPENDED]": _RAISE,
        "Connected": _RAISE,
        "ConnectedReadOnly": _RAISE,
        "Suspended": (Handshaking, (SendConnectPacket,)),
        "ExpiredSession": _RAISE,
        "AuthFailed": (_SAME, ()),
    },
    "ConnectResponseReceived": {
        "Inactive": (_SAME, ()),
        "Connecting": _RAISE,
        "Handshaking": (Connected, _EFF_CONNECT_OK),
        "Handshaking[SUSPENDED]": (Connected, _EFF_CONNECT_OK),
        "Authenticating.SASL": _RAISE,
        "Authenticating.SASL[SUSPENDED]": _RAISE,
        "Authenticating.INITIAL_AUTH": _RAISE,
        "Authenticating.INITIAL_AUTH[SUSPENDED]": _RAISE,
        "Connected": _RAISE,
        "ConnectedReadOnly": _RAISE,
        "Suspended": _RAISE,
        "ExpiredSession": _RAISE,
        "AuthFailed": (_SAME, ()),
    },
    "SASLChallengeReceived": {
        "Inactive": (_SAME, ()),
        "Connecting": _RAISE,
        "Handshaking": _RAISE,
        "Handshaking[SUSPENDED]": _RAISE,
        "Authenticating.SASL": (_SAME, (StepSASLHandshake,)),
        "Authenticating.SASL[SUSPENDED]": (_SAME, (StepSASLHandshake,)),
        "Authenticating.INITIAL_AUTH": _RAISE,
        "Authenticating.INITIAL_AUTH[SUSPENDED]": _RAISE,
        "Connected": _RAISE,
        "ConnectedReadOnly": _RAISE,
        "Suspended": _RAISE,
        "ExpiredSession": _RAISE,
        "AuthFailed": _RAISE,
    },
    "SASLCompleted": {
        "Inactive": (_SAME, ()),
        "Connecting": _RAISE,
        "Handshaking": _RAISE,
        "Handshaking[SUSPENDED]": _RAISE,
        "Authenticating.SASL": (Connected, _EFF_FINALIZE),
        "Authenticating.SASL[SUSPENDED]": (Connected, _EFF_FINALIZE),
        "Authenticating.INITIAL_AUTH": _RAISE,
        "Authenticating.INITIAL_AUTH[SUSPENDED]": _RAISE,
        "Connected": _RAISE,
        "ConnectedReadOnly": _RAISE,
        "Suspended": _RAISE,
        "ExpiredSession": _RAISE,
        "AuthFailed": _RAISE,
    },
    "AuthPacketCompleted": {
        "Inactive": (_SAME, ()),
        "Connecting": _RAISE,
        "Handshaking": _RAISE,
        "Handshaking[SUSPENDED]": _RAISE,
        "Authenticating.SASL": _RAISE,
        "Authenticating.SASL[SUSPENDED]": _RAISE,
        "Authenticating.INITIAL_AUTH": (Connected, _EFF_FINALIZE),
        "Authenticating.INITIAL_AUTH[SUSPENDED]": (Connected, _EFF_FINALIZE),
        "Connected": (_SAME, ()),
        "ConnectedReadOnly": (_SAME, ()),
        "Suspended": _RAISE,
        "ExpiredSession": _RAISE,
        "AuthFailed": _RAISE,
    },
    "ConnectionLostEvent": {
        "Inactive": (_SAME, ()),
        "Connecting": (_SAME, ()),
        "Handshaking": (Connecting, (CloseSocket,)),
        "Handshaking[SUSPENDED]": (Suspended, (CloseSocket,)),
        "Authenticating.SASL": (
            Connecting,
            (CloseSocket, LogStateTransition),
        ),
        "Authenticating.SASL[SUSPENDED]": (
            Suspended,
            (CloseSocket, LogStateTransition),
        ),
        "Authenticating.INITIAL_AUTH": (Connecting, (CloseSocket,)),
        "Authenticating.INITIAL_AUTH[SUSPENDED]": (Suspended, (CloseSocket,)),
        "Connected": (Suspended, _EFF_CLOSE_SUSPEND),
        "ConnectedReadOnly": (Suspended, _EFF_CLOSE_SUSPEND),
        "Suspended": (_SAME, ()),
        "ExpiredSession": (_SAME, (LogStateTransition,)),
        "AuthFailed": (_SAME, (LogStateTransition,)),
    },
    "SessionExpiredEvent": {
        "Inactive": (_SAME, ()),
        "Connecting": (ExpiredSession, _EFF_EXPIRED),
        "Handshaking": (ExpiredSession, _EFF_EXPIRED),
        "Handshaking[SUSPENDED]": (ExpiredSession, _EFF_EXPIRED),
        "Authenticating.SASL": (ExpiredSession, _EFF_EXPIRED),
        "Authenticating.SASL[SUSPENDED]": (ExpiredSession, _EFF_EXPIRED),
        "Authenticating.INITIAL_AUTH": (ExpiredSession, _EFF_EXPIRED),
        "Authenticating.INITIAL_AUTH[SUSPENDED]": (
            ExpiredSession,
            _EFF_EXPIRED,
        ),
        "Connected": (ExpiredSession, _EFF_EXPIRED),
        "ConnectedReadOnly": (ExpiredSession, _EFF_EXPIRED),
        "Suspended": (ExpiredSession, _EFF_EXPIRED),
        "ExpiredSession": (_SAME, ()),
        "AuthFailed": (_SAME, ()),
    },
    "AuthFailedEvent": {
        "Inactive": (_SAME, ()),
        "Connecting": (AuthFailed, _EFF_AUTH_FAILED),
        "Handshaking": (AuthFailed, _EFF_AUTH_FAILED),
        "Handshaking[SUSPENDED]": (AuthFailed, _EFF_AUTH_FAILED),
        "Authenticating.SASL": (AuthFailed, _EFF_AUTH_FAILED),
        "Authenticating.SASL[SUSPENDED]": (AuthFailed, _EFF_AUTH_FAILED),
        "Authenticating.INITIAL_AUTH": (AuthFailed, _EFF_AUTH_FAILED),
        "Authenticating.INITIAL_AUTH[SUSPENDED]": (
            AuthFailed,
            _EFF_AUTH_FAILED,
        ),
        "Connected": (AuthFailed, _EFF_AUTH_FAILED),
        "ConnectedReadOnly": (AuthFailed, _EFF_AUTH_FAILED),
        "Suspended": (AuthFailed, _EFF_AUTH_FAILED),
        "ExpiredSession": (AuthFailed, _EFF_AUTH_FAILED),
        "AuthFailed": (_SAME, ()),
    },
    "SessionClosedEvent": {
        "Inactive": (_SAME, ()),
        "Connecting": (Inactive, _EFF_CLOSED),
        "Handshaking": (Inactive, _EFF_CLOSED),
        "Handshaking[SUSPENDED]": (Inactive, _EFF_CLOSED),
        "Authenticating.SASL": (Inactive, _EFF_CLOSED),
        "Authenticating.SASL[SUSPENDED]": (Inactive, _EFF_CLOSED),
        "Authenticating.INITIAL_AUTH": (Inactive, _EFF_CLOSED),
        "Authenticating.INITIAL_AUTH[SUSPENDED]": (Inactive, _EFF_CLOSED),
        "Connected": (Inactive, _EFF_CLOSED),
        "ConnectedReadOnly": (Inactive, _EFF_CLOSED),
        "Suspended": (Inactive, _EFF_CLOSED),
        "ExpiredSession": (Inactive, _EFF_CLOSED),
        "AuthFailed": (Inactive, _EFF_CLOSED),
    },
    "CloseRequested": {
        "Inactive": (_SAME, ()),
        "Connecting": (Inactive, _EFF_CLOSED),
        "Handshaking": (Inactive, _EFF_CLOSED),
        "Handshaking[SUSPENDED]": (Inactive, _EFF_CLOSED),
        "Authenticating.SASL": (Inactive, _EFF_CLOSED),
        "Authenticating.SASL[SUSPENDED]": (Inactive, _EFF_CLOSED),
        "Authenticating.INITIAL_AUTH": (Inactive, _EFF_CLOSED),
        "Authenticating.INITIAL_AUTH[SUSPENDED]": (Inactive, _EFF_CLOSED),
        "Connected": (Connected, (EnqueueCloseRequest,)),
        "ConnectedReadOnly": (ConnectedReadOnly, (EnqueueCloseRequest,)),
        "Suspended": (Inactive, _EFF_CLOSED),
        "ExpiredSession": (Inactive, _EFF_CLOSED),
        "AuthFailed": (Inactive, _EFF_CLOSED),
    },
    "RWServerFoundEvent": {
        "Inactive": (_SAME, ()),
        "Connecting": (_SAME, ()),
        "Handshaking": (_SAME, ()),
        "Handshaking[SUSPENDED]": (_SAME, ()),
        "Authenticating.SASL": (_SAME, ()),
        "Authenticating.SASL[SUSPENDED]": (_SAME, ()),
        "Authenticating.INITIAL_AUTH": (_SAME, ()),
        "Authenticating.INITIAL_AUTH[SUSPENDED]": (_SAME, ()),
        "Connected": (Suspended, _EFF_CLOSE_SUSPEND),
        "ConnectedReadOnly": (Suspended, _EFF_CLOSE_SUSPEND),
        "Suspended": (_SAME, ()),
        "ExpiredSession": (_SAME, ()),
        "AuthFailed": (_SAME, ()),
    },
    "SocketReadable": {
        "Inactive": (_SAME, ()),
        "Connecting": (_SAME, ()),
        "Handshaking": (_SAME, (ReadConnectResponsePacket,)),
        "Handshaking[SUSPENDED]": (_SAME, (ReadConnectResponsePacket,)),
        "Authenticating.SASL": (_SAME, (ReadSocketPacket,)),
        "Authenticating.SASL[SUSPENDED]": (_SAME, (ReadSocketPacket,)),
        "Authenticating.INITIAL_AUTH": (_SAME, (ReadSocketPacket,)),
        "Authenticating.INITIAL_AUTH[SUSPENDED]": (_SAME, (ReadSocketPacket,)),
        "Connected": _RAISE,
        "ConnectedReadOnly": _RAISE,
        "Suspended": (_SAME, ()),
        "ExpiredSession": (_SAME, ()),
        "AuthFailed": (_SAME, ()),
    },
    "WakeUpSignaled": {
        "Inactive": (_SAME, (DrainWakeupPipe,)),
        "Connecting": (_SAME, (DrainWakeupPipe,)),
        "Handshaking": (_SAME, (DrainWakeupPipe,)),
        "Handshaking[SUSPENDED]": (_SAME, (DrainWakeupPipe,)),
        "Authenticating.SASL": (_SAME, (DrainWakeupPipe,)),
        "Authenticating.SASL[SUSPENDED]": (_SAME, (DrainWakeupPipe,)),
        "Authenticating.INITIAL_AUTH": (_SAME, (DrainWakeupPipe,)),
        "Authenticating.INITIAL_AUTH[SUSPENDED]": (_SAME, (DrainWakeupPipe,)),
        "Connected": _RAISE,
        "ConnectedReadOnly": _RAISE,
        "Suspended": (_SAME, (DrainWakeupPipe,)),
        "ExpiredSession": (_SAME, (DrainWakeupPipe,)),
        "AuthFailed": (_SAME, (DrainWakeupPipe,)),
    },
    "PollTimeoutExpired": {
        "Inactive": (_SAME, ()),
        "Connecting": (_SAME, ()),
        "Handshaking": (Connecting, _EFF_POLL_TIMEOUT),
        "Handshaking[SUSPENDED]": (Suspended, _EFF_POLL_TIMEOUT),
        "Authenticating.SASL": (Connecting, _EFF_POLL_TIMEOUT),
        "Authenticating.SASL[SUSPENDED]": (Suspended, _EFF_POLL_TIMEOUT),
        "Authenticating.INITIAL_AUTH": (Connecting, _EFF_POLL_TIMEOUT),
        "Authenticating.INITIAL_AUTH[SUSPENDED]": (
            Suspended,
            _EFF_POLL_TIMEOUT,
        ),
        "Connected": (_SAME, ()),
        "ConnectedReadOnly": (_SAME, ()),
        "Suspended": (_SAME, ()),
        "ExpiredSession": (_SAME, ()),
        "AuthFailed": (_SAME, ()),
    },
    "HeartbeatDeadlineReached": {
        "Inactive": (_SAME, ()),
        "Connecting": (_SAME, ()),
        "Handshaking": (_SAME, ()),
        "Handshaking[SUSPENDED]": (_SAME, ()),
        "Authenticating.SASL": (_SAME, ()),
        "Authenticating.SASL[SUSPENDED]": (_SAME, ()),
        "Authenticating.INITIAL_AUTH": (_SAME, ()),
        "Authenticating.INITIAL_AUTH[SUSPENDED]": (_SAME, ()),
        "Connected": (Connected, (SendHeartbeatPing,)),
        "ConnectedReadOnly": (ConnectedReadOnly, (SendHeartbeatPing,)),
        "Suspended": (_SAME, ()),
        "ExpiredSession": (_SAME, ()),
        "AuthFailed": (_SAME, ()),
    },
    "PingReceived": {
        "Inactive": (_SAME, ()),
        "Connecting": (_SAME, ()),
        "Handshaking": (_SAME, ()),
        "Handshaking[SUSPENDED]": (_SAME, ()),
        "Authenticating.SASL": (_SAME, ()),
        "Authenticating.SASL[SUSPENDED]": (_SAME, ()),
        "Authenticating.INITIAL_AUTH": (_SAME, ()),
        "Authenticating.INITIAL_AUTH[SUSPENDED]": (_SAME, ()),
        "Connected": (_SAME, ()),
        "ConnectedReadOnly": (_SAME, ()),
        "Suspended": (_SAME, ()),
        "ExpiredSession": (_SAME, ()),
        "AuthFailed": (_SAME, ()),
    },
}


class TestExhaustiveTransitionMatrix:
    def test_matrix_covers_all_states_and_events(self) -> None:
        state_types = {type(s) for _, s in _MATRIX_STATES}
        assert state_types == set(get_args(ClientState))
        assert len(_MATRIX_STATES) == 13

        event_types = {type(e) for _, e in _MATRIX_EVENTS}
        assert event_types == set(get_args(ClientEvent))
        assert len(_MATRIX_EVENTS) == 18

        assert set(_EXPECTED_MATRIX.keys()) == {n for n, _ in _MATRIX_EVENTS}
        state_names = {n for n, _ in _MATRIX_STATES}
        for ev_name, row in _EXPECTED_MATRIX.items():
            assert set(row.keys()) == state_names, ev_name

    @pytest.mark.parametrize(
        "state_name,state",
        _MATRIX_STATES,
        ids=[n for n, _ in _MATRIX_STATES],
    )
    @pytest.mark.parametrize(
        "event_name,event",
        _MATRIX_EVENTS,
        ids=[n for n, _ in _MATRIX_EVENTS],
    )
    def test_transition_matrix_cell(
        self,
        state_name: str,
        state: ClientState,
        event_name: str,
        event: ClientEvent,
    ) -> None:
        expected = _EXPECTED_MATRIX[event_name][state_name]
        if expected == _RAISE:
            with pytest.raises(InvalidStateTransitionError):
                transition(state, event)
            return

        assert isinstance(expected, tuple)
        exp_state, exp_effects = expected
        next_state, effects = transition(state, event)
        assert isinstance(effects, tuple)
        if exp_state == _SAME:
            assert next_state == state
        else:
            assert type(next_state) is exp_state
        assert tuple(type(e) for e in effects) == exp_effects

    def test_secondary_matrix_branches(self) -> None:
        # InvalidStateTransitionError reports both state and event names
        with pytest.raises(InvalidStateTransitionError) as exc_info:
            transition(
                Connected(session_id=1),
                ConnectResponseReceived(
                    connect_result=MagicMock(time_out=4000, session_id=1),
                ),
            )
        assert "ConnectResponseReceived" in str(exc_info.value)
        assert "Connected" in str(exc_info.value)

        # PollTimeoutExpired with ping_outstanding=True drops session
        s_drop, eff_drop = transition(
            Connected(host="127.0.0.1", port=2181, ping_outstanding=True),
            PollTimeoutExpired(),
        )
        assert isinstance(s_drop, Suspended)
        assert [type(e) for e in eff_drop] == [
            CloseSocket,
            LogStateTransition,
            LogStateTransition,
            FailPendingRequests,
            EmitKazooStateChange,
            InvalidateWatchers,
        ]

        # ConnectResponseReceived(time_out <= 0) expires immediately
        s_exp, eff_exp = transition(
            Handshaking(host="127.0.0.1", port=2181),
            ConnectResponseReceived(
                connect_result=Connect(0, 0, 0, 99, b"pwd", False)
            ),
        )
        assert isinstance(s_exp, ExpiredSession)
        assert [type(e) for e in eff_exp] == [
            LogStateTransition,
            EmitKazooStateChange,
            FailPendingRequests,
            ResetSessionData,
            CloseSocket,
        ]

        # CloseRequested while already closing is idempotent
        closing_state = Connected(
            host="127.0.0.1", port=2181, is_connecting_allowed=False
        )
        s_idem, eff_idem = transition(closing_state, CloseRequested())
        assert s_idem == closing_state
        assert eff_idem == ()

    def test_dataclasses_use_slots(self) -> None:
        instances = [
            Inactive(),
            Connecting(),
            Handshaking(host="127.0.0.1", port=2181),
            Authenticating(stage=AuthStage.SASL),
            Connected(),
            ConnectedReadOnly(),
            Suspended(),
            ExpiredSession(),
            AuthFailed(),
            SocketReadable(),
            WakeUpSignaled(),
            PollTimeoutExpired(),
            HeartbeatDeadlineReached(),
            PingReceived(),
            ReadSocketPacket(),
            EnqueueCloseRequest(),
            DrainWakeupPipe(),
            SendHeartbeatPing(),
            SASLConfig(mechanism="DIGEST-MD5"),
        ]
        for obj in instances:
            assert not hasattr(obj, "__dict__")


class TestWatcherManager:
    def test_update_and_collect_watchers(self) -> None:
        mgr = WatcherManager()
        w_data = MagicMock()
        w_child = MagicMock()
        w_pers = MagicMock()
        w_prec = MagicMock()

        mgr.update_for_response(GetData("/a/b", w_data))
        mgr.update_for_response(GetChildren("/a/b", w_child))
        mgr.update_for_response(
            AddWatch("/a/b", w_pers, AddWatchMode.PERSISTENT)
        )
        mgr.update_for_response(
            AddWatch("/a", w_prec, AddWatchMode.PERSISTENT_RECURSIVE)
        )

        # CHANGED_EVENT pops data watcher, keeps persistent & recursive
        collected = mgr.collect_for_event(CHANGED_EVENT, "/a/b")
        assert collected is not None
        assert set(collected) == {w_data, w_pers, w_prec}
        assert len(mgr.data_watchers["/a/b"]) == 0

        # CHILD_EVENT pops child watcher, keeps persistent (no recursive)
        collected_child = mgr.collect_for_event(CHILD_EVENT, "/a/b")
        assert collected_child is not None
        assert set(collected_child) == {w_child, w_pers}
        assert len(mgr.child_watchers["/a/b"]) == 0

        # DELETED_EVENT collects persistent & recursive
        collected_del = mgr.collect_for_event(DELETED_EVENT, "/a/b")
        assert collected_del is not None
        assert set(collected_del) == {w_pers, w_prec}

        # Unknown event returns None
        assert mgr.collect_for_event(999, "/a/b") is None

        # RemoveWatches ANY clears all tables for path
        mgr.update_for_response(RemoveWatches("/a/b", WatcherType.ANY))
        assert "/a/b" not in mgr.persistent_watchers

        # Reset returns remaining watchers and clears all tables
        remaining = mgr.reset()
        assert remaining == [w_prec]
        assert len(mgr.persistent_recursive_watchers) == 0

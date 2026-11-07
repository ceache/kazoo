from __future__ import annotations

from dataclasses import FrozenInstanceError
import logging
import selectors
import socket
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
from kazoo.loggingsupport import BLATHER
from kazoo.protocol.connection import (
    AUTH_XID,
    CHANGED_EVENT,
    CHILD_EVENT,
    CREATED_EVENT,
    DELETED_EVENT,
    FrameDecoder,
    RequestManager,
    WatcherManager,
)
from kazoo.protocol.sasl import SASLConfig, SASLExchange
from kazoo.protocol.serialization import (
    AddWatch,
    Auth,
    AuthResponse,
    Connect,
    GetChildren,
    GetData,
    PingResponse,
    RemoveWatches,
    ReplyHeader,
    ReplyPacket,
    SASLResponse,
    Watch,
    WatchPacket,
    int_int_long_struct,
    int_struct,
    reply_header_struct,
    write_buffer,
)
from kazoo.protocol.statemachine import (
    Authenticating,
    AuthFailed,
    AuthFailedEvent,
    AuthPacketCompleted,
    AuthStage,
    CloseRequested,
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
    ExpiredSession,
    FailPendingRequests,
    HandshakeCompleted,
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
    SendQueuedRequest,
    SessionClosedEvent,
    SessionEstablished,
    SessionExpiredEvent,
    SocketConnected,
    SocketReadable,
    StartRequested,
    StartSASLHandshake,
    StepSASLHandshake,
    StopConnectionLoop,
    Suspended,
    TriggerConnectionDrop,
    UpdateIOInterest,
    UpdateSendCapacity,
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
        assert effects == [
            LogStateTransition(BLATHER, "Skipping state change")
        ]

    def test_start_requested_then_connect_attempt_started(self) -> None:
        state = Inactive()
        s1, eff1 = transition(state, StartRequested())
        assert s1 == Connecting(attempt=0)
        assert eff1 == []

        s2, eff2 = transition(
            s1, ConnectAttemptStarted(host="127.0.0.1", port=2181)
        )
        assert s2 == Connecting(host="127.0.0.1", port=2181, attempt=1)
        assert eff2 == [
            LogStateTransition(BLATHER, "Skipping state change")
        ]

    def test_connecting_increments_attempt_without_effects(self) -> None:
        state = Connecting(host="10.0.0.1", port=2181, attempt=1)
        next_state, effects = transition(
            state, ConnectAttemptStarted(host="10.0.0.2", port=2181)
        )
        assert next_state == Connecting(host="10.0.0.2", port=2181, attempt=2)
        assert effects == []

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
            wakeup_interested=False,
        )
        expected_connect = Connect(0, 50, 10000, 123, b"pass", False)
        assert effects == [
            SendConnectPacket(connect=expected_connect, timeout=5.0)
        ]

    def test_handshaking_readable_yields_read_connect_response_packet(
        self,
    ) -> None:
        state = Handshaking(host="127.0.0.1", port=2181)
        next_state, effects = transition(state, SocketReadable())
        assert next_state == state
        assert effects == [ReadConnectResponsePacket()]

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
            state, ConnectResponseReceived(connect_result=connect_res, zxid=77)
        )
        assert next_state == Connected(
            host="127.0.0.1",
            port=2181,
            session_id=999,
            read_timeout=20.0,
            connect_timeout=15.0,
            wakeup_interested=True,
        )
        assert effects == [
            UpdateSessionData(
                session_id=999,
                session_passwd=b"newpass",
                protocol_version=0,
                last_zxid=77,
            ),
            ConfigureTimeouts(
                read_timeout=20.0,
                connect_timeout=15.0,
            ),
            ConfigureReadOnlyMode(read_only=False),
            UpdateIOInterest(
                interest_mask=selectors.EVENT_READ, target="wakeup"
            ),
            LogStateTransition(
                logging.INFO,
                "Zookeeper connection established, state: %s",
                (KeeperState.CONNECTED,),
            ),
            ResolveConnectResult(success=True),
            EmitKazooStateChange(KazooState.CONNECTED),
            SessionEstablished(),
        ]

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
        assert effects == [
            LogStateTransition(
                logging.WARNING,
                "Zookeeper session closed, state: %s",
                (KeeperState.EXPIRED_SESSION,),
            ),
            EmitKazooStateChange(KazooState.LOST),
            FailPendingRequests(KeeperState.EXPIRED_SESSION),
            ResetSessionData(),
            TriggerConnectionDrop("Session has expired"),
        ]

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
        assert effects == [
            LogStateTransition(
                logging.WARNING,
                "Zookeeper session closed, state: %s",
                (KeeperState.EXPIRED_SESSION,),
            ),
            FailPendingRequests(KeeperState.EXPIRED_SESSION),
            ResetSessionData(),
            TriggerConnectionDrop("Session has expired"),
        ]

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
            wakeup_interested=False,
        )
        assert effects == [
            UpdateSessionData(
                session_id=555,
                session_passwd=b"pwd",
                protocol_version=0,
                last_zxid=None,
            ),
            ConfigureTimeouts(read_timeout=10.0, connect_timeout=15.0),
            ConfigureReadOnlyMode(read_only=False),
            UpdateIOInterest(interest_mask=0, target="wakeup"),
            StartSASLHandshake(host="127.0.0.1"),
        ]

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
            wakeup_interested=False,
        )
        assert effects == [
            UpdateSessionData(
                session_id=555,
                session_passwd=b"pwd",
                protocol_version=0,
                last_zxid=None,
            ),
            ConfigureTimeouts(read_timeout=10.0, connect_timeout=15.0),
            ConfigureReadOnlyMode(read_only=False),
            UpdateIOInterest(interest_mask=0, target="wakeup"),
            SendAuthPacket(scheme="digest", credential="u1:p1"),
        ]

    def test_handshaking_poll_timeout_drops_connection(self) -> None:
        state = Handshaking(host="127.0.0.1", port=2181)
        next_state, effects = transition(state, PollTimeoutExpired())
        assert next_state == state
        assert effects == [
            TriggerConnectionDrop(reason="socket time-out during handshake")
        ]

    def test_handshaking_connection_lost_transitions_to_connecting(
        self,
    ) -> None:
        state = Handshaking(
            host="127.0.0.1",
            port=2181,
            session_id=1,
            attempt=1,
            kazoo_state=KazooState.LOST,
            wakeup_interested=False,
        )
        next_state, effects = transition(
            state, ConnectionLostEvent(reason="drop")
        )
        assert next_state == Connecting(host="127.0.0.1", port=2181, attempt=1)
        assert effects == []

    def test_connecting_to_connected_rw(self) -> None:
        state = Connecting(host="127.0.0.1", port=2181, attempt=1)
        next_state, effects = transition(
            state,
            HandshakeCompleted(
                host="127.0.0.1",
                port=2181,
                session_id=999,
                read_timeout=6.6,
                connect_timeout=3.3,
                read_only=False,
            ),
        )
        assert next_state == Connected(
            host="127.0.0.1",
            port=2181,
            session_id=999,
            read_timeout=6.6,
            connect_timeout=3.3,
            wakeup_interested=True,
        )
        assert effects == [
            UpdateIOInterest(
                interest_mask=selectors.EVENT_READ, target="wakeup"
            ),
            LogStateTransition(
                logging.INFO,
                "Zookeeper connection established, state: %s",
                (KeeperState.CONNECTED,),
            ),
            ResolveConnectResult(success=True),
            EmitKazooStateChange(KazooState.CONNECTED),
            SessionEstablished(),
        ]

    def test_connecting_to_connected_ro(self) -> None:
        state = Connecting(host="127.0.0.1", port=2181, attempt=1)
        next_state, effects = transition(
            state,
            HandshakeCompleted(
                host="127.0.0.1",
                port=2181,
                session_id=999,
                read_timeout=6.6,
                connect_timeout=3.3,
                read_only=True,
            ),
        )
        assert next_state == ConnectedReadOnly(
            host="127.0.0.1",
            port=2181,
            session_id=999,
            read_timeout=6.6,
            connect_timeout=3.3,
            wakeup_interested=True,
        )
        assert effects == [
            UpdateIOInterest(
                interest_mask=selectors.EVENT_READ, target="wakeup"
            ),
            LogStateTransition(
                logging.INFO,
                "Zookeeper connection established, state: %s",
                (KeeperState.CONNECTED_RO,),
            ),
            ResolveConnectResult(success=True),
            EmitKazooStateChange(KazooState.CONNECTED),
            SessionEstablished(),
        ]

    def test_connecting_to_authenticating_sasl_and_auth_packets(self) -> None:
        state = Connecting(host="127.0.0.1", port=2181, attempt=1)
        s1, eff1 = transition(
            state,
            HandshakeCompleted(
                host="127.0.0.1",
                port=2181,
                session_id=999,
                read_timeout=6.0,
                connect_timeout=3.0,
                read_only=False,
                has_sasl=True,
                auth_data=(("digest", "u1:p1"), ("digest", "u2:p2")),
            ),
        )
        assert s1 == Authenticating(
            stage=AuthStage.SASL,
            host="127.0.0.1",
            port=2181,
            session_id=999,
            read_timeout=6.0,
            connect_timeout=3.0,
            read_only=False,
            pending_auth=(("digest", "u1:p1"), ("digest", "u2:p2")),
            wakeup_interested=False,
        )
        assert eff1 == [
            UpdateIOInterest(interest_mask=0, target="wakeup"),
            StartSASLHandshake(host="127.0.0.1"),
        ]

        # Server challenge -> StepSASLHandshake
        s2, eff2 = transition(s1, SASLChallengeReceived(challenge=b"chal1"))
        assert s2 == s1
        assert eff2 == [StepSASLHandshake(challenge=b"chal1")]

        # SASL completes -> enters INITIAL_AUTH stage and sends first Auth
        s3, eff3 = transition(s2, SASLCompleted())
        assert isinstance(s3, Authenticating)
        assert s3.stage == AuthStage.INITIAL_AUTH
        assert s3.pending_auth == (("digest", "u2:p2"),)
        assert eff3 == [SendAuthPacket(scheme="digest", credential="u1:p1")]

        # First Auth packet reply arrives -> sends second pending Auth packet
        s4, eff4 = transition(s3, AuthPacketCompleted())
        assert isinstance(s4, Authenticating)
        assert s4.stage == AuthStage.INITIAL_AUTH
        assert s4.pending_auth == ()
        assert eff4 == [SendAuthPacket(scheme="digest", credential="u2:p2")]

        # Second Auth packet reply arrives -> Connected!
        s5, eff5 = transition(s4, AuthPacketCompleted())
        assert s5 == Connected(
            host="127.0.0.1",
            port=2181,
            session_id=999,
            read_timeout=6.0,
            connect_timeout=3.0,
            wakeup_interested=True,
        )
        assert eff5 == [
            UpdateIOInterest(
                interest_mask=selectors.EVENT_READ, target="wakeup"
            ),
            LogStateTransition(
                logging.INFO,
                "Zookeeper connection established, state: %s",
                (KeeperState.CONNECTED,),
            ),
            ResolveConnectResult(success=True),
            EmitKazooStateChange(KazooState.CONNECTED),
            SessionEstablished(),
        ]

    def test_connected_to_suspended_on_connection_lost_disables_wakeup(
        self,
    ) -> None:
        state = Connected(
            host="127.0.0.1", port=2181, session_id=42, wakeup_interested=True
        )
        next_state, effects = transition(
            state, ConnectionLostEvent(reason="socket broken")
        )
        assert next_state == Suspended(
            host="127.0.0.1", port=2181, session_id=42, attempt=1
        )
        assert effects == [
            UpdateIOInterest(interest_mask=0, target="wakeup"),
            LogStateTransition(logging.INFO, "Zookeeper connection lost"),
            FailPendingRequests(KeeperState.CONNECTING),
            EmitKazooStateChange(KazooState.SUSPENDED),
            InvalidateWatchers(),
        ]

    def test_connected_to_suspended_skips_wakeup_disable_if_already_disabled(
        self,
    ) -> None:
        state = Connected(
            host="127.0.0.1",
            port=2181,
            session_id=42,
            wakeup_interested=False,
        )
        next_state, effects = transition(
            state, ConnectionLostEvent(reason="socket broken")
        )
        assert next_state == Suspended(
            host="127.0.0.1", port=2181, session_id=42, attempt=1
        )
        assert effects == [
            LogStateTransition(logging.INFO, "Zookeeper connection lost"),
            FailPendingRequests(KeeperState.CONNECTING),
            EmitKazooStateChange(KazooState.SUSPENDED),
            InvalidateWatchers(),
        ]

    def test_connected_ro_to_suspended_on_rw_server_found(self) -> None:
        state = ConnectedReadOnly(
            host="127.0.0.1", port=2181, session_id=42, wakeup_interested=True
        )
        next_state, effects = transition(
            state, RWServerFoundEvent(server=("127.0.0.2", 2181))
        )
        assert next_state == Suspended(
            host="127.0.0.2", port=2181, session_id=42, attempt=1
        )
        assert effects == [
            UpdateIOInterest(interest_mask=0, target="wakeup"),
            LogStateTransition(logging.INFO, "Zookeeper connection lost"),
            FailPendingRequests(KeeperState.CONNECTING),
            EmitKazooStateChange(KazooState.SUSPENDED),
            InvalidateWatchers(),
        ]

    def test_auth_states_connect_attempt_started(self) -> None:
        for stage in (AuthStage.SASL, AuthStage.INITIAL_AUTH):
            # 1. kazoo_state == LOST -> Connecting
            s_lost = Authenticating(
                stage=stage,
                host="127.0.0.1",
                port=2181,
                session_id=1,
                kazoo_state=KazooState.LOST,
                wakeup_interested=False,
            )
            next_s1, eff1 = transition(
                s_lost, ConnectAttemptStarted(host="127.0.0.2", port=2182)
            )
            assert next_s1 == Connecting(
                host="127.0.0.2", port=2182, attempt=1
            )
            assert eff1 == []

            # 2. kazoo_state == SUSPENDED -> Suspended
            s_susp = Authenticating(
                stage=stage,
                host="127.0.0.1",
                port=2181,
                session_id=1,
                kazoo_state=KazooState.SUSPENDED,
                wakeup_interested=False,
            )
            next_s2, eff2 = transition(
                s_susp, ConnectAttemptStarted(host="127.0.0.2", port=2182)
            )
            assert next_s2 == Suspended(
                host="127.0.0.2", port=2182, session_id=1, attempt=1
            )
            assert eff2 == []

    def test_auth_sasl_connection_lost_transitions_to_auth_failed(
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
        assert isinstance(next_state, AuthFailed)
        assert isinstance(next_state.error, AuthFailedError)
        assert str(next_state.error) == "Connection dropped in SASL"
        assert effects == [
            LogStateTransition(
                logging.WARNING,
                "AUTH_FAILED closing (connection dropped in SASL): %s",
                ("Connection reset by peer",),
            ),
            ResolveConnectResult(success=False, exception=next_state.error),
            EmitKazooStateChange(KazooState.LOST),
            FailPendingRequests(KeeperState.AUTH_FAILED),
            ResetSessionData(),
            StopConnectionLoop(),
        ]

    def test_auth_sasl_connection_lost_initial_lost_no_emit(
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
        assert isinstance(next_state, AuthFailed)
        assert isinstance(next_state.error, AuthFailedError)
        assert str(next_state.error) == "Connection dropped in SASL"
        assert effects == [
            LogStateTransition(
                logging.WARNING,
                "AUTH_FAILED closing (connection dropped in SASL): %s",
                ("Connection reset by peer",),
            ),
            ResolveConnectResult(success=False, exception=next_state.error),
            FailPendingRequests(KeeperState.AUTH_FAILED),
            ResetSessionData(),
            StopConnectionLoop(),
        ]

    def test_auth_initial_connection_lost_transitions_to_connecting(
        self,
    ) -> None:
        state_lost = Authenticating(
            stage=AuthStage.INITIAL_AUTH,
            host="127.0.0.1",
            port=2181,
            session_id=1,
            kazoo_state=KazooState.LOST,
            wakeup_interested=False,
        )
        next_s1, eff1 = transition(
            state_lost, ConnectionLostEvent(reason="network drop")
        )
        assert next_s1 == Connecting(host="127.0.0.1", port=2181, attempt=1)
        assert eff1 == []

        state_susp = Authenticating(
            stage=AuthStage.INITIAL_AUTH,
            host="127.0.0.1",
            port=2181,
            session_id=1,
            kazoo_state=KazooState.SUSPENDED,
            wakeup_interested=False,
        )
        next_s2, eff2 = transition(
            state_susp, ConnectionLostEvent(reason="network drop")
        )
        assert next_s2 == Suspended(
            host="127.0.0.1", port=2181, session_id=1, attempt=1
        )
        assert eff2 == []

    def test_terminal_states_ignore_connection_lost(self) -> None:
        for term_state in (
            Inactive(),
            ExpiredSession(session_id=10),
            AuthFailed(error=AuthFailedError()),
        ):
            next_state, effects = transition(
                term_state, ConnectionLostEvent(reason="drop")
            )
            assert next_state == term_state
            assert effects == [
                LogStateTransition(BLATHER, "Skipping state change")
            ]

    def test_suspended_to_expired_session(self) -> None:
        state = Suspended(host="127.0.0.1", port=2181, session_id=42)
        next_state, effects = transition(
            state, SessionExpiredEvent(session_id=42)
        )
        assert next_state == ExpiredSession(session_id=42)
        assert effects == [
            LogStateTransition(
                logging.INFO,
                "Zookeeper session closed, state: %s",
                (KeeperState.EXPIRED_SESSION,),
            ),
            EmitKazooStateChange(KazooState.LOST),
            FailPendingRequests(KeeperState.EXPIRED_SESSION),
            ResetSessionData(),
        ]

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
        assert effects == [
            LogStateTransition(
                logging.INFO,
                "Zookeeper session closed, state: %s",
                (KeeperState.CLOSED,),
            ),
            ResolveConnectResult(
                success=False,
                exception=effects[1].exception,  # type: ignore[union-attr]
            ),
            EmitKazooStateChange(KazooState.LOST),
            FailPendingRequests(KeeperState.CLOSED),
            ResetSessionData(),
            StopConnectionLoop(),
        ]
        assert isinstance(
            effects[1].exception,  # type: ignore[union-attr]
            ConnectionClosedError,
        )

    def test_inactive_session_closed_is_idempotent(self) -> None:
        state = Inactive()
        next_state, effects = transition(state, SessionClosedEvent())
        assert next_state == Inactive()
        assert effects == []

    def test_close_requested_in_connected_forbids_reconnect_on_drop(
        self,
    ) -> None:
        state = Connected(
            host="127.0.0.1",
            port=2181,
            session_id=42,
            wakeup_interested=False,
        )
        s1, eff1 = transition(state, CloseRequested())
        assert isinstance(s1, Connected)
        assert s1.is_connecting_allowed is False
        assert s1.wakeup_interested is True
        assert eff1 == [
            UpdateIOInterest(
                interest_mask=selectors.EVENT_READ, target="wakeup"
            )
        ]

        s2, _ = transition(s1, ConnectionLostEvent(reason="dropped"))
        assert isinstance(s2, Suspended)
        assert s2.is_connecting_allowed is False

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

        # 1. SocketReadable -> ReadSocketPacket
        s1, eff1 = transition(state, SocketReadable())
        assert s1 == state
        assert eff1 == [ReadSocketPacket()]

        # 2. WakeUpSignaled -> SendQueuedRequest
        s2, eff2 = transition(state, WakeUpSignaled())
        assert s2 == state
        assert eff2 == [SendQueuedRequest()]

        # 3. HeartbeatDeadlineReached -> ping_outstanding=True
        s3, eff3 = transition(state, HeartbeatDeadlineReached())
        assert isinstance(s3, Connected)
        assert s3.ping_outstanding is True
        assert eff3 == [SendHeartbeatPing()]

        # 4. PingReceived -> ping_outstanding=False
        s4, eff4 = transition(s3, PingReceived())
        assert isinstance(s4, Connected)
        assert s4.ping_outstanding is False
        assert eff4 == []

        # 5. PollTimeoutExpired while ping_outstanding=True
        s5, eff5 = transition(s3, PollTimeoutExpired())
        assert isinstance(s5, Connected)
        assert s5.ping_outstanding is False
        assert eff5 == [
            TriggerConnectionDrop(
                reason="outstanding heartbeat ping not received"
            )
        ]

    def test_update_send_capacity_toggles_io_interest(self) -> None:
        state = Connected(wakeup_interested=True)

        # Same capacity -> no-op
        s1, eff1 = transition(state, UpdateSendCapacity(can_send=True))
        assert s1 == state
        assert eff1 == []

        # Capacity exhausted -> unregister wakeup read interest (mask=0)
        s2, eff2 = transition(state, UpdateSendCapacity(can_send=False))
        assert isinstance(s2, Connected)
        assert s2.wakeup_interested is False
        assert eff2 == [UpdateIOInterest(interest_mask=0, target="wakeup")]

        # Capacity restored -> re-register wakeup read interest (EVENT_READ)
        s3, eff3 = transition(s2, UpdateSendCapacity(can_send=True))
        assert isinstance(s3, Connected)
        assert s3.wakeup_interested is True
        assert eff3 == [
            UpdateIOInterest(
                interest_mask=selectors.EVENT_READ, target="wakeup"
            )
        ]

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
            assert eff1 == [SendQueuedRequest()]

            s2, eff2 = transition(
                terminal_state,
                SocketReadable(),
            )
            assert s2 == terminal_state
            assert eff2 == [ReadSocketPacket()]

        for in_flight_state in (
            Inactive(),
            Connecting(),
            Handshaking(),
            Authenticating(stage=AuthStage.SASL),
            Authenticating(stage=AuthStage.INITIAL_AUTH),
            Suspended(),
        ):
            s_inf, eff_inf = transition(
                in_flight_state,
                WakeUpSignaled(),
            )
            assert s_inf == in_flight_state
            assert eff_inf == [DrainWakeupPipe()]


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
        with pytest.raises(ConfigurationError, match="mechanism must be"):
            SASLConfig.from_client_options({"mechanism": "INVALID"})
        with pytest.raises(ConfigurationError, match="requires a password"):
            SASLConfig.from_client_options(
                {"mechanism": "DIGEST-MD5", "username": "alice"}
            )
        with pytest.raises(ConfigurationError, match="requires a username"):
            SASLConfig.from_client_options(
                {"mechanism": "PLAIN", "password": "pass"}
            )

    def test_sasl_exchange_error_handling(self) -> None:
        class ErrorSASLClient:
            @property
            def complete(self) -> bool:
                return False

            def process(self, challenge: bytes | None = None) -> bytes | None:
                raise Exception("pure-sasl internal failure")

            def dispose(self) -> None:
                pass

        cfg = SASLConfig(mechanism="PLAIN", username="u", password="p")
        ex = SASLExchange(
            cfg, "127.0.0.1", client_factory=lambda **kw: ErrorSASLClient()
        )
        with pytest.raises(SASLException, match="SASL step failed"):
            ex.next_token()


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
        client.send_event(
            HandshakeCompleted(
                host="127.0.0.1", port=2181, session_id=100, read_only=False
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

    def test_selector_interest_updates_via_client_events(self) -> None:
        client = KazooClient(concurrent_request_limit=1)
        conn = client._connection
        s_zk_a, s_zk_b = socket.socketpair()
        s_wk_a, s_wk_b = socket.socketpair()
        try:
            conn._socket = s_zk_a
            conn._read_sock = s_wk_a
            conn._write_sock = s_wk_b
            conn._setup_selector()
            assert conn._selector is not None
            assert conn._wakeup_registered is True

            client.send_event(
                HandshakeCompleted(
                    host="127.0.0.1", port=2181, session_id=1, read_only=False
                )
            )
            assert isinstance(client.state_machine, Connected)
            assert conn._wakeup_registered is True

            # Gate sending via state machine event
            client.send_event(UpdateSendCapacity(can_send=False))
            assert conn._wakeup_registered is False

            # Write a wakeup byte; since wakeup is unregistered, poll ignores
            s_wk_b.send(b"\0")
            ready = conn._select(timeout=0.0)
            assert ready == set()

            # Ungate sending via state machine event
            client.send_event(UpdateSendCapacity(can_send=True))
            assert conn._wakeup_registered is True

            ready = conn._select(timeout=0.0)
            assert ready == {"wakeup"}
        finally:
            conn._close_selector()
            s_zk_a.close()
            s_zk_b.close()
            s_wk_a.close()
            s_wk_b.close()

    def test_wakeup_pipe_does_not_spin_during_handshaking(self) -> None:
        client = KazooClient()
        conn = client._connection
        s_zk_a, s_zk_b = socket.socketpair()
        s_wk_a, s_wk_b = socket.socketpair()
        try:
            conn._socket = s_zk_a
            conn._read_sock = s_wk_a
            conn._write_sock = s_wk_b
            conn._setup_selector()

            client.send_event(StartRequested())
            client.send_event(
                ConnectAttemptStarted(host="127.0.0.1", port=2181)
            )
            # Even if wakeup was registered before, WakeUpSignaled drains/masks
            conn._update_io_interest(selectors.EVENT_READ, "wakeup")
            s_wk_b.send(b"\0")
            assert conn._select(timeout=0.0) == {"wakeup"}

            # Emitting WakeUpSignaled in Connecting unregisters wakeup and
            # drains the stray byte so the next _select(0.0) does not spin!
            client.send_event(WakeUpSignaled())
            assert conn._wakeup_registered is False
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

            conn._socket = FakeSSLSocket()  # type: ignore[assignment]
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
        conn = client._connection
        async_result = client.handler.async_result()
        client._pending.append(
            (Auth(0, "digest", "u:p"), async_result, AUTH_XID)
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
        conn = client._connection
        async_result = client.handler.async_result()
        client._pending.append(
            (Auth(0, "digest", "u:p"), async_result, AUTH_XID)
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

    def test_sasl_connection_dropped_fails_start_with_auth_failed_error(
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
        client.send_event(
            HandshakeCompleted(
                host="127.0.0.1", port=2181, session_id=1, has_sasl=True
            )
        )
        assert isinstance(client.state_machine, Authenticating)
        assert client.state_machine.stage == AuthStage.SASL
        client.send_event(
            ConnectionLostEvent(reason="Connection reset by peer")
        )
        assert isinstance(client.state_machine, AuthFailed)
        assert isinstance(client.state_machine.error, AuthFailedError)
        assert str(client.state_machine.error) == "Connection dropped in SASL"

    def test_execute_effect_session_established_resets_retry(
        self,
    ) -> None:
        client = KazooClient()
        conn = client._connection
        mock_retry = MagicMock()
        conn._current_retry = mock_retry
        assert conn._is_session_active is False

        client.send_event(
            HandshakeCompleted(
                host="127.0.0.1", port=2181, session_id=1, read_only=False
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
                last_zxid=100,
            )
        )
        assert client._session_id == 888
        assert client._session_passwd == b"mysecret"
        assert client._protocol_version == 1
        assert client.last_zxid == 100

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
        decoder = FrameDecoder()
        assert decoder.bytes_needed() == 4

        # Build two ReplyHeader frames
        f1 = reply_header_struct.pack(1, 100, 0)
        f2 = reply_header_struct.pack(2, 101, 0)
        wire = (
            int_struct.pack(len(f1))
            + f1
            + int_struct.pack(len(f2))
            + f2
        )

        # Feed 1 byte at a time for the first frame
        first_frame_wire = wire[: 4 + len(f1)]
        for b in first_frame_wire[:-1]:
            decoder.feed(bytes([b]))
            assert decoder.pop_packet() is None
        decoder.feed(first_frame_wire[-1:])
        p1 = decoder.pop_packet()
        assert isinstance(p1, ReplyPacket)
        assert p1.header == ReplyHeader(xid=1, zxid=100, err=0)
        assert decoder.pop_packet() is None

        # Feed both frames concatenated at once
        decoder.feed(wire)
        p_both_1 = decoder.pop_packet()
        p_both_2 = decoder.pop_packet()
        assert isinstance(p_both_1, ReplyPacket)
        assert isinstance(p_both_2, ReplyPacket)
        assert p_both_1.header == ReplyHeader(xid=1, zxid=100, err=0)
        assert p_both_2.header == ReplyHeader(xid=2, zxid=101, err=0)
        assert decoder.pop_packet() is None

    def test_frame_decoder_connect_and_typed_packets(self) -> None:
        decoder = FrameDecoder()
        body = bytearray()
        body.extend(int_int_long_struct.pack(0, 30000, 12345))
        body.extend(write_buffer(b"password12345678"))
        body.extend([0])
        wire = int_struct.pack(len(body)) + bytes(body)

        decoder.feed(wire[:3])
        assert decoder.pop_packet(is_connect=True) is None
        decoder.feed(wire[3:])
        conn_res = decoder.pop_packet(is_connect=True)
        assert isinstance(conn_res, Connect)
        assert conn_res.session_id == 12345

        # Ping (-2), Auth (-4), SASL (xid=3)
        f_ping = reply_header_struct.pack(-2, 200, 0)
        f_auth = reply_header_struct.pack(-4, 201, 0)
        f_sasl = reply_header_struct.pack(3, 202, 0) + write_buffer(b"tok")
        for f in (f_ping, f_auth, f_sasl):
            decoder.feed(int_struct.pack(len(f)) + f)

        assert decoder.pop_packet() == PingResponse(zxid=200, err=0)
        assert decoder.pop_packet() == AuthResponse(zxid=201, err=0)
        assert decoder.pop_packet(is_sasl=True) == SASLResponse(
            xid=3, zxid=202, err=0, challenge=b"tok"
        )

        decoder.reset()
        with pytest.raises(ConnectionDropped, match="Invalid negative frame"):
            decoder.feed(int_struct.pack(-5))

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


class TestExhaustiveTransitionMatrix:
    def test_invalid_transitions_abort(self) -> None:
        with pytest.raises(InvalidStateTransitionError) as exc_info:
            transition(
                Connected(session_id=1),
                ConnectResponseReceived(
                    connect_result=MagicMock(time_out=4000, session_id=1),
                    zxid=1,
                ),
            )
        assert "ConnectResponseReceived" in str(exc_info.value)
        assert "Connected" in str(exc_info.value)

        with pytest.raises(InvalidStateTransitionError):
            transition(Inactive(), SASLChallengeReceived(challenge=b"token"))

        with pytest.raises(InvalidStateTransitionError):
            transition(
                Authenticating(stage=AuthStage.INITIAL_AUTH),
                SASLChallengeReceived(challenge=b"token"),
            )

        with pytest.raises(InvalidStateTransitionError):
            transition(Connected(session_id=1), SocketConnected())

        with pytest.raises(InvalidStateTransitionError):
            transition(Handshaking(), PingReceived())

        with pytest.raises(InvalidStateTransitionError):
            transition(Connecting(), AuthPacketCompleted())

    def test_benign_skip_matches(self) -> None:
        # UpdateSendCapacity in non-connected states
        for state in (
            Inactive(),
            Connecting(),
            Handshaking(),
            Authenticating(stage=AuthStage.SASL),
            Authenticating(stage=AuthStage.INITIAL_AUTH),
            Suspended(),
            ExpiredSession(),
            AuthFailed(),
        ):
            st, effs = transition(state, UpdateSendCapacity(can_send=True))
            assert st == state
            assert effs == []

        # WakeUpSignaled in non-connected states emits DrainWakeupPipe
        for state in (
            Inactive(),
            Connecting(),
            Handshaking(),
            Authenticating(stage=AuthStage.SASL),
            Authenticating(stage=AuthStage.INITIAL_AUTH),
            Suspended(),
        ):
            st, effs = transition(state, WakeUpSignaled())
            assert st == state
            assert effs == [DrainWakeupPipe()]

        # PollTimeoutExpired in inactive states
        for state in (
            Inactive(),
            Connecting(),
            Suspended(),
            ExpiredSession(),
            AuthFailed(),
        ):
            st, effs = transition(state, PollTimeoutExpired())
            assert st == state
            assert effs == []

        # HeartbeatDeadlineReached in non-connected states
        for state in (
            Inactive(),
            Connecting(),
            Handshaking(),
            Authenticating(stage=AuthStage.SASL),
            Authenticating(stage=AuthStage.INITIAL_AUTH),
            Suspended(),
            ExpiredSession(),
            AuthFailed(),
        ):
            st, effs = transition(state, HeartbeatDeadlineReached())
            assert st == state
            assert effs == []

        # RWServerFoundEvent in non-connected states
        for state in (
            Inactive(),
            Connecting(),
            Handshaking(),
            Authenticating(stage=AuthStage.SASL),
            Authenticating(stage=AuthStage.INITIAL_AUTH),
            Suspended(),
            ExpiredSession(),
            AuthFailed(),
        ):
            st, effs = transition(
                state, RWServerFoundEvent(server=("127.0.0.1", 2181))
            )
            assert st == state
            assert effs == []

        # SocketReadable in non-active states
        for state in (Inactive(), Connecting(), Suspended()):
            st, effs = transition(state, SocketReadable())
            assert st == state
            assert effs == []

        # PingReceived in terminal/suspended states
        for state in (Inactive(), Suspended(), ExpiredSession(), AuthFailed()):
            st, effs = transition(state, PingReceived())
            assert st == state
            assert effs == []

        # AuthPacketCompleted in connected states
        for state in (Connected(), ConnectedReadOnly(), Inactive()):
            st, effs = transition(state, AuthPacketCompleted())
            assert st == state
            assert effs == []

    def test_dataclasses_use_slots(self) -> None:
        instances = [
            Inactive(),
            Connecting(),
            Handshaking(),
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
            SendQueuedRequest(),
            UpdateIOInterest(interest_mask=1, target="wakeup"),
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

from __future__ import annotations

import logging
from typing import Any

import pytest

from kazoo.exceptions import (
    AuthFailedError,
    ConnectionDropped,
    ConnectionLoss,
    SessionExpiredError,
    SystemZookeeperError,
)
from kazoo.protocol.paths import unchroot
from kazoo.protocol.serialization import (
    AuthResponse,
    Close,
    GetData,
    PingResponse,
    ReplyHeader,
    ResponsePacket,
    SASLResponse,
    Transaction,
    WatchPacket,
)
from kazoo.protocol.session import (
    CHANGED_EVENT,
    CLOSE_RESPONSE,
    PacketRouter,
    RequestManager,
    RWProbeSchedule,
    RWServerAvailable,
    WatcherManager,
    _exception_to_event,
)
from kazoo.protocol.statemachine import (
    Authenticating,
    AuthFailedEvent,
    AuthPacketCompleted,
    AuthStage,
    ClientEvent,
    ClientState,
    Connected,
    ConnectionLostEvent,
    Handshaking,
    RWServerFoundEvent,
    SASLChallengeReceived,
    SessionClosedEvent,
    SessionExpiredEvent,
)
from kazoo.protocol.states import Callback, EventType, KeeperState


class _FakeCompletion:
    """Minimal ICompletion test double (no IHandler needed)."""

    def __init__(self) -> None:
        self.value: Any = None
        self.exc: Exception | None = None
        self.done: bool = False

    def set(self, value: Any, /) -> None:
        self.value = value
        self.done = True

    def set_exception(self, exception: Exception, /) -> None:
        self.exc = exception
        self.done = True


class TestRWProbeSchedule:
    def test_empty_hosts_returns_none(self) -> None:
        sched = RWProbeSchedule([])
        assert sched.next_host(10.0, lambda: 0.0) is None

    def test_host_rotation_exponential_backoff_cap_and_reset(self) -> None:
        h1 = ("h1", "10.0.0.1", 2181)
        h2 = ("h2", "10.0.0.2", 2182)
        sched = RWProbeSchedule([h1, h2])

        # First pass iterates without backoff.
        assert sched.next_host(100.0, lambda: 0.0) == h1
        sched.finish_probe(100.0)
        assert sched.next_host(100.0, lambda: 0.0) == h2
        sched.finish_probe(100.0)

        # Backoff window (0.5s): skipped before 100.5, advances at 100.5.
        assert sched.next_host(100.4, lambda: 0.0) is None
        assert sched.next_host(100.5, lambda: 0.0) == h1
        assert sched.delay == 1.0
        sched.finish_probe(100.5)
        assert sched.next_host(100.5, lambda: 0.0) == h2
        sched.finish_probe(100.5)

        # Second backoff window (1.0s + 0.25s jitter = 101.75).
        assert sched.next_host(101.7, lambda: 0.25) is None
        assert sched.next_host(101.75, lambda: 0.25) == h1
        assert sched.delay == 2.0

        # Finding a RW host resets delay to 0.5.
        sched.mark_found()
        assert sched.delay == 0.5


class TestPacketRouter:
    @staticmethod
    def _make_router(
        state_box: list[ClientState],
        *,
        chroot: str = "",
    ) -> tuple[
        PacketRouter,
        RequestManager,
        WatcherManager,
        list[ClientEvent],
        list[Callback],
        list[int],
    ]:
        requests = RequestManager()
        watchers = WatcherManager()
        events: list[ClientEvent] = []
        dispatched: list[Callback] = []
        zxids: list[int] = []
        router = PacketRouter(
            requests=requests,
            watchers=watchers,
            get_state=lambda: state_box[0],
            set_last_zxid=zxids.append,
            unchroot=lambda p: unchroot(chroot, p),
            emit_event=events.append,
            dispatch_watch=dispatched.append,
            logger=logging.getLogger("kazoo.test.session"),
        )
        return router, requests, watchers, events, dispatched, zxids

    def test_pre_session_guards_reject_session_packets(self) -> None:
        state_box: list[ClientState] = [
            Handshaking(host="127.0.0.1", port=2181)
        ]
        router, _, _, _, _, _ = self._make_router(state_box)

        for pkt in (
            PingResponse(zxid=1, err=0),
            WatchPacket(zxid=1, type=CHANGED_EVENT, state=3, path="/a"),
            AuthResponse(zxid=1, err=0),
            SASLResponse(xid=1, zxid=1, err=0, challenge=b"tok"),
            ResponsePacket(
                header=ReplyHeader(1, 1, 0),
                request=GetData("/a", None),
                async_object=None,
            ),
        ):
            with pytest.raises(ConnectionDropped, match="Unexpected"):
                router.process_packet(pkt)

    def test_sasl_and_auth_routing_and_error_classification(self) -> None:
        state_box: list[ClientState] = [
            Authenticating(stage=AuthStage.SASL, host="127.0.0.1")
        ]
        router, requests, _, events, _, zxids = self._make_router(state_box)

        router.process_packet(
            SASLResponse(xid=1, zxid=10, err=0, challenge=b"c1")
        )
        assert zxids == [10]
        assert events == [SASLChallengeReceived(challenge=b"c1")]

        with pytest.raises(ConnectionDropped, match="Transient SASL error"):
            router.process_packet(
                SASLResponse(xid=2, zxid=11, err=ConnectionLoss.code)
            )
        with pytest.raises(AuthFailedError, match="SASL error -999"):
            router.process_packet(SASLResponse(xid=3, zxid=12, err=-999))

        # Switch to INITIAL_AUTH stage for AuthResponse tests.
        state_box[0] = Authenticating(
            stage=AuthStage.INITIAL_AUTH, host="127.0.0.1"
        )
        ok_comp = _FakeCompletion()
        requests.auth_pending.append(ok_comp)
        router.process_packet(AuthResponse(zxid=20, err=0))
        assert ok_comp.value is True
        assert events[-1] == AuthPacketCompleted()

        transient_comp = _FakeCompletion()
        requests.auth_pending.append(transient_comp)
        with pytest.raises(ConnectionDropped, match="Transient auth error"):
            router.process_packet(
                AuthResponse(zxid=21, err=ConnectionLoss.code)
            )
        assert isinstance(transient_comp.exc, ConnectionLoss)

        perm_comp = _FakeCompletion()
        requests.auth_pending.append(perm_comp)
        router.process_packet(AuthResponse(zxid=22, err=AuthFailedError.code))
        assert isinstance(perm_comp.exc, AuthFailedError)
        assert isinstance(events[-1], AuthFailedEvent)

    def test_watch_dispatch_unchroot_and_empty_short_circuit(self) -> None:
        state_box: list[ClientState] = [Connected(host="127.0.0.1")]
        router, _, watchers, _, dispatched, _ = self._make_router(
            state_box, chroot="/app"
        )

        # No watchers registered -> short-circuit without dispatching.
        router.process_packet(
            WatchPacket(zxid=5, type=CHANGED_EVENT, state=3, path="/app/node")
        )
        assert dispatched == []

        seen_cb = lambda ev: None  # noqa: E731
        watchers.data_watchers["/app/node"].add(seen_cb)
        router.process_packet(
            WatchPacket(zxid=6, type=CHANGED_EVENT, state=3, path="/app/node")
        )
        assert len(dispatched) == 1
        ev = dispatched[0].args[0]
        assert ev.type == EventType.CHANGED
        assert ev.state == KeeperState.CONNECTED
        assert ev.path == "/node"
        assert ev.zxid == 6

    def test_response_unknown_error_transaction_unchroot_and_close(
        self,
    ) -> None:
        state_box: list[ClientState] = [Connected(host="127.0.0.1")]
        router, _, _, events, _, zxids = self._make_router(
            state_box, chroot="/app"
        )

        err_comp = _FakeCompletion()
        router.process_packet(
            ResponsePacket(
                header=ReplyHeader(xid=1, zxid=30, err=-9999),
                request=GetData("/app/k", None),
                async_object=err_comp,
            )
        )
        assert isinstance(err_comp.exc, SystemZookeeperError)
        assert zxids == [30]

        tx_comp = _FakeCompletion()
        router.process_packet(
            ResponsePacket(
                header=ReplyHeader(xid=2, zxid=31, err=0),
                request=Transaction([]),
                async_object=tx_comp,
                response=["/app/a", True],
            )
        )
        assert tx_comp.value == ["/a", True]

        res = router.process_packet(
            ResponsePacket(
                header=ReplyHeader(xid=3, zxid=32, err=0),
                request=Close(),
                async_object=None,
            )
        )
        assert res is CLOSE_RESPONSE
        assert events == [SessionClosedEvent()]


class TestExceptionToEvent:
    def test_maps_handled_exceptions_and_returns_none_for_unknown(
        self,
    ) -> None:
        logger = logging.getLogger("kazoo.test.session")

        ev_to = _exception_to_event(
            TimeoutError("t"), TimeoutError, 1, None, logger
        )
        assert ev_to == (ConnectionLostEvent(reason="t"), False, False)

        ev_exp = _exception_to_event(
            SessionExpiredError(), TimeoutError, 42, None, logger
        )
        assert ev_exp == (SessionExpiredEvent(session_id=42), True, False)

        ev_rw = _exception_to_event(
            RWServerAvailable(), TimeoutError, 42, ("10.0.0.1", 2181), logger
        )
        assert ev_rw == (
            RWServerFoundEvent(server=("10.0.0.1", 2181)),
            True,
            False,
        )

        assert (
            _exception_to_event(
                ValueError("bug"), TimeoutError, 1, None, logger
            )
            is None
        )

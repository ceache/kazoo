from __future__ import annotations

import collections
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import Mock, patch

import pytest

from kazoo.client import KazooClient
from kazoo.exceptions import (
    ConfigurationError,
    ConnectionDropped,
    KazooException,
    ReplyTooLargeError,
    SASLException,
    SessionClosedRequireSaslError,
    ZookeeperError,
)
from kazoo.loggingsupport import BLATHER
from kazoo.protocol.connection import (
    CLOSE_RESPONSE,
    STOP_CONNECTING,
    ConnectionHandler,
    RequestManager,
    RWPinger,
    RWServerAvailable,
    WatcherManager,
    _exception_to_event,
)
from kazoo.protocol.sasl import SASLConfig
from kazoo.protocol.serialization import (
    JUTE_MAX_BUFFER,
    SASL,
    Close,
    Connect,
    GetData,
    Ping,
    ReplyHeader,
    ResponsePacket,
    WatchPacket,
    _write_buffer,
    int_int_long_struct,
    int_struct,
    reply_header_struct,
    stat_struct,
)
from kazoo.protocol.statemachine import (
    AuthFailed,
    AuthFailedEvent,
    CloseRequested,
    ConnectAttemptStarted,
    Connected,
    Connecting,
    ConnectResponseReceived,
    EnqueueCloseRequest,
    HeartbeatDeadlineReached,
    Inactive,
    InvalidStateTransitionError,
    PollTimeoutExpired,
    ReadConnectResponsePacket,
    ResetSessionData,
    SendConnectPacket,
    SocketReadable,
    WakeUpSignaled,
)
from kazoo.protocol.states import KeeperState
from kazoo.retry import KazooRetry, RetryFailedError


def _make_mock_client(**kwargs: Any) -> Mock:
    client = Mock()
    handler = Mock()
    handler.timeout_exception = TimeoutError
    handler.lock_object = threading.Lock
    client.handler = handler
    client.hosts = [("127.0.0.1", 2181)]
    client._session_timeout = 10000
    client._session_id = None
    client._session_passwd = b""
    client.last_zxid = 0
    client.read_only = False
    client.auth_data = set()
    client.use_ssl = False
    client.keyfile = None
    client.certfile = None
    client.ca = None
    client.keyfile_password = None
    client.verify_certs = True
    client.check_hostname = False
    client.client_state = KeeperState.CONNECTING
    client.concurrent_request_limit = None
    sm = Mock()
    sm.state = Connecting()
    client._state_machine = sm
    client._request_manager = RequestManager()
    client._watcher_manager = WatcherManager()
    for key, value in kwargs.items():
        setattr(client, key, value)
    return client


class TestConnectionAuthExceptions:
    def test_connect_attempt_sasl_exception(self) -> None:
        client = _make_mock_client()
        retry = KazooRetry()
        connection = ConnectionHandler(client, retry)

        with patch.object(
            client.handler,
            "create_connection",
            side_effect=SASLException("library error"),
        ):
            result = connection._connect_attempt(
                "127.0.0.1", "127.0.0.1", 2181, retry
            )
            assert result is STOP_CONNECTING
            assert client.send_event.call_count == 2
            assert client.send_event.call_args_list[0][0][0] == (
                ConnectAttemptStarted(host="127.0.0.1", port=2181)
            )
            event = client.send_event.call_args_list[1][0][0]
            assert isinstance(event, AuthFailedEvent)
            assert isinstance(event.error, SASLException)

    def test_connect_attempt_session_closed_require_sasl(self) -> None:
        client = _make_mock_client()
        retry = KazooRetry()
        connection = ConnectionHandler(client, retry)

        with patch.object(
            client.handler,
            "create_connection",
            side_effect=SessionClosedRequireSaslError(
                "session closed require sasl"
            ),
        ):
            result = connection._connect_attempt(
                "127.0.0.1", "127.0.0.1", 2181, retry
            )
            assert result is STOP_CONNECTING
            assert client.send_event.call_count == 2
            assert client.send_event.call_args_list[0][0][0] == (
                ConnectAttemptStarted(host="127.0.0.1", port=2181)
            )
            event = client.send_event.call_args_list[1][0][0]
            assert isinstance(event, AuthFailedEvent)
            assert isinstance(event.error, SessionClosedRequireSaslError)

    def test_connection_handler_with_typed_sasl_config(self) -> None:
        client = _make_mock_client()
        retry = KazooRetry()
        cfg = SASLConfig(
            mechanism="DIGEST-MD5", username="user", password="pwd"
        )
        connection = ConnectionHandler(client, retry, sasl_config=cfg)
        assert connection.sasl_config == cfg
        assert not hasattr(connection, "sasl_options")

    def test_step_sasl_appends_pending_and_fail_all_skips_none(self) -> None:
        client = _make_mock_client()
        connection = ConnectionHandler(client, KazooRetry())
        exchange = Mock()
        exchange.is_complete = False
        exchange.next_token.return_value = b"init-token"
        connection._sasl_exchange = exchange

        with patch.object(connection, "_submit") as mock_submit:
            connection._step_sasl(challenge=None)

        assert len(connection._requests.pending) == 1
        req, async_obj, xid = connection._requests.pending[0]
        assert isinstance(req, SASL)
        assert req.challenge == b"init-token"
        assert async_obj is None
        assert xid == 1
        mock_submit.assert_called_once_with(
            req, connection._connect_timeout, 1
        )
        connection._requests.fail_all(KeeperState.CONNECTING)
        assert len(connection._requests.pending) == 0


class TestConnectionXidMismatch:
    def test_decode_packet_xid_mismatch_sets_exception_and_raises(
        self,
    ) -> None:
        client = _make_mock_client()
        async_obj = Mock()
        request = Mock()
        expected_xid = 1

        connection = ConnectionHandler(client, KazooRetry())
        connection._requests.pending = collections.deque(
            [(request, async_obj, expected_xid)]
        )
        wire = reply_header_struct.pack(2, 100, 0)

        with pytest.raises(RuntimeError) as exc_info:
            connection._frame_decoder._decode_packet(wire)

        assert exc_info.value.args == (
            "xids do not match, expected 1 received 2",
        )
        async_obj.set_exception.assert_called_once_with(exc_info.value)

    def test_execute_effect_send_connect_packet(self) -> None:
        client = _make_mock_client()
        connection = ConnectionHandler(client, KazooRetry())
        connect_packet = Connect(0, 0, 10000, 0, b"", False)
        with patch.object(connection, "_submit") as mock_submit:
            connection.execute_effect(
                SendConnectPacket(connect=connect_packet, timeout=5.0)
            )
            mock_submit.assert_called_once_with(connect_packet, 5.0)

    def test_execute_effect_read_connect_response_packet(self) -> None:
        client = _make_mock_client()
        connection = ConnectionHandler(client, KazooRetry())
        body = bytearray()
        body.extend(int_int_long_struct.pack(0, 30000, 12345))
        body.extend(_write_buffer(b"password12345678"))
        body.extend([0])
        chunks = [int_struct.pack(len(body)), bytes(body)]
        sock = Mock()

        def fake_recv_into(buf: memoryview) -> int:
            chunk = chunks.pop(0)
            buf[: len(chunk)] = chunk
            return len(chunk)

        sock.recv_into.side_effect = fake_recv_into
        connection._socket = sock
        connection.execute_effect(ReadConnectResponsePacket())
        assert client.send_event.call_count == 1
        event = client.send_event.call_args[0][0]
        assert isinstance(event, ConnectResponseReceived)
        assert event.connect_result.session_id == 12345
        assert event.connect_result.time_out == 30000
        assert event.connect_result.passwd == b"password12345678"
        assert event.connect_result.read_only is False

    def test_execute_effect_reset_session_data(self) -> None:
        client = _make_mock_client()
        retry = Mock()
        connection = ConnectionHandler(client, KazooRetry())
        connection._current_retry = retry
        connection.execute_effect(ResetSessionData())
        retry.reset.assert_called_once()

    def test_read_socket_dispatches_to_decoder_on_mismatch(
        self,
    ) -> None:
        client = _make_mock_client()
        async_obj = Mock()
        request = Mock()

        connection = ConnectionHandler(client, KazooRetry())
        connection._requests.pending = collections.deque(
            [(request, async_obj, 5)]
        )
        body = reply_header_struct.pack(6, 200, 0)
        wire = int_struct.pack(len(body)) + body
        sock = Mock()

        def fake_recv_into(buf: memoryview) -> int:
            buf[: len(wire)] = wire
            return len(wire)

        sock.recv_into.side_effect = fake_recv_into
        connection._socket = sock

        with pytest.raises(RuntimeError) as exc_info:
            connection._read_socket(read_timeout=1.0)

        assert exc_info.value.args == (
            "xids do not match, expected 5 received 6",
        )
        async_obj.set_exception.assert_called_once_with(exc_info.value)


class TestPersistentWatchEvents:
    def test_find_persistent_recursive_watchers(self) -> None:
        client = _make_mock_client()
        w_root = Mock()
        w_a = Mock()
        connection = ConnectionHandler(client, KazooRetry())
        connection._watchers.persistent_recursive_watchers = {
            "/": [w_root],
            "/a": [w_a],
        }

        assert connection._find_persistent_recursive_watchers("/") == [w_root]
        assert connection._find_persistent_recursive_watchers("/b") == [w_root]
        assert connection._find_persistent_recursive_watchers("/a") == [
            w_root,
            w_a,
        ]
        assert connection._find_persistent_recursive_watchers("/a/b") == [
            w_root,
            w_a,
        ]
        assert connection._find_persistent_recursive_watchers("/a/b/c") == [
            w_root,
            w_a,
        ]

    def test_child_event_triggers_persistent_watcher(self) -> None:
        client = _make_mock_client()
        client.client_state = KeeperState.CONNECTED
        client.unchroot.side_effect = lambda p: p
        w_child = Mock()
        w_pers = Mock()

        connection = ConnectionHandler(client, KazooRetry())
        connection._watchers.child_watchers = {"/a": {w_child}}
        connection._watchers.persistent_watchers = {"/a": {w_pers}}
        connection._watchers.persistent_recursive_watchers = {}

        connection._read_watch_event(
            WatchPacket(zxid=-1, type=4, state=3, path="/a")
        )

        # Both child watch and persistent watch should be dispatched
        assert client.handler.dispatch_callback.call_count == 2
        callbacks = [
            call.args[0]
            for call in client.handler.dispatch_callback.call_args_list
        ]
        called_funcs = {cb.func for cb in callbacks}
        assert called_funcs == {w_child, w_pers}
        # One-time child watcher should be popped
        assert "/a" not in connection._watchers.child_watchers
        # Persistent watcher should remain
        assert connection._watchers.persistent_watchers["/a"] == {w_pers}

    def test_created_event_triggers_persistent_and_recursive_watchers(
        self,
    ) -> None:
        client = _make_mock_client()
        client.client_state = KeeperState.CONNECTED
        client.unchroot.side_effect = lambda p: p
        w_data = Mock()
        w_pers = Mock()
        w_rec = Mock()

        connection = ConnectionHandler(client, KazooRetry())
        connection._watchers.data_watchers = {"/a/b": {w_data}}
        connection._watchers.persistent_watchers = {"/a/b": {w_pers}}
        connection._watchers.persistent_recursive_watchers = {"/a": [w_rec]}

        connection._read_watch_event(
            WatchPacket(zxid=-1, type=1, state=3, path="/a/b")
        )

        assert client.handler.dispatch_callback.call_count == 3
        callbacks = [
            call.args[0]
            for call in client.handler.dispatch_callback.call_args_list
        ]
        called_funcs = {cb.func for cb in callbacks}
        assert called_funcs == {w_data, w_pers, w_rec}
        # One-time data watcher should be popped
        assert "/a/b" not in connection._watchers.data_watchers
        # Persistent and recursive watchers remain
        assert connection._watchers.persistent_watchers["/a/b"] == {w_pers}
        assert connection._watchers.persistent_recursive_watchers["/a"] == [
            w_rec
        ]

    def test_changed_event_triggers_persistent_and_recursive_watchers(
        self,
    ) -> None:
        client = _make_mock_client()
        client.client_state = KeeperState.CONNECTED
        client.unchroot.side_effect = lambda p: p
        w_data = Mock()
        w_pers = Mock()
        w_rec = Mock()

        connection = ConnectionHandler(client, KazooRetry())
        connection._watchers.data_watchers = {"/a/b": {w_data}}
        connection._watchers.persistent_watchers = {"/a/b": {w_pers}}
        connection._watchers.persistent_recursive_watchers = {"/a": [w_rec]}

        connection._read_watch_event(
            WatchPacket(zxid=-1, type=3, state=3, path="/a/b")
        )

        assert client.handler.dispatch_callback.call_count == 3
        # One-time data watcher should be popped
        assert "/a/b" not in connection._watchers.data_watchers
        # Persistent and recursive watchers remain
        assert connection._watchers.persistent_watchers["/a/b"] == {w_pers}
        assert connection._watchers.persistent_recursive_watchers["/a"] == [
            w_rec
        ]

    def test_deleted_event_triggers_all_watchers(self) -> None:
        client = _make_mock_client()
        client.client_state = KeeperState.CONNECTED
        client.unchroot.side_effect = lambda p: p
        w_data = Mock()
        w_child = Mock()
        w_pers = Mock()
        w_rec = Mock()

        connection = ConnectionHandler(client, KazooRetry())
        connection._watchers.data_watchers = {"/a": {w_data}}
        connection._watchers.child_watchers = {"/a": {w_child}}
        connection._watchers.persistent_watchers = {"/a": {w_pers}}
        connection._watchers.persistent_recursive_watchers = {"/": [w_rec]}

        connection._read_watch_event(
            WatchPacket(zxid=-1, type=2, state=3, path="/a")
        )

        assert client.handler.dispatch_callback.call_count == 4
        # Data and child watchers should be popped
        assert "/a" not in connection._watchers.data_watchers
        assert "/a" not in connection._watchers.child_watchers
        # Persistent and recursive watchers remain
        assert connection._watchers.persistent_watchers["/a"] == {w_pers}
        assert connection._watchers.persistent_recursive_watchers["/"] == [
            w_rec
        ]

    def test_watch_event_dispatches_zxid(self) -> None:
        """Test that watch events dispatched via ConnectionHandler include the
        zxid.
        """
        client = _make_mock_client()
        client.client_state = KeeperState.CONNECTED
        client.unchroot.side_effect = lambda p: p
        watcher = Mock()

        connection = ConnectionHandler(client, KazooRetry())
        connection._watchers.data_watchers = {"/node": {watcher}}
        connection._watchers.persistent_watchers = {}
        connection._watchers.persistent_recursive_watchers = {}

        connection._read_watch_event(
            WatchPacket(zxid=99999, type=1, state=3, path="/node")
        )

        assert client.handler.dispatch_callback.call_count == 1
        cb = client.handler.dispatch_callback.call_args[0][0]
        ev = cb.args[0]
        assert ev.path == "/node"
        assert ev.zxid == 99999

    def test_watch_event_dispatches_zxid_to_persistent_and_recursive_watchers(
        self,
    ) -> None:
        """Test that ConnectionHandler dispatches zxid to persistent and
        persistent recursive watchers.
        """
        client = _make_mock_client()
        client.client_state = KeeperState.CONNECTED
        client.unchroot.side_effect = lambda p: p
        w_pers = Mock()
        w_rec = Mock()

        connection = ConnectionHandler(client, KazooRetry())
        connection._watchers.data_watchers = {}
        connection._watchers.persistent_watchers = {"/node": {w_pers}}
        connection._watchers.persistent_recursive_watchers = {"/": [w_rec]}

        connection._read_watch_event(
            WatchPacket(zxid=88888, type=3, state=3, path="/node")
        )

        assert client.handler.dispatch_callback.call_count == 2
        callbacks = [
            call.args[0]
            for call in client.handler.dispatch_callback.call_args_list
        ]
        assert {cb.func for cb in callbacks} == {w_pers, w_rec}
        for cb in callbacks:
            ev = cb.args[0]
            assert ev.path == "/node"
            assert ev.zxid == 88888

    def test_watched_event_no_zxid_default(self) -> None:
        """Test that WatchedEvent defaults zxid to NO_ZXID (-1)."""
        from kazoo.protocol.states import EventType, WatchedEvent

        ev = WatchedEvent(EventType.NONE, KeeperState.CONNECTED, None)
        assert ev.zxid == WatchedEvent.NO_ZXID
        assert ev.zxid == -1

    def test_watched_event_immutability(self) -> None:
        """Test that WatchedEvent instances are immutable."""
        from kazoo.protocol.states import EventType, WatchedEvent

        ev = WatchedEvent(EventType.CREATED, KeeperState.CONNECTED, "/a", 100)
        with pytest.raises(AttributeError):
            ev.zxid = 200  # type: ignore[misc]

    def test_watched_event_properties_and_defaults(self) -> None:
        """Test WatchedEvent attributes, defaults, and tuple unpacking."""
        from kazoo.protocol.states import EventType, WatchedEvent

        ev = WatchedEvent(EventType.CREATED, KeeperState.CONNECTED, "/a")
        assert ev.type == EventType.CREATED
        assert ev.state == KeeperState.CONNECTED
        assert ev.path == "/a"
        assert ev.zxid == WatchedEvent.NO_ZXID
        assert ev.zxid == -1

        ev_with_zxid = WatchedEvent(
            EventType.CHANGED, KeeperState.CONNECTED, "/b", 42
        )
        assert ev_with_zxid.zxid == 42
        assert ev_with_zxid[0] == EventType.CHANGED
        assert ev_with_zxid[1] == KeeperState.CONNECTED
        assert ev_with_zxid[2] == "/b"
        assert ev_with_zxid[3] == 42
        assert ev_with_zxid == (
            EventType.CHANGED,
            KeeperState.CONNECTED,
            "/b",
            42,
        )


class TestConnectionHandlerWatchRegistration:
    """Unit tests for ConnectionHandler registering and removing watchers in
    read_response.
    """

    def test_read_response_add_watch_persistent(self) -> None:
        from collections import defaultdict

        from kazoo.protocol.serialization import AddWatch
        from kazoo.protocol.states import AddWatchMode

        client = _make_mock_client()
        w = Mock()
        async_obj = Mock()
        request = AddWatch("/a", w, AddWatchMode.PERSISTENT)

        connection = ConnectionHandler(client, KazooRetry())
        connection._watchers.persistent_watchers = defaultdict(set)
        connection._watchers.persistent_recursive_watchers = defaultdict(set)

        header = ReplyHeader(xid=1, zxid=10, err=0)
        connection._read_response(
            ResponsePacket(
                header=header, request=request, async_object=async_obj
            )
        )

        assert connection._watchers.persistent_watchers["/a"] == {w}
        assert len(connection._watchers.persistent_recursive_watchers) == 0
        async_obj.set.assert_called_once_with(None)

    def test_read_response_add_watch_persistent_recursive(self) -> None:
        from collections import defaultdict

        from kazoo.protocol.serialization import AddWatch
        from kazoo.protocol.states import AddWatchMode

        client = _make_mock_client()
        w = Mock()
        async_obj = Mock()
        request = AddWatch("/a", w, AddWatchMode.PERSISTENT_RECURSIVE)

        connection = ConnectionHandler(client, KazooRetry())
        connection._watchers.persistent_watchers = defaultdict(set)
        connection._watchers.persistent_recursive_watchers = defaultdict(set)

        header = ReplyHeader(xid=1, zxid=10, err=0)
        connection._read_response(
            ResponsePacket(
                header=header, request=request, async_object=async_obj
            )
        )

        assert connection._watchers.persistent_recursive_watchers["/a"] == {w}
        assert len(connection._watchers.persistent_watchers) == 0

    def test_read_response_remove_watches_children(self) -> None:
        from kazoo.protocol.serialization import RemoveWatches
        from kazoo.protocol.states import WatcherType

        client = _make_mock_client()
        w1, w2 = Mock(), Mock()
        async_obj = Mock()
        request = RemoveWatches("/a", WatcherType.CHILDREN)

        connection = ConnectionHandler(client, KazooRetry())
        connection._watchers.child_watchers = {"/a": {w1}}
        connection._watchers.data_watchers = {"/a": {w2}}

        header = ReplyHeader(xid=1, zxid=10, err=0)
        connection._read_response(
            ResponsePacket(
                header=header, request=request, async_object=async_obj
            )
        )

        assert "/a" not in connection._watchers.child_watchers
        assert connection._watchers.data_watchers["/a"] == {w2}

    def test_read_response_remove_watches_data(self) -> None:
        from kazoo.protocol.serialization import RemoveWatches
        from kazoo.protocol.states import WatcherType

        client = _make_mock_client()
        w1, w2 = Mock(), Mock()
        async_obj = Mock()
        request = RemoveWatches("/a", WatcherType.DATA)

        connection = ConnectionHandler(client, KazooRetry())
        connection._watchers.child_watchers = {"/a": {w1}}
        connection._watchers.data_watchers = {"/a": {w2}}

        header = ReplyHeader(xid=1, zxid=10, err=0)
        connection._read_response(
            ResponsePacket(
                header=header, request=request, async_object=async_obj
            )
        )

        assert "/a" not in connection._watchers.data_watchers
        assert connection._watchers.child_watchers["/a"] == {w1}

    def test_read_response_remove_watches_any(self) -> None:
        from kazoo.protocol.serialization import RemoveWatches
        from kazoo.protocol.states import WatcherType

        client = _make_mock_client()
        w1, w2, w3, w4 = Mock(), Mock(), Mock(), Mock()
        async_obj = Mock()
        request = RemoveWatches("/a", WatcherType.ANY)

        connection = ConnectionHandler(client, KazooRetry())
        connection._watchers.child_watchers = {"/a": {w1}}
        connection._watchers.data_watchers = {"/a": {w2}}
        connection._watchers.persistent_watchers = {"/a": {w3}}
        connection._watchers.persistent_recursive_watchers = {"/a": {w4}}

        header = ReplyHeader(xid=1, zxid=10, err=0)
        connection._read_response(
            ResponsePacket(
                header=header, request=request, async_object=async_obj
            )
        )

        assert "/a" not in connection._watchers.child_watchers
        assert "/a" not in connection._watchers.data_watchers
        assert "/a" not in connection._watchers.persistent_watchers
        assert "/a" not in connection._watchers.persistent_recursive_watchers

    def test_read_response_add_watch_unexpected_mode(self) -> None:
        from kazoo.protocol.serialization import AddWatch

        client = _make_mock_client()
        w = Mock()
        async_obj = Mock()
        request = AddWatch("/a", w, 999)  # invalid mode

        connection = ConnectionHandler(client, KazooRetry())
        header = ReplyHeader(xid=1, zxid=10, err=0)
        with pytest.raises(ValueError, match="Unexpected AddWatchMode: 999"):
            connection._read_response(
                ResponsePacket(
                    header=header, request=request, async_object=async_obj
                )
            )

    def test_read_response_remove_watches_unexpected_type(self) -> None:
        from kazoo.protocol.serialization import RemoveWatches

        client = _make_mock_client()
        async_obj = Mock()
        request = RemoveWatches("/a", 999)  # invalid type

        connection = ConnectionHandler(client, KazooRetry())
        header = ReplyHeader(xid=1, zxid=10, err=0)
        with pytest.raises(ValueError, match="Unexpected WatcherType: 999"):
            connection._read_response(
                ResponsePacket(
                    header=header, request=request, async_object=async_obj
                )
            )


class TestRWServerSelection:
    def test_rw_pinger_skips_down_and_ro_hosts_and_returns_hostip(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        send_fail_sock = Mock()
        send_fail_sock.sendall.side_effect = OSError("broken pipe")
        ro_sock = Mock()
        ro_sock.recv.return_value = b"ro"
        rw_sock = Mock()
        rw_sock.recv.return_value = b"rw"

        conns = {
            "10.0.0.1": send_fail_sock,
            "10.0.0.2": ro_sock,
            "10.0.0.3": rw_sock,
        }
        dialed: list[tuple[str, str, int, float | None]] = []

        def fake_connect(
            host: str, hostip: str, port: int, timeout: float | None
        ) -> Mock:
            dialed.append((host, hostip, port, timeout))
            if hostip == "10.0.0.0":
                raise OSError("connection refused")
            return conns[hostip]

        @contextmanager
        def socket_handling() -> Iterator[None]:
            try:
                yield
            except OSError as exc:
                raise ConnectionDropped(str(exc)) from exc

        logger = logging.getLogger("kazoo.test.rwpinger")
        pinger = RWPinger(
            [
                ("refused.example", "10.0.0.0", 2180),
                ("sendfail.example", "10.0.0.1", 2181),
                ("ro.example", "10.0.0.2", 2182),
                ("rw.example", "10.0.0.3", 2183),
            ],
            fake_connect,
            socket_handling,
            timeout=0.75,
            logger=logger,
        )

        with caplog.at_level(logging.DEBUG, logger="kazoo.test.rwpinger"):
            it = iter(pinger)
            # Each next() probes at most one endpoint
            assert next(it) is False
            assert len(dialed) == 1
            assert next(it) is False
            assert len(dialed) == 2
            assert next(it) is False
            assert len(dialed) == 3
            result = next(it)

        assert result == ("rw.example", "10.0.0.3", 2183)
        assert dialed == [
            ("refused.example", "10.0.0.0", 2180, 0.75),
            ("sendfail.example", "10.0.0.1", 2181, 0.75),
            ("ro.example", "10.0.0.2", 2182, 0.75),
            ("rw.example", "10.0.0.3", 2183, 0.75),
        ]
        send_fail_sock.settimeout.assert_called_once_with(0.75)
        ro_sock.settimeout.assert_called_once_with(0.75)
        rw_sock.settimeout.assert_called_once_with(0.75)
        send_fail_sock.close.assert_called_once()
        ro_sock.close.assert_called_once()
        rw_sock.close.assert_called_once()
        assert (
            "Pinging server for r/w: rw.example(10.0.0.3):2183" in caplog.text
        )

    def test_rw_pinger_exponential_backoff(self) -> None:
        ro_sock = Mock()
        ro_sock.recv.return_value = b"ro"
        dialed: list[tuple[str, str, int, float | None]] = []

        def fake_connect(
            host: str, hostip: str, port: int, timeout: float | None
        ) -> Mock:
            dialed.append((host, hostip, port, timeout))
            return ro_sock

        @contextmanager
        def no_error() -> Iterator[None]:
            yield

        now = 100.0
        pinger = RWPinger(
            [
                ("ro1.example", "10.0.0.1", 2181),
                ("ro2.example", "10.0.0.2", 2182),
            ],
            fake_connect,
            no_error,
        )

        with (
            patch(
                "kazoo.protocol.connection.time.monotonic",
                side_effect=lambda: now,
            ),
            patch("kazoo.protocol.connection.random.randint", return_value=0),
        ):
            it = iter(pinger)
            # First run-through steps one host per next() before back-off
            assert next(it) is False
            assert len(dialed) == 1
            assert next(it) is False
            assert len(dialed) == 2

            # Within initial 0.5s backoff -> skips without dialing
            now = 100.4
            assert next(it) is False
            assert len(dialed) == 2

            # Past 0.5s -> second pass starts, next backoff doubles to 1.0s
            now = 100.5
            assert next(it) is False
            assert len(dialed) == 3
            assert next(it) is False
            assert len(dialed) == 4

            now = 101.4
            assert next(it) is False
            assert len(dialed) == 4

            # Past 1.0s -> third pass starts
            now = 101.5
            assert next(it) is False
            assert len(dialed) == 5

    def test_server_pinger_reuses_host_ports_and_shares_open_socket(
        self,
    ) -> None:
        sock = Mock()
        sock.recv.return_value = b"rw"
        client = _make_mock_client(
            use_ssl=True,
            keyfile="k",
            certfile="c",
            ca="ca",
            keyfile_password="pw",
            verify_certs=True,
            check_hostname=True,
        )
        client.handler.create_connection.return_value = sock
        connection = ConnectionHandler(client, KazooRetry())
        connection._host_ports = [("rw.example", "10.0.0.2", 2182)]

        pinger = connection._server_pinger()
        result = next(iter(pinger))

        assert result == ("rw.example", "10.0.0.2", 2182)
        client.handler.create_connection.assert_called_once_with(
            address=("10.0.0.2", 2182),
            hostname="rw.example",
            timeout=1.0,
            use_ssl=True,
            keyfile="k",
            certfile="c",
            ca="ca",
            keyfile_password="pw",
            verify_certs=True,
            check_hostname=True,
        )
        sock.settimeout.assert_called_once_with(1.0)
        sock.close.assert_called_once()

    def test_connect_attempt_rw_server_overrides_host_hostip_port_and_logs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = _make_mock_client()
        sock = Mock()
        client.handler.create_connection.return_value = sock
        retry = KazooRetry()
        logger = logging.getLogger("kazoo.test.rw_attempt")
        connection = ConnectionHandler(client, retry, logger=logger)
        connection._rw_server = ("rw.example", "10.0.0.2", 2182)

        def finish_loop() -> None:
            client._state_machine.state = Inactive()

        with (
            caplog.at_level(BLATHER, logger="kazoo.test.rw_attempt"),
            patch.object(connection, "_setup_selector"),
            patch.object(connection, "_close_socket"),
            patch.object(connection, "_run_io_loop", side_effect=finish_loop),
        ):
            result = connection._connect_attempt(
                "ro.example", "10.0.0.1", 2181, retry
            )

        assert result is STOP_CONNECTING
        assert connection._rw_server is None
        client.handler.create_connection.assert_called_once_with(
            address=("10.0.0.2", 2182),
            hostname="rw.example",
            timeout=retry.cur_delay,
            use_ssl=False,
            keyfile=None,
            certfile=None,
            ca=None,
            keyfile_password=None,
            verify_certs=True,
            check_hostname=False,
        )
        assert (
            "Found r/w server to use, rw.example(10.0.0.2):2182" in caplog.text
        )
        assert "ro.example" not in caplog.text
        assert "Closing connection to rw.example(10.0.0.2):2182" in caplog.text

    def test_connect_loop_failover_uses_rw_server_hostip(self) -> None:
        client = _make_mock_client(
            hosts=[
                ("ro.example", 2181),
                ("other.example", 2183),
            ]
        )
        retry = KazooRetry()
        connection = ConnectionHandler(client, retry)

        expanded = [
            ("ro.example", "10.0.0.1", 2181),
            ("other.example", "10.0.0.3", 2183),
        ]
        open_calls: list[tuple[str, str, int]] = []

        def fake_open_socket(
            host: str, hostip: str, port: int, timeout: float | None = None
        ) -> Mock:
            open_calls.append((host, hostip, port))
            return Mock()

        def fake_io_loop() -> None:
            if len(open_calls) == 1:
                connection._rw_server = ("rw.example", "10.0.0.2", 2182)
                raise RWServerAvailable()
            client._state_machine.state = Inactive()

        with (
            patch.object(
                connection, "_expand_client_hosts", return_value=expanded
            ),
            patch.object(
                connection, "_open_socket", side_effect=fake_open_socket
            ),
            patch.object(connection, "_setup_selector"),
            patch.object(connection, "_close_socket"),
            patch.object(connection, "_run_io_loop", side_effect=fake_io_loop),
        ):
            result = connection._connect_loop(retry)

        assert result is STOP_CONNECTING
        assert open_calls == [
            ("ro.example", "10.0.0.1", 2181),
            ("rw.example", "10.0.0.2", 2182),
        ]
        assert connection._rw_server is None


class TestConnectionIOLoopDecomposition:
    def test_is_terminal_property(self) -> None:
        client = _make_mock_client()
        conn = ConnectionHandler(client, KazooRetry())

        # Initially non-terminal
        assert conn._is_terminal is False

        # When state machine state is Connecting (non-terminal)
        client._state_machine = Mock()
        client._state_machine.state = Connecting()
        assert conn._is_terminal is False

        # When state machine state is Inactive (terminal for connecting)
        client._state_machine.state = Inactive()
        assert conn._is_terminal is True

        # When state machine state is AuthFailed (terminal)
        client._state_machine.state = AuthFailed()
        assert conn._is_terminal is True

        client._state_machine.state = Connected()
        assert conn._is_terminal is False

    def test_compute_poll_timeout_non_session(self) -> None:
        client = _make_mock_client()
        conn = ConnectionHandler(client, KazooRetry())
        conn._connect_timeout = 5.0
        timeout, deadline = conn._compute_poll_timeout()
        assert timeout == 5.0
        assert deadline is None

    def test_compute_poll_timeout_session_active(self) -> None:
        client = _make_mock_client()
        client._state_machine = Mock()
        client._state_machine.state = Connected()
        conn = ConnectionHandler(client, KazooRetry())
        conn._read_timeout = 10.0
        conn._last_send = 100.0

        with patch("time.monotonic", return_value=102.0):
            timeout, deadline = conn._compute_poll_timeout()
            assert deadline is not None
            assert timeout > 0

    def test_dispatch_ready_events(self) -> None:
        client = _make_mock_client()
        conn = ConnectionHandler(client, KazooRetry())
        events: list[Any] = []
        conn._emit_event = events.append  # type: ignore[method-assign]

        # 1. Empty ready set -> PollTimeoutExpired
        conn._dispatch_ready_events(set(), None)
        assert len(events) == 1
        assert isinstance(events[0], PollTimeoutExpired)

        # 2. zk_socket in ready -> SocketReadable
        events.clear()
        conn._dispatch_ready_events({"zk_socket"}, None)
        assert len(events) == 1
        assert isinstance(events[0], SocketReadable)

        # 3. wakeup in ready with wakeup registered -> WakeUpSignaled
        events.clear()
        conn._selector = Mock()
        conn._read_sock = Mock()
        conn._read_sock.recv.side_effect = BlockingIOError
        conn._selector.get_key.return_value = Mock(events=1)
        res = conn._dispatch_ready_events({"wakeup"}, None)
        assert res is True
        assert len(events) == 1
        assert isinstance(events[0], WakeUpSignaled)

        # 4. deadline expired while session active -> HeartbeatDeadlineReached
        events.clear()
        client._state_machine = Mock()
        client._state_machine.state = Connected()
        with patch("time.monotonic", return_value=105.0):
            conn._dispatch_ready_events(set(), 100.0)
            assert any(isinstance(e, PollTimeoutExpired) for e in events)
            assert any(isinstance(e, HeartbeatDeadlineReached) for e in events)

        # 5. Fast-path: zk_socket when session active bypasses state machine
        events.clear()
        read_called = False

        def fake_read_socket(*args: Any, **kwargs: Any) -> None:
            nonlocal read_called
            read_called = True

        conn._read_socket = fake_read_socket  # type: ignore[method-assign]
        conn._dispatch_ready_events({"zk_socket"}, None)
        assert read_called is True
        assert len(events) == 0

        # 6. Fast-path: wakeup when session active calls _send_request directly
        events.clear()
        send_called = False

        def fake_send_request(*args: Any, **kwargs: Any) -> None:
            nonlocal send_called
            send_called = True

        conn._send_request = fake_send_request  # type: ignore[method-assign]
        res = conn._dispatch_ready_events({"wakeup"}, None)
        assert res is True
        assert send_called is True
        assert len(events) == 0

    def test_run_io_loop_terminates_when_is_terminal(self) -> None:
        from kazoo.protocol.statemachine import Handshaking

        client = _make_mock_client()
        client._state_machine = Mock()
        client._state_machine.state = Handshaking(host="127.0.0.1", port=2181)
        conn = ConnectionHandler(client, KazooRetry())
        conn._socket = Mock()

        steps = 0

        def fake_poll_step() -> None:
            nonlocal steps
            steps += 1
            if steps == 3:
                client._state_machine.state = Inactive()

        conn._poll_step = fake_poll_step  # type: ignore[method-assign]
        conn._run_io_loop()
        assert steps == 3

    def test_run_io_loop_exits_without_spin_on_direct_teardown_events(
        self,
    ) -> None:
        """Direct ConnectionLost/SessionExpired closes socket without spin."""
        import socket

        from kazoo.protocol.statemachine import (
            ConnectionLostEvent,
            SessionExpiredEvent,
            Suspended,
        )

        for ev in (
            ConnectionLostEvent(reason="direct drop"),
            SessionExpiredEvent(session_id=42),
        ):
            client = KazooClient(hosts="127.0.0.1:2181")
            conn = client._connection
            s_a, s_b = socket.socketpair()
            try:
                s_a.setblocking(False)
                s_b.sendall(b"unread bytes")
                conn._selector = client.handler.create_selector()
                conn._socket = s_a
                conn._setup_selector()
                client._state_machine.state = Connected(
                    host="127.0.0.1", port=2181, session_id=42
                )
                client.send_event(ev)
                assert conn._socket is None
                # Even if a socket were still set in a non-active state,
                # _run_io_loop exits immediately without spinning.
                conn._socket = s_b
                if isinstance(ev, ConnectionLostEvent):
                    assert isinstance(client.state_machine, Suspended)
                conn._run_io_loop()
                conn._socket = None
            finally:
                conn._close_selector()
                s_a.close()
                s_b.close()

    def test_run_io_loop_terminates_when_socket_closed(self) -> None:
        client = _make_mock_client()
        client._state_machine = Mock()
        client._state_machine.state = Connected()
        conn = ConnectionHandler(client, KazooRetry())
        conn._socket = Mock()

        steps = 0

        def fake_poll_step() -> None:
            nonlocal steps
            steps += 1
            if steps == 2:
                conn._socket = None

        conn._poll_step = fake_poll_step  # type: ignore[method-assign]
        conn._run_io_loop()
        assert steps == 2

    def test_wake_sends_byte_on_write_sock(self) -> None:
        client = _make_mock_client()
        conn = ConnectionHandler(client, KazooRetry())

        # When write_sock is None, returns False
        assert conn.wake() is False

        # When write_sock is present, sends byte and returns True
        mock_sock = Mock()
        conn._write_sock = mock_sock
        assert conn.wake() is True
        mock_sock.send.assert_called_once_with(b"\0")

        # When write_sock raises OSError, returns False
        mock_sock.send.side_effect = OSError("broken pipe")
        assert conn.wake() is False

    def test_write_partial_send_drains_remaining_bytes(
        self,
    ) -> None:
        client = _make_mock_client()
        conn = ConnectionHandler(client, KazooRetry())
        mock_sock = Mock()
        conn._socket = mock_sock
        conn.handler.select = Mock(return_value=([], [mock_sock], []))  # type: ignore[assignment]

        # Send 10 of 20 bytes initially, then remaining 10
        mock_sock.send.side_effect = [10, 10]
        payload = b"0123456789abcdefghij"
        conn._write(payload)

        assert mock_sock.send.call_count == 2
        conn.handler.select.assert_called_once()  # type: ignore[attr-defined]

    def test_write_timeout_during_drain(self) -> None:
        client = _make_mock_client()
        conn = ConnectionHandler(client, KazooRetry())
        mock_sock = Mock()
        conn._socket = mock_sock
        conn.handler.select = Mock(return_value=([], [], []))  # type: ignore[assignment]

        # Send 10 of 20 bytes initially, then select times out
        mock_sock.send.return_value = 10
        payload = b"0123456789abcdefghij"
        with pytest.raises(conn.handler.timeout_exception):
            conn._write(payload, timeout=0.1)

    def test_write_raises_connection_dropped_when_socket_is_none(self) -> None:
        client = _make_mock_client()
        conn = ConnectionHandler(client, KazooRetry())
        conn._socket = None
        with pytest.raises(ConnectionDropped, match="socket closed"):
            conn._write(b"data", timeout=0.1)

    def test_unconditional_enqueue_and_wake(self) -> None:
        client = _make_mock_client()
        client._connection = Mock()
        client._connection.wake.return_value = True
        client._state_machine = Mock()
        client._state_machine.state = Connected()
        client._request_manager = RequestManager()

        r1, a1 = Mock(), Mock()
        r2, a2 = Mock(), Mock()

        # First request on empty queue triggers wake
        KazooClient._call(client, r1, a1)
        client._connection.wake.assert_called_once()

        # Second request on non-empty queue also triggers wake
        KazooClient._call(client, r2, a2)
        assert client._connection.wake.call_count == 2

    def test_send_request_batches_multiple_queued_requests(self) -> None:
        client = _make_mock_client()
        client.concurrent_request_limit = 10
        conn = ConnectionHandler(client, KazooRetry())
        conn._socket = Mock()
        conn._socket.send.side_effect = lambda v: len(v)

        r1, a1 = Ping(), Mock()
        r2, a2 = Ping(), Mock()
        conn._requests.enqueue(r1, a1)
        conn._requests.enqueue(r2, a2)

        conn._send_request(10.0)

        # Both requests should have been dequeued and submitted
        assert len(conn._requests.queue) == 0
        assert len(conn._requests.pending) == 2

    def test_poll_step_dispatches_queued_requests_when_session_active(
        self,
    ) -> None:
        client = _make_mock_client()
        client.concurrent_request_limit = 10
        client._state_machine = Mock()
        client._state_machine.state = Connected()
        conn = ConnectionHandler(client, KazooRetry())
        conn._socket = Mock()
        conn._socket.send.side_effect = lambda v: len(v)
        conn._select = Mock(return_value=set())  # type: ignore[method-assign]

        r1, a1 = Ping(), Mock()
        conn._requests.enqueue(r1, a1)
        assert len(conn._requests.queue) == 1

        conn._poll_step()

        # Request should have been sent directly before/during poll step
        assert len(conn._requests.queue) == 0
        assert len(conn._requests.pending) == 1

    def test_session_established_wakes_selector_when_requests_queued(
        self,
    ) -> None:
        from kazoo.protocol.statemachine import SessionEstablished

        client = _make_mock_client()
        conn = ConnectionHandler(client, KazooRetry())
        conn.wake = Mock()  # type: ignore[method-assign]

        r1, a1 = Ping(), Mock()
        conn._requests.enqueue(r1, a1)

        conn.execute_effect(SessionEstablished())
        conn.wake.assert_called_once()

    def test_execute_effect_enqueue_close_request(self) -> None:
        client = _make_mock_client()
        conn = ConnectionHandler(client, KazooRetry())
        conn.wake = Mock()  # type: ignore[method-assign]

        conn.execute_effect(EnqueueCloseRequest())
        assert len(conn._requests.queue) == 1
        req, async_obj = conn._requests.peek_queued()
        assert isinstance(req, Close)
        assert async_obj is None
        conn.wake.assert_called_once()

    def test_emit_event_unexpected_event_raises_connection_dropped(
        self,
    ) -> None:
        client = _make_mock_client()
        client.send_event.side_effect = InvalidStateTransitionError(
            state=Connecting(), event=Mock()
        )
        conn = ConnectionHandler(client, KazooRetry())

        with pytest.raises(ConnectionDropped) as exc_info:
            conn._emit_event(Mock())
        assert "unexpected event" in str(exc_info.value)

    def test_send_request_bypasses_limit_when_terminal(self) -> None:
        client = _make_mock_client()
        client.concurrent_request_limit = 1
        conn = ConnectionHandler(client, KazooRetry())
        conn._socket = Mock()
        conn._socket.send.side_effect = lambda v: len(v)

        # Enqueue 1 request that fills capacity
        r1, a1 = Ping(), Mock()
        conn._requests.enqueue(r1, a1)
        conn._send_request(10.0)
        assert len(conn._requests.pending) == 1
        assert not conn._requests.has_send_capacity(1)

        # Enqueue Close request
        r2, a2 = Close(), None
        conn._requests.enqueue(r2, a2)

        # When not terminal, _send_request cannot send r2
        conn._send_request(10.0)
        assert len(conn._requests.queue) == 1

        # When terminal, _send_request bypasses capacity and sends Close
        client._state_machine.state = Inactive()
        conn._send_request(10.0)
        assert len(conn._requests.queue) == 0
        assert len(conn._requests.pending) == 2

    def test_stop_when_non_connected_leaves_queue_empty(self) -> None:
        from kazoo.exceptions import AuthFailedError
        from kazoo.protocol.statemachine import (
            AuthFailedEvent,
            SessionExpiredEvent,
            StartRequested,
        )

        for ev in (
            AuthFailedEvent(error=AuthFailedError("auth err")),
            SessionExpiredEvent(session_id=123),
        ):
            client = KazooClient(hosts="127.0.0.1:2181")
            client.send_event(StartRequested())
            client.send_event(ev)
            assert not client.state_machine.is_connected
            client.stop()
            assert isinstance(client.state_machine, Inactive)
            assert len(client._request_manager.queue) == 0

    def test_connected_close_requested_enqueues_close_and_wakes(self) -> None:
        from kazoo.protocol.statemachine import (
            CloseRequested,
            Connected,
            ConnectResponseReceived,
            Handshaking,
        )

        client = KazooClient(hosts="127.0.0.1:2181")
        sock = Mock()
        sock.send.side_effect = lambda v: len(v)
        client._connection._socket = sock
        client._state_machine.state = Handshaking(host="127.0.0.1", port=2181)
        client.send_event(
            ConnectResponseReceived(Connect(0, 0, 10000, 42, b"pwd", False))
        )
        assert isinstance(client.state_machine, Connected)
        client._connection.wake = Mock()  # type: ignore[method-assign]

        client.send_event(CloseRequested())
        assert len(client._request_manager.queue) == 1
        req, async_obj = client._request_manager.peek_queued()
        assert isinstance(req, Close)
        assert async_obj is None
        client._connection.wake.assert_called_once()

        # Second CloseRequested while closing must be a no-op
        client.send_event(CloseRequested())
        assert len(client._request_manager.queue) == 1
        client._connection.wake.assert_called_once()

    def test_emit_event_driver_e2e_reconnects(self) -> None:
        from kazoo.protocol.statemachine import (
            Connecting,
            SASLChallengeReceived,
            StartRequested,
        )

        client = KazooClient(hosts="127.0.0.1:2181")
        client.send_event(StartRequested())
        assert isinstance(client.state_machine, Connecting)

        conn = ConnectionHandler(client, client.retry)
        with pytest.raises(ConnectionDropped) as exc_info:
            conn._emit_event(SASLChallengeReceived(challenge=b"token"))
        assert "unexpected event" in str(exc_info.value)
        # State machine remains in Connecting (drop is handled cleanly
        # by transport)
        assert isinstance(client.state_machine, Connecting)

    def test_exception_to_event_mapping(self) -> None:
        import logging

        from kazoo.exceptions import AuthFailedError, SessionExpiredError
        from kazoo.protocol.statemachine import (
            ConnectionLostEvent,
            RWServerFoundEvent,
            SessionExpiredEvent,
        )

        logger = logging.getLogger("kazoo.tests.connection")

        # 1. Timeout exception
        mapped = _exception_to_event(
            TimeoutError("timed out"), TimeoutError, 99, None, logger
        )
        assert mapped == (
            ConnectionLostEvent(reason="timed out"),
            False,
            False,
        )

        # 2. OSError / ConnectionDropped
        mapped = _exception_to_event(
            ConnectionDropped("eof"), TimeoutError, 99, None, logger
        )
        assert mapped == (ConnectionLostEvent(reason="eof"), False, False)

        # 3. Auth failure variants
        for err in (
            AuthFailedError("bad"),
            SASLException("sasl"),
            SessionClosedRequireSaslError("require sasl"),
        ):
            mapped = _exception_to_event(err, TimeoutError, 99, None, logger)
            assert mapped == (AuthFailedEvent(error=err), True, True)

        # 4. Session expired
        mapped = _exception_to_event(
            SessionExpiredError("expired"), TimeoutError, 99, None, logger
        )
        assert mapped == (SessionExpiredEvent(session_id=99), True, False)

        # 5. RWServerAvailable
        mapped = _exception_to_event(
            RWServerAvailable(), TimeoutError, 99, ("10.0.0.2", 2181), logger
        )
        assert mapped == (
            RWServerFoundEvent(server=("10.0.0.2", 2181)),
            True,
            False,
        )

        # 6. Unhandled
        assert (
            _exception_to_event(
                RuntimeError("boom"), TimeoutError, 99, None, logger
            )
            is None
        )

    def test_persistent_selector_lifecycle(self) -> None:
        import socket

        from kazoo.protocol.statemachine import CloseSocket

        client = KazooClient(hosts="127.0.0.1:2181")
        conn = client._connection
        try:
            with patch.object(client.handler, "spawn", return_value=Mock()):
                conn.start()

            sel = conn._selector
            assert sel is not None
            # Registered per connection attempt, not by start()
            assert conn._wakeup_registered is False

            # Register both sockets via _setup_selector
            s_a, s_b = socket.socketpair()
            try:
                fd_a = s_a.fileno()
                conn._socket = s_a
                conn._setup_selector()
                assert fd_a in sel.get_map()
                assert len(sel.get_map()) == 2

                # CloseSocket unregisters _socket, keeping the selector open
                conn.execute_effect(CloseSocket(reason="drop"))
                assert conn._selector is sel
                assert conn._socket is None
                assert s_a.fileno() == -1
                assert fd_a not in sel.get_map()
                assert len(sel.get_map()) == 1
                assert conn._wakeup_registered is True
            finally:
                s_a.close()
                s_b.close()

            # close() shuts down the selector cleanly
            conn.connection_stopped.set()
            conn._connection_routine = None
            conn.close()
            assert conn._selector is None
        finally:
            conn.connection_stopped.set()
            conn._connection_routine = None
            if not conn.connection_closed.is_set():
                conn.close()


class TestConnectionRecvIntoAndBurstScheduling:
    @staticmethod
    def _make_getdata_wire(xid: int, payload: bytes) -> bytes:
        st = stat_struct.pack(1, 2, 3, 4, 1, 0, 0, 0, len(payload), 0, 5)
        body = (
            reply_header_struct.pack(xid, xid * 10, 0)
            + _write_buffer(payload)
            + st
        )
        return int_struct.pack(len(body)) + body

    def test_coalesced_burst_replies_bypass_select(self) -> None:
        client = _make_mock_client(concurrent_request_limit=10)
        client._state_machine.state = Connected()
        conn = ConnectionHandler(client, KazooRetry())
        conn._socket = Mock()

        frames: list[bytes] = []
        results: list[Mock] = []
        for xid in range(1, 6):
            req = GetData(f"/k{xid}", None)
            async_obj = Mock()
            results.append(async_obj)
            conn._requests.pending.append((req, async_obj, xid))
            frames.append(self._make_getdata_wire(xid, f"v{xid}".encode()))

        burst = b"".join(frames)
        called = False

        def fake_recv_into(buf: memoryview) -> int:
            nonlocal called
            if not called:
                called = True
                buf[: len(burst)] = burst
                return len(burst)
            raise BlockingIOError

        conn._socket.recv_into.side_effect = fake_recv_into

        with patch.object(
            conn, "_select", return_value={"zk_socket"}
        ) as mock_select:
            for _ in range(5):
                conn._poll_step()
            assert mock_select.call_count == 1
            for i, async_obj in enumerate(results, start=1):
                async_obj.set.assert_called_once()
                assert async_obj.set.call_args[0][0][0] == f"v{i}".encode()

            # Once all buffered replies are drained, the next cycle selects
            mock_select.return_value = set()
            conn._poll_step()
            assert mock_select.call_count == 2

    def test_queued_requests_interleaved_between_buffered_replies(
        self,
    ) -> None:
        client = _make_mock_client(concurrent_request_limit=10)
        client._state_machine.state = Connected()
        conn = ConnectionHandler(client, KazooRetry())
        conn._socket = Mock()

        frames: list[bytes] = []
        for xid in (1, 2, 3):
            conn._requests.pending.append(
                (GetData(f"/n{xid}", None), Mock(), xid)
            )
            frames.append(self._make_getdata_wire(xid, f"d{xid}".encode()))
        burst = b"".join(frames)

        def fake_recv_into(buf: memoryview) -> int:
            buf[: len(burst)] = burst
            return len(burst)

        conn._socket.recv_into.side_effect = fake_recv_into

        with (
            patch.object(conn, "_select", return_value={"zk_socket"}),
            patch.object(
                conn,
                "_send_request",
                side_effect=lambda *_: conn._requests.queue.popleft(),
            ) as mock_send,
        ):
            # Step 1: reads all 3 frames in one recv_into and processes #1
            conn._poll_step()
            assert mock_send.call_count == 0
            assert conn._frame_decoder.frame_ready is True

            # Queue a new request while frames #2 and #3 remain buffered
            conn._requests.queue.append((Ping(), Mock()))
            conn._poll_step()
            assert mock_send.call_count == 1
            assert conn._frame_decoder.frame_ready is True

            conn._poll_step()
            assert mock_send.call_count == 1
            assert conn._frame_decoder.frame_ready is False

    def test_fast_path_send_drains_wakeup_and_unblocks_close(self) -> None:
        """Fast-path send drains wakeup pipe and bypasses limit for Close."""
        import socket

        client = KazooClient(
            hosts="127.0.0.1:2181", concurrent_request_limit=1
        )
        conn = client._connection
        client._state_machine.state = Connected(host="127.0.0.1", port=2181)
        s_a, s_b = socket.socketpair()
        try:
            s_a.setblocking(False)
            s_b.setblocking(False)
            conn._read_sock, conn._write_sock = (
                client.handler.create_socket_pair()
            )
            conn._read_sock.setblocking(False)
            conn._write_sock.setblocking(False)
            conn._selector = client.handler.create_selector()
            conn._socket = s_a
            conn._last_send = time.monotonic()
            conn._setup_selector()

            # 1. Enqueue + wake(), then _poll_step() must drain wakeup so
            # _select(0.0) does NOT wake spuriously with {"wakeup"}.
            conn._requests.enqueue(Ping(), None)
            conn.wake()
            conn._connect_timeout = 10.0
            conn._read_timeout = 10.0
            with patch.object(
                conn, "_select", return_value=set()
            ) as mock_select:
                conn._poll_step()
                # The wakeup byte written before _poll_step was already drained
                assert mock_select.call_count == 1
                assert len(conn._requests.pending) == 1
            assert conn._select(0.0) == set()

            # 2. Saturated pipeline (pending == limit=1): CloseRequested sets
            # is_connecting_allowed=False and enqueues Close(); _poll_step()
            # must send Close() immediately despite being at limit.
            client.send_event(CloseRequested())
            assert len(conn._requests.queue) == 1
            with patch.object(conn, "_select", return_value=set()):
                conn._poll_step()
            assert len(conn._requests.queue) == 0
            assert len(conn._requests.pending) == 2
            assert conn._select(0.0) == set()
        finally:
            conn._close_selector()
            s_a.close()
            s_b.close()
            if conn._read_sock is not None:
                conn._read_sock.close()
            if conn._write_sock is not None:
                conn._write_sock.close()

    def test_replies_per_yield_triggers_yield_control_and_resets(self) -> None:
        client = _make_mock_client(concurrent_request_limit=200)
        client._state_machine.state = Connected()
        conn = ConnectionHandler(client, KazooRetry())
        conn._socket = Mock()

        count = conn._REPLIES_PER_YIELD + 2
        frames: list[bytes] = []
        for xid in range(1, count + 1):
            conn._requests.pending.append(
                (GetData(f"/r{xid}", None), Mock(), xid)
            )
            frames.append(self._make_getdata_wire(xid, b"ok"))
        burst = b"".join(frames)

        def fake_recv_into(buf: memoryview) -> int:
            buf[: len(burst)] = burst
            return len(burst)

        conn._socket.recv_into.side_effect = fake_recv_into

        with patch.object(conn, "_select", return_value={"zk_socket"}):
            for _ in range(count):
                conn._poll_step()

        assert client.handler.yield_control.call_count == 1
        assert conn._replies_since_select == 1

        # Next empty _poll_step selects and resets _replies_since_select
        with patch.object(conn, "_select", return_value=set()):
            conn._poll_step()
        assert conn._replies_since_select == 0

        # _close_socket also resets _replies_since_select
        conn._replies_since_select = 17
        conn._close_socket()
        assert conn._replies_since_select == 0

    def test_eof_with_partial_frame_resets_decoder(self) -> None:
        client = _make_mock_client()
        conn = ConnectionHandler(client, KazooRetry())
        conn._socket = Mock()

        partial_hdr = int_struct.pack(64)[:2]
        calls = 0

        def fake_recv_into(buf: memoryview) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                buf[: len(partial_hdr)] = partial_hdr
                return len(partial_hdr)
            return 0

        conn._socket.recv_into.side_effect = fake_recv_into

        with pytest.raises(
            ConnectionDropped, match="socket connection broken"
        ):
            conn._read_packet(timeout=1.0)

        assert conn._frame_decoder._head == 0
        assert conn._frame_decoder._tail == 0
        assert conn._frame_decoder._scan == 0
        assert conn._frame_decoder.frame_ready is False

    def test_missing_yield_control_fails_at_construction(self) -> None:
        class MinimalHandler:
            timeout_exception = TimeoutError

            def lock_object(self) -> threading.Lock:
                return threading.Lock()

            def event_object(self) -> threading.Event:
                return threading.Event()

        client = _make_mock_client()
        client.handler = MinimalHandler()
        with pytest.raises(AttributeError, match="yield_control"):
            ConnectionHandler(client, KazooRetry())

    def test_read_packet_returns_none_on_partial_read(self) -> None:
        client = _make_mock_client()
        conn = ConnectionHandler(client, KazooRetry())
        conn._socket = Mock()
        conn._requests.pending.append((GetData("/x", None), Mock(), 7))

        wire = self._make_getdata_wire(7, b"x" * 100)
        pieces = [wire[:10], wire[10:50], wire[50:]]
        script: list[bytes | None] = [
            pieces[0],
            None,
            pieces[1],
            None,
            pieces[2],
        ]

        def fake_recv_into(buf: memoryview) -> int:
            piece = script.pop(0)
            if piece is None:
                raise BlockingIOError()
            buf[: len(piece)] = piece
            return len(piece)

        conn._socket.recv_into.side_effect = fake_recv_into

        assert conn._read_packet(timeout=2.5) is None
        assert conn._read_packet(timeout=2.5) is None
        pkt = conn._read_packet(timeout=2.5)

        assert isinstance(pkt, ResponsePacket)
        assert pkt.header.xid == 7
        assert pkt.response[0] == b"x" * 100
        client.handler.select.assert_not_called()

    def test_read_packet_ssl_want_read_returns_none(self) -> None:
        import ssl

        client = _make_mock_client()
        conn = ConnectionHandler(client, KazooRetry())
        conn._socket = Mock()
        conn._socket_is_ssl = True
        conn._socket.pending.return_value = 0
        conn._socket.recv_into.side_effect = ssl.SSLWantReadError(
            ssl.SSL_ERROR_WANT_READ, "want read"
        )

        assert conn._read_packet(timeout=2.5) is None
        client.handler.select.assert_not_called()

    def test_oversize_reply_over_read_window_drains_without_blocking(
        self,
    ) -> None:
        """Oversize reply > READ_WINDOW drains _skip across _poll_step()."""
        import socket

        from kazoo.protocol.serialization import READ_WINDOW

        client = KazooClient(hosts="127.0.0.1:2181")
        conn = client._connection
        client._state_machine.state = Connected(
            host="127.0.0.1", port=2181, session_id=123
        )
        s_client, s_server = socket.socketpair()
        try:
            s_client.setblocking(False)
            s_server.setblocking(False)
            conn._selector = client.handler.create_selector()
            conn._read_sock, conn._write_sock = (
                client.handler.create_socket_pair()
            )
            conn._read_sock.setblocking(False)
            conn._write_sock.setblocking(False)
            conn._socket = s_client
            conn._read_timeout = 1.0
            conn._last_send = time.monotonic()
            conn._setup_selector()

            # Oversize reply larger than default max_buffer + REPLY_OVERHEAD
            # AND larger than READ_WINDOW (256 KiB), with no trailing frame.
            oversize_payload = b"A" * (JUTE_MAX_BUFFER + 1024)
            oversize_wire = self._make_getdata_wire(1, oversize_payload)
            assert len(oversize_wire) > READ_WINDOW

            res1 = client.handler.async_result()
            conn._requests.pending.append((GetData("/big", None), res1, 1))

            # Stream oversize_wire from a background thread while _poll_step
            # drains the socket without ever blocking in _read_packet().
            sent_all = threading.Event()

            def send_all() -> None:
                s_server.setblocking(True)
                s_server.sendall(oversize_wire)
                sent_all.set()

            sender = threading.Thread(target=send_all, daemon=True)
            sender.start()

            deadline = time.monotonic() + 5.0
            while not (sent_all.is_set() and conn._frame_decoder._skip == 0):
                assert time.monotonic() < deadline
                conn._poll_step()
            sender.join(timeout=2.0)

            assert res1.ready()
            assert isinstance(res1.exception, ReplyTooLargeError)
            assert isinstance(client.state_machine, Connected)

            # Follow-up reply succeeds on the same session without stall.
            res2 = client.handler.async_result()
            conn._requests.pending.append((GetData("/ok", None), res2, 2))
            s_server.sendall(self._make_getdata_wire(2, b"ok"))
            conn._poll_step()
            assert res2.ready()
            assert res2.get_nowait()[0] == b"ok"
            assert isinstance(client.state_machine, Connected)
        finally:
            conn._close_selector()
            s_client.close()
            s_server.close()
            if conn._read_sock is not None:
                conn._read_sock.close()
            if conn._write_sock is not None:
                conn._write_sock.close()

    def test_read_response_error_and_max_buffer_plumbing(self) -> None:
        assert issubclass(ReplyTooLargeError, KazooException)
        assert not issubclass(ReplyTooLargeError, ZookeeperError)

        retry = KazooRetry(max_tries=3, delay=0)
        attempts = 0

        def fail_too_large() -> None:
            nonlocal attempts
            attempts += 1
            raise ReplyTooLargeError("too large: max_buffer")

        with pytest.raises(ReplyTooLargeError):
            retry(fail_too_large)
        assert attempts == 1

        c_default = KazooClient(hosts="127.0.0.1:2181")
        assert c_default.max_buffer == JUTE_MAX_BUFFER
        assert c_default._connection._frame_decoder._max_buffer == (
            JUTE_MAX_BUFFER
        )

        c_custom = KazooClient(hosts="127.0.0.1:2181", max_buffer=4096)
        assert c_custom.max_buffer == 4096
        assert c_custom._connection._frame_decoder._max_buffer == 4096

        with pytest.raises(ConfigurationError):
            KazooClient(hosts="127.0.0.1:2181", max_buffer=0)
        with pytest.raises(ConfigurationError):
            KazooClient(hosts="127.0.0.1:2181", max_buffer=-5)

        client = _make_mock_client()
        conn = ConnectionHandler(client, KazooRetry())
        watcher = Mock()
        async_obj = Mock()
        req = GetData("/z", watcher)
        err = ReplyTooLargeError("exceeds max_buffer")
        pkt = ResponsePacket(
            header=ReplyHeader(xid=1, zxid=55, err=0),
            request=req,
            async_object=async_obj,
            error=err,
        )
        assert conn._read_response(pkt) is None
        assert client.last_zxid == 55
        async_obj.set_exception.assert_called_once_with(err)
        assert "/z" not in conn._watchers.data_watchers

        close_pkt = ResponsePacket(
            header=ReplyHeader(xid=2, zxid=56, err=0),
            request=Close(),
            async_object=None,
            error=ValueError("bad close"),
        )
        assert conn._read_response(close_pkt) is CLOSE_RESPONSE
        assert client.last_zxid == 56
        assert client.send_event.call_count == 1


class TestWakeupOutsideSelect:
    @staticmethod
    def _assert_wakeup_released(conn: ConnectionHandler) -> None:
        assert conn._wakeup_registered is False
        assert conn._read_sock is not None
        with pytest.raises(BlockingIOError):
            conn._read_sock.recv(1)

    def test_routine_exit_releases_wakeup(self) -> None:
        """Nothing is left registered or unread when retries run out."""
        client = KazooClient(hosts="127.0.0.1:2181")
        conn = client._connection
        try:
            with patch.object(client.handler, "spawn", return_value=Mock()):
                conn.start()
            with patch.object(
                conn, "_connect_loop", side_effect=RetryFailedError("done")
            ):
                conn.zk_loop()

            assert isinstance(client.state_machine, Inactive)
            self._assert_wakeup_released(conn)
        finally:
            conn._connection_routine = None
            conn.close()

    def test_stop_after_routine_exit_releases_wakeup(self) -> None:
        """stop() cleans up the wake-up it sent to a finished routine."""
        client = KazooClient(hosts="127.0.0.1:2181")
        conn = client._connection
        try:
            with patch.object(client.handler, "spawn", return_value=Mock()):
                conn.start()
            # The routine returned on its own: authentication failed.
            client._state_machine.state = AuthFailed()
            conn.connection_stopped.set()

            client.stop()

            assert isinstance(client.state_machine, Inactive)
            self._assert_wakeup_released(conn)
        finally:
            conn.connection_stopped.set()
            conn._connection_routine = None
            conn.close()

    def test_gevent_wake_during_backoff_does_not_spin_hub(self) -> None:
        """A wake byte sent while the routine backs off costs no CPU.

        gevent's selector keeps watching registered sockets between
        select() calls, so a readable wakeup socket that nobody selects
        on would make the hub loop spin.
        """
        gevent = pytest.importorskip("gevent")
        gevent_event = pytest.importorskip("gevent.event")
        gevent_server = pytest.importorskip("gevent.server")

        from kazoo.handlers.gevent import SequentialGeventHandler

        def read_request_then_drop(sock: Any, address: Any) -> None:
            # Reading first makes the client fail in select(), with the
            # wakeup socket registered, and not in connect() or send().
            sock.recv(4096)
            sock.close()

        server = gevent_server.StreamServer(
            ("127.0.0.1", 0), read_request_then_drop
        )
        server.start()
        backing_off = gevent_event.Event()

        def backoff_sleep(seconds: float) -> None:
            backing_off.set()
            gevent.sleep(seconds)

        class Handler(SequentialGeventHandler):
            sleep_func = staticmethod(backoff_sleep)

        handler = Handler()
        client = KazooClient(
            hosts=f"127.0.0.1:{server.server_port}",
            handler=handler,
            connection_retry={
                "max_tries": -1,
                "delay": 30,
                "max_jitter": 0.0,
                "sleep_func": handler.sleep_func,
            },
        )
        try:
            client.start_async()
            assert backing_off.wait(5)

            # What submitting a request during an outage does.
            assert client._connection.wake() is True
            cpu_before = time.process_time()
            gevent.sleep(0.2)
            assert time.process_time() - cpu_before < 0.05
        finally:
            client.stop()
            client.close()
            server.stop()

    def test_stop_timeout_does_not_block_on_unbounded_join(self) -> None:
        """stop(timeout) returns False without hanging on a stuck routine."""
        client = KazooClient(hosts="127.0.0.1:2181")
        conn = client._connection
        release = threading.Event()
        stuck_thread = threading.Thread(target=release.wait, daemon=True)
        stuck_thread.start()
        try:
            conn.connection_stopped.clear()
            conn._connection_routine = stuck_thread

            start = time.monotonic()
            stopped = conn.stop(timeout=0.05)
            elapsed = time.monotonic() - start

            assert stopped is False
            assert elapsed < 0.5
            assert conn._connection_routine is stuck_thread
        finally:
            release.set()
            stuck_thread.join(timeout=2.0)
            conn.connection_stopped.set()
            conn._connection_routine = None

    @pytest.mark.parametrize("backend", ["gevent", "eventlet"])
    @pytest.mark.parametrize(
        "phase", ["handshaking", "authenticating", "connected_backpressured"]
    )
    def test_greenlet_stop_wakes_in_flight_select(
        self, backend: str, phase: str
    ) -> None:
        """stop() wakes an in-flight select() in <0.5s across greenlets."""
        from kazoo.protocol.statemachine import (
            Authenticating,
            AuthStage,
            Handshaking,
        )

        if backend == "gevent":
            mod = pytest.importorskip("gevent")
            from kazoo.handlers.gevent import SequentialGeventHandler

            handler: Any = SequentialGeventHandler()
        else:
            mod = pytest.importorskip("eventlet")
            from kazoo.handlers.eventlet import SequentialEventletHandler

            handler = SequentialEventletHandler()

        client = KazooClient(
            hosts="127.0.0.1:2181",
            handler=handler,
            concurrent_request_limit=1,
            timeout=30.0,
        )
        conn = client._connection
        s_a, s_b = handler.create_socket_pair()
        try:
            s_a.setblocking(False)
            s_b.setblocking(False)
            conn._read_sock, conn._write_sock = handler.create_socket_pair()
            conn._read_sock.setblocking(False)
            conn._write_sock.setblocking(False)
            conn._selector = handler.create_selector()
            conn._socket = s_a
            conn._connect_timeout = 30.0
            conn._read_timeout = 30.0
            conn._last_send = time.monotonic()
            conn._setup_selector()
            conn.connection_closed.clear()
            conn.connection_stopped.clear()

            if phase == "handshaking":
                client._state_machine.state = Handshaking(
                    host="127.0.0.1", port=2181
                )
                # Stray wake during handshaking must drain without
                # unregistering the wakeup pipe.
                conn.wake()
                conn._poll_step()
                assert conn._wakeup_registered is True
            elif phase == "authenticating":
                client._state_machine.state = Authenticating(
                    stage=AuthStage.SASL, host="127.0.0.1", port=2181
                )
            else:
                client._state_machine.state = Connected(
                    host="127.0.0.1", port=2181
                )
                conn._requests.pending.append((Ping(), None, 1))

            in_select = handler.event_object()
            loop_done = handler.event_object()
            orig_select = conn._select

            def tracked_select(timeout: float) -> set[str]:
                in_select.set()
                return orig_select(timeout)

            conn._select = tracked_select  # type: ignore[method-assign]

            def run_loop() -> None:
                try:
                    conn._run_io_loop()
                finally:
                    conn._close_socket()
                    conn._release_wakeup()
                    conn.connection_stopped.set()
                    loop_done.set()

            conn._connection_routine = handler.spawn(run_loop)
            assert in_select.wait(timeout=2.0)
            # Let the spawned greenlet enter the blocking select() call.
            mod.sleep(0.02)

            start = time.monotonic()
            client.send_event(CloseRequested())
            if phase == "connected_backpressured":
                # Connected CloseRequested enqueues Close and wakes the loop;
                # simulate server closing the socket right after receiving it.
                mod.sleep(0.02)
                s_b.close()
            assert loop_done.wait(timeout=1.0)
            elapsed = time.monotonic() - start
            assert elapsed < 0.5
        finally:
            conn.connection_stopped.set()
            conn._connection_routine = None
            conn._close_socket()
            conn._release_wakeup()
            conn.close()
            try:
                s_a.close()
            except Exception:
                pass
            try:
                s_b.close()
            except Exception:
                pass

    def test_sasl_init_and_response_error_normalization(self) -> None:
        """SASLExchange.__init__ errors and SASLResponse codes never crash."""
        from kazoo.exceptions import (
            AuthFailedError,
            ConnectionLoss,
            MarshallingError,
            SessionExpiredError,
            SessionMovedError,
            UnimplementedError,
        )
        from kazoo.protocol.sasl import SASLConfig, SASLExchange
        from kazoo.protocol.serialization import SASLResponse

        cfg = SASLConfig(mechanism="GSSAPI")

        def failing_factory(**kwargs: Any) -> Any:
            raise OSError("kerberos GSSError init failed")

        with pytest.raises(SASLException, match="SASL init failed"):
            SASLExchange(cfg, "127.0.0.1", client_factory=failing_factory)

        from kazoo.protocol.statemachine import Authenticating, AuthStage

        client = _make_mock_client()
        client._state_machine.state = Authenticating(
            stage=AuthStage.SASL, host="127.0.0.1", port=2181
        )
        conn = ConnectionHandler(client, KazooRetry())

        # Permanent/unknown codes raise AuthFailedError
        for code in (UnimplementedError.code, MarshallingError.code, -999):
            with pytest.raises(AuthFailedError):
                conn._process_packet(SASLResponse(xid=1, zxid=10, err=code))

        # Transient transport/session codes raise ConnectionDropped
        for code in (ConnectionLoss.code, SessionMovedError.code):
            with pytest.raises(ConnectionDropped):
                conn._process_packet(SASLResponse(xid=1, zxid=11, err=code))

        # SessionExpiredError propagates for reconnect handling
        with pytest.raises(SessionExpiredError):
            conn._process_packet(
                SASLResponse(xid=1, zxid=12, err=SessionExpiredError.code)
            )

    def test_pre_session_frames_rejected_and_handshake_deadline_bounded(
        self,
    ) -> None:
        """Pre-session Watch/Ping/Auth frames raise ConnectionDropped."""
        from kazoo.protocol.serialization import (
            AuthResponse,
            PingResponse,
            WatchPacket,
        )
        from kazoo.protocol.statemachine import (
            Authenticating,
            AuthStage,
            Handshaking,
        )

        client = KazooClient(hosts="127.0.0.1:2181")
        conn = client._connection
        watcher = Mock()
        conn._watchers.data_watchers["/z"].add(watcher)
        client.last_zxid = 5

        # 1. During Handshaking: WatchPacket, PingResponse, AuthResponse fail
        # with ConnectionDropped without popping watchers or mutating zxid.
        client._state_machine.state = Handshaking(host="127.0.0.1", port=2181)
        for bad_pkt in (
            WatchPacket(zxid=10, type=1, state=3, path="/z"),
            PingResponse(zxid=10, err=0),
            AuthResponse(zxid=99, err=-115),
            AuthResponse(zxid=99, err=0),
        ):
            with patch.object(conn, "_read_packet", return_value=bad_pkt):
                with pytest.raises(ConnectionDropped):
                    conn.execute_effect(ReadConnectResponsePacket())
        assert watcher in conn._watchers.data_watchers["/z"]
        assert client.last_zxid == 5
        assert isinstance(client.state_machine, Handshaking)

        # 2. During Authenticating(SASL): Watch/Ping/Auth are rejected.
        client._state_machine.state = Authenticating(
            stage=AuthStage.SASL, host="127.0.0.1", port=2181
        )
        for bad_pkt in (
            WatchPacket(zxid=10, type=1, state=3, path="/z"),
            PingResponse(zxid=10, err=0),
            AuthResponse(zxid=99, err=-115),
        ):
            with pytest.raises(ConnectionDropped):
                conn._process_packet(bad_pkt)
        assert watcher in conn._watchers.data_watchers["/z"]
        assert client.last_zxid == 5
        assert isinstance(client.state_machine, Authenticating)

        # 3. Handshaking monotonic deadline expires even when trickle reads
        # return None on ready socket.
        client._state_machine.state = Handshaking(host="127.0.0.1", port=2181)
        conn._connect_timeout = 5.0
        conn._phase_deadline = 100.0
        with patch("time.monotonic", return_value=98.0):
            timeout, deadline = conn._compute_poll_timeout()
            assert timeout == pytest.approx(2.0)
            assert deadline is None

        with (
            patch("time.monotonic", return_value=101.0),
            patch.object(conn, "_read_packet", return_value=None),
        ):
            conn._dispatch_ready_events({"zk_socket"}, None)
        assert isinstance(client.state_machine, Connecting)

    def test_unknown_header_error_resolves_async_result_without_crashing(
        self,
    ) -> None:
        """Unknown wire error codes fail request with SystemZookeeperError."""
        from kazoo.exceptions import SystemZookeeperError

        client = _make_mock_client()
        conn = ConnectionHandler(client, KazooRetry())
        async_obj = Mock()
        pkt = ResponsePacket(
            header=ReplyHeader(xid=1, zxid=42, err=-999),
            request=GetData("/x", None),
            async_object=async_obj,
        )
        assert conn._read_response(pkt) is None
        async_obj.set_exception.assert_called_once()
        err = async_obj.set_exception.call_args[0][0]
        assert isinstance(err, SystemZookeeperError)
        assert "-999" in str(err)

    def test_runtime_auth_pending_isolated_from_positive_xid_fifo(
        self,
    ) -> None:
        """Out-of-order GetData reply before AuthResponse does not mismatch."""
        from kazoo.protocol.serialization import Auth

        client = KazooClient(hosts="127.0.0.1:2181")
        conn = client._connection
        client._state_machine.state = Connected(host="127.0.0.1", port=2181)
        conn._socket = Mock()
        conn._socket.send.side_effect = lambda v: len(v)
        conn._requests.reset_xid(0)

        auth_res = client.handler.async_result()
        get_res = client.handler.async_result()
        conn._requests.enqueue(Auth(0, "digest", "u:p"), auth_res)
        conn._requests.enqueue(GetData("/k", None), get_res)

        conn._send_request(10.0)
        assert len(conn._requests.auth_pending) == 1
        assert len(conn._requests.pending) == 1

        # Server answers GetData (xid=1) BEFORE AuthResponse (xid=-4).
        getdata_wire = (
            TestConnectionRecvIntoAndBurstScheduling._make_getdata_wire(
                1, b"val"
            )
        )
        auth_body = reply_header_struct.pack(-4, 20, 0)
        auth_wire = int_struct.pack(len(auth_body)) + auth_body

        conn._frame_decoder._feed(getdata_wire + auth_wire)
        conn._read_socket()
        conn._read_socket()

        assert get_res.get_nowait()[0] == b"val"
        assert auth_res.get_nowait() is True
        assert len(conn._requests.pending) == 0
        assert len(conn._requests.auth_pending) == 0

    def test_outbound_wakeups_do_not_starve_heartbeat_or_dead_peer_timeout(
        self,
    ) -> None:
        """Continuous outbound requests still ping and time out silent peer."""
        client = KazooClient(hosts="127.0.0.1:2181")
        conn = client._connection
        client._state_machine.state = Connected(host="127.0.0.1", port=2181)
        conn._socket = Mock()
        conn._socket.send.side_effect = lambda v: len(v)
        conn._read_timeout = 10.0
        conn._last_send = 100.0
        conn._last_recv = 100.0

        # At t=106 (past read_timeout/2) with continuous wakeup sends advancing
        # _last_send, heartbeat ping must still be emitted.
        conn._requests.enqueue(GetData("/a", None), Mock())
        with patch("time.monotonic", return_value=106.0):
            _, deadline = conn._compute_poll_timeout()
            conn._dispatch_ready_events({"wakeup"}, deadline)
        assert isinstance(client.state_machine, Connected)
        assert client.state_machine.ping_outstanding is True

        # At t=111 (past read_timeout since _last_recv=100) even with wakeup
        # ready, connection must raise timeout_exception.
        conn._requests.enqueue(GetData("/b", None), Mock())
        with patch("time.monotonic", return_value=111.0):
            _, deadline = conn._compute_poll_timeout()
            with pytest.raises(client.handler.timeout_exception):
                conn._dispatch_ready_events({"wakeup"}, deadline)

    def test_auth_response_transient_vs_permanent_errors(self) -> None:
        """AuthResponse separates transient drops from permanent AuthFailed."""
        from kazoo.exceptions import (
            AuthFailedError,
            SessionExpiredError,
            SessionMovedError,
        )
        from kazoo.protocol.serialization import AuthResponse

        client = KazooClient(hosts="127.0.0.1:2181")
        conn = client._connection
        client._state_machine.state = Connected(host="127.0.0.1", port=2181)

        # Transient SessionExpiredError -> raises SessionExpiredError
        res_exp = client.handler.async_result()
        conn._requests.auth_pending.append(res_exp)
        with pytest.raises(SessionExpiredError):
            conn._process_packet(
                AuthResponse(zxid=1, err=SessionExpiredError.code)
            )
        assert isinstance(res_exp.exception, SessionExpiredError)
        assert isinstance(client.state_machine, Connected)

        # Transient SessionMovedError -> raises ConnectionDropped (reconnects)
        res_moved = client.handler.async_result()
        conn._requests.auth_pending.append(res_moved)
        with pytest.raises(ConnectionDropped):
            conn._process_packet(
                AuthResponse(zxid=2, err=SessionMovedError.code)
            )
        assert isinstance(res_moved.exception, SessionMovedError)
        assert isinstance(client.state_machine, Connected)

        # Permanent AuthFailedError -> transitions to terminal AuthFailed
        res_fail = client.handler.async_result()
        conn._requests.auth_pending.append(res_fail)
        conn._process_packet(AuthResponse(zxid=3, err=AuthFailedError.code))
        assert isinstance(res_fail.exception, AuthFailedError)
        assert isinstance(client.state_machine, AuthFailed)

    def test_zk_loop_sets_connection_stopped_after_teardown(self) -> None:
        """connection_stopped is set only after SessionClosed & release."""
        from kazoo.protocol.states import KazooState

        client = KazooClient(hosts="127.0.0.1:2181")
        conn = client._connection
        observed: list[tuple[Any, bool]] = []
        orig_release = conn._release_wakeup

        def spy_release() -> None:
            observed.append(("release", conn.connection_stopped.is_set()))
            orig_release()

        client.add_listener(
            lambda s: observed.append((s, conn.connection_stopped.is_set()))
        )
        try:
            with patch.object(client.handler, "spawn", return_value=Mock()):
                conn.start()
            client._state_machine.state = Connected(
                host="127.0.0.1", port=2181
            )
            client._last_notified_state = KazooState.CONNECTED
            with (
                patch.object(
                    conn, "_connect_loop", side_effect=RetryFailedError("done")
                ),
                patch.object(conn, "_release_wakeup", side_effect=spy_release),
            ):
                conn.zk_loop()
            assert conn.connection_stopped.is_set() is True
            assert (KazooState.LOST, False) in observed
            assert ("release", False) in observed
        finally:
            conn.connection_stopped.set()
            conn._connection_routine = None
            conn.close()

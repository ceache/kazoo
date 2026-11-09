from __future__ import annotations

import collections
from typing import Any
from unittest.mock import Mock, patch

import pytest

from kazoo.client import KazooClient
from kazoo.exceptions import (
    SASLException,
    SessionClosedRequireSaslError,
)
from kazoo.protocol.connection import (
    ConnectionHandler,
    STOP_CONNECTING,
)
from kazoo.protocol.sasl import SASLConfig
from kazoo.protocol.serialization import (
    Connect,
    ReplyHeader,
    ReplyPacket,
    Watch,
    int_int_long_struct,
    int_struct,
    _write_buffer,
)
from kazoo.protocol.states import KeeperState
from kazoo.protocol.statemachine import (
    AuthFailed,
    AuthFailedEvent,
    ConnectAttemptStarted,
    Connected,
    Connecting,
    ConnectResponseReceived,
    HeartbeatDeadlineReached,
    Inactive,
    PollTimeoutExpired,
    ReadConnectResponsePacket,
    ResetSessionData,
    SendConnectPacket,
    SocketReadable,
    UpdateSendCapacity,
    WakeUpSignaled,
)
from kazoo.retry import KazooRetry


def _make_mock_client(**kwargs: Any) -> Mock:
    client = Mock()
    handler = Mock()
    handler.timeout_exception = TimeoutError
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
    sm = Mock()
    sm.state = Connecting()
    client._state_machine = sm
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
        client = Mock()
        retry = KazooRetry()
        cfg = SASLConfig(
            mechanism="DIGEST-MD5", username="user", password="pwd"
        )
        connection = ConnectionHandler(client, retry, sasl_config=cfg)
        assert connection.sasl_config == cfg
        assert not hasattr(connection, "sasl_options")


class TestConnectionXidMismatch:
    def test_read_response_xid_mismatch_sets_exception_and_raises(
        self,
    ) -> None:
        client = Mock()
        async_obj = Mock()
        request = Mock()
        expected_xid = 1
        client._pending = collections.deque(
            [(request, async_obj, expected_xid)]
        )

        connection = ConnectionHandler(client, KazooRetry())
        header = ReplyHeader(xid=2, zxid=100, err=0)

        with pytest.raises(RuntimeError) as exc_info:
            connection._read_response(header, b"", 0)

        assert exc_info.value.args == (
            "xids do not match, expected %r received %r",
            1,
            2,
        )
        assert client.last_zxid == 100
        async_obj.set_exception.assert_called_once_with(exc_info.value)

    def test_execute_effect_send_connect_packet(self) -> None:
        client = Mock()
        connection = ConnectionHandler(client, KazooRetry())
        connect_packet = Connect(0, 0, 10000, 0, b"", False)
        with patch.object(connection, "_submit") as mock_submit:
            connection.execute_effect(
                SendConnectPacket(connect=connect_packet, timeout=5.0)
            )
            mock_submit.assert_called_once_with(connect_packet, 5.0)

    def test_execute_effect_read_connect_response_packet(self) -> None:
        client = Mock()
        connection = ConnectionHandler(client, KazooRetry())
        body = bytearray()
        body.extend(int_int_long_struct.pack(0, 30000, 12345))
        body.extend(_write_buffer(b"password12345678"))
        body.extend([0])
        with patch.object(
            connection,
            "_read",
            side_effect=[
                int_struct.pack(len(body)),
                bytes(body),
            ],
        ):
            connection.execute_effect(ReadConnectResponsePacket())
            assert client.send_event.call_count == 1
            event = client.send_event.call_args[0][0]
            assert isinstance(event, ConnectResponseReceived)
            assert event.connect_result.session_id == 12345
            assert event.connect_result.time_out == 30000
            assert event.connect_result.passwd == b"password12345678"
            assert event.connect_result.read_only is False

    def test_execute_effect_reset_session_data(self) -> None:
        client = Mock()
        retry = Mock()
        connection = ConnectionHandler(client, KazooRetry())
        connection._current_retry = retry
        connection.execute_effect(ResetSessionData())
        retry.reset.assert_called_once()

    def test_read_socket_dispatches_to_read_response_on_mismatch(
        self,
    ) -> None:
        client = Mock()
        async_obj = Mock()
        request = Mock()
        client._pending = collections.deque([(request, async_obj, 5)])

        connection = ConnectionHandler(client, KazooRetry())
        header = ReplyHeader(xid=6, zxid=200, err=0)

        with patch.object(
            connection,
            "_read_packet",
            return_value=ReplyPacket(header=header, buffer=b"", offset=0),
        ):
            with pytest.raises(RuntimeError) as exc_info:
                connection._read_socket(read_timeout=1.0)

        assert exc_info.value.args == (
            "xids do not match, expected %r received %r",
            5,
            6,
        )
        async_obj.set_exception.assert_called_once_with(exc_info.value)


class TestPersistentWatchEvents:
    def test_find_persistent_recursive_watchers(self) -> None:
        client = Mock()
        w_root = Mock()
        w_a = Mock()
        client._persistent_recursive_watchers = {
            "/": [w_root],
            "/a": [w_a],
        }
        connection = ConnectionHandler(client, KazooRetry())

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
        client = Mock()
        client.client_state = KeeperState.CONNECTED
        client.unchroot.side_effect = lambda p: p
        w_child = Mock()
        w_pers = Mock()
        client._child_watchers = {"/a": {w_child}}
        client._persistent_watchers = {"/a": {w_pers}}
        client._persistent_recursive_watchers = {}

        connection = ConnectionHandler(client, KazooRetry())
        connection._read_watch_event(Watch(type=4, state=3, path="/a"))

        # Both child watch and persistent watch should be dispatched
        assert client.handler.dispatch_callback.call_count == 2
        callbacks = [
            call.args[0]
            for call in client.handler.dispatch_callback.call_args_list
        ]
        called_funcs = {cb.func for cb in callbacks}
        assert called_funcs == {w_child, w_pers}
        # One-time child watcher should be popped
        assert "/a" not in client._child_watchers
        # Persistent watcher should remain
        assert client._persistent_watchers["/a"] == {w_pers}

    def test_created_event_triggers_persistent_and_recursive_watchers(
        self,
    ) -> None:
        client = Mock()
        client.client_state = KeeperState.CONNECTED
        client.unchroot.side_effect = lambda p: p
        w_data = Mock()
        w_pers = Mock()
        w_rec = Mock()
        client._data_watchers = {"/a/b": {w_data}}
        client._persistent_watchers = {"/a/b": {w_pers}}
        client._persistent_recursive_watchers = {"/a": [w_rec]}

        connection = ConnectionHandler(client, KazooRetry())
        connection._read_watch_event(Watch(type=1, state=3, path="/a/b"))

        assert client.handler.dispatch_callback.call_count == 3
        callbacks = [
            call.args[0]
            for call in client.handler.dispatch_callback.call_args_list
        ]
        called_funcs = {cb.func for cb in callbacks}
        assert called_funcs == {w_data, w_pers, w_rec}
        # One-time data watcher should be popped
        assert "/a/b" not in client._data_watchers
        # Persistent and recursive watchers remain
        assert client._persistent_watchers["/a/b"] == {w_pers}
        assert client._persistent_recursive_watchers["/a"] == [w_rec]

    def test_changed_event_triggers_persistent_and_recursive_watchers(
        self,
    ) -> None:
        client = Mock()
        client.client_state = KeeperState.CONNECTED
        client.unchroot.side_effect = lambda p: p
        w_data = Mock()
        w_pers = Mock()
        w_rec = Mock()
        client._data_watchers = {"/a/b": {w_data}}
        client._persistent_watchers = {"/a/b": {w_pers}}
        client._persistent_recursive_watchers = {"/a": [w_rec]}

        connection = ConnectionHandler(client, KazooRetry())
        connection._read_watch_event(Watch(type=3, state=3, path="/a/b"))

        assert client.handler.dispatch_callback.call_count == 3
        # One-time data watcher should be popped
        assert "/a/b" not in client._data_watchers
        # Persistent and recursive watchers remain
        assert client._persistent_watchers["/a/b"] == {w_pers}
        assert client._persistent_recursive_watchers["/a"] == [w_rec]

    def test_deleted_event_triggers_all_watchers(self) -> None:
        client = Mock()
        client.client_state = KeeperState.CONNECTED
        client.unchroot.side_effect = lambda p: p
        w_data = Mock()
        w_child = Mock()
        w_pers = Mock()
        w_rec = Mock()
        client._data_watchers = {"/a": {w_data}}
        client._child_watchers = {"/a": {w_child}}
        client._persistent_watchers = {"/a": {w_pers}}
        client._persistent_recursive_watchers = {"/": [w_rec]}

        connection = ConnectionHandler(client, KazooRetry())
        connection._read_watch_event(Watch(type=2, state=3, path="/a"))

        assert client.handler.dispatch_callback.call_count == 4
        # Data and child watchers should be popped
        assert "/a" not in client._data_watchers
        assert "/a" not in client._child_watchers
        # Persistent and recursive watchers remain
        assert client._persistent_watchers["/a"] == {w_pers}
        assert client._persistent_recursive_watchers["/"] == [w_rec]

    def test_watch_event_dispatches_zxid(self) -> None:
        """Test that watch events dispatched via ConnectionHandler include the
        zxid.
        """
        client = Mock()
        client.client_state = KeeperState.CONNECTED
        client.unchroot.side_effect = lambda p: p
        watcher = Mock()
        client._data_watchers = {"/node": {watcher}}
        client._persistent_watchers = {}
        client._persistent_recursive_watchers = {}

        connection = ConnectionHandler(client, KazooRetry())
        connection._read_watch_event(
            Watch(type=1, state=3, path="/node"), zxid=99999
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
        client = Mock()
        client.client_state = KeeperState.CONNECTED
        client.unchroot.side_effect = lambda p: p
        w_pers = Mock()
        w_rec = Mock()
        client._data_watchers = {}
        client._persistent_watchers = {"/node": {w_pers}}
        client._persistent_recursive_watchers = {"/": [w_rec]}

        connection = ConnectionHandler(client, KazooRetry())
        connection._read_watch_event(
            Watch(type=3, state=3, path="/node"), zxid=88888
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
        from collections import defaultdict, deque
        from kazoo.protocol.serialization import AddWatch
        from kazoo.protocol.states import AddWatchMode

        client = Mock()
        client._persistent_watchers = defaultdict(set)
        client._persistent_recursive_watchers = defaultdict(set)

        w = Mock()
        async_obj = Mock()
        request = AddWatch("/a", w, AddWatchMode.PERSISTENT)
        client._pending = deque([(request, async_obj, 1)])

        connection = ConnectionHandler(client, KazooRetry())
        header = ReplyHeader(xid=1, zxid=10, err=0)
        connection._read_response(header, b"", 0)

        assert client._persistent_watchers["/a"] == {w}
        assert len(client._persistent_recursive_watchers) == 0
        async_obj.set.assert_called_once_with(None)

    def test_read_response_add_watch_persistent_recursive(self) -> None:
        from collections import defaultdict, deque
        from kazoo.protocol.serialization import AddWatch
        from kazoo.protocol.states import AddWatchMode

        client = Mock()
        client._persistent_watchers = defaultdict(set)
        client._persistent_recursive_watchers = defaultdict(set)

        w = Mock()
        async_obj = Mock()
        request = AddWatch("/a", w, AddWatchMode.PERSISTENT_RECURSIVE)
        client._pending = deque([(request, async_obj, 1)])

        connection = ConnectionHandler(client, KazooRetry())
        header = ReplyHeader(xid=1, zxid=10, err=0)
        connection._read_response(header, b"", 0)

        assert client._persistent_recursive_watchers["/a"] == {w}
        assert len(client._persistent_watchers) == 0

    def test_read_response_remove_watches_children(self) -> None:
        from collections import deque
        from kazoo.protocol.serialization import RemoveWatches
        from kazoo.protocol.states import WatcherType

        client = Mock()
        w1, w2 = Mock(), Mock()
        client._child_watchers = {"/a": {w1}}
        client._data_watchers = {"/a": {w2}}

        async_obj = Mock()
        request = RemoveWatches("/a", WatcherType.CHILDREN)
        client._pending = deque([(request, async_obj, 1)])

        connection = ConnectionHandler(client, KazooRetry())
        header = ReplyHeader(xid=1, zxid=10, err=0)
        connection._read_response(header, b"", 0)

        assert "/a" not in client._child_watchers
        assert client._data_watchers["/a"] == {w2}

    def test_read_response_remove_watches_data(self) -> None:
        from collections import deque
        from kazoo.protocol.serialization import RemoveWatches
        from kazoo.protocol.states import WatcherType

        client = Mock()
        w1, w2 = Mock(), Mock()
        client._child_watchers = {"/a": {w1}}
        client._data_watchers = {"/a": {w2}}

        async_obj = Mock()
        request = RemoveWatches("/a", WatcherType.DATA)
        client._pending = deque([(request, async_obj, 1)])

        connection = ConnectionHandler(client, KazooRetry())
        header = ReplyHeader(xid=1, zxid=10, err=0)
        connection._read_response(header, b"", 0)

        assert "/a" not in client._data_watchers
        assert client._child_watchers["/a"] == {w1}

    def test_read_response_remove_watches_any(self) -> None:
        from collections import deque
        from kazoo.protocol.serialization import RemoveWatches
        from kazoo.protocol.states import WatcherType

        client = Mock()
        w1, w2, w3, w4 = Mock(), Mock(), Mock(), Mock()
        client._child_watchers = {"/a": {w1}}
        client._data_watchers = {"/a": {w2}}
        client._persistent_watchers = {"/a": {w3}}
        client._persistent_recursive_watchers = {"/a": {w4}}

        async_obj = Mock()
        request = RemoveWatches("/a", WatcherType.ANY)
        client._pending = deque([(request, async_obj, 1)])

        connection = ConnectionHandler(client, KazooRetry())
        header = ReplyHeader(xid=1, zxid=10, err=0)
        connection._read_response(header, b"", 0)

        assert "/a" not in client._child_watchers
        assert "/a" not in client._data_watchers
        assert "/a" not in client._persistent_watchers
        assert "/a" not in client._persistent_recursive_watchers

    def test_read_response_add_watch_unexpected_mode(self) -> None:
        from collections import deque
        from kazoo.protocol.serialization import AddWatch

        client = Mock()

        w = Mock()
        async_obj = Mock()
        request = AddWatch("/a", w, 999)  # invalid mode
        client._pending = deque([(request, async_obj, 1)])

        connection = ConnectionHandler(client, KazooRetry())
        header = ReplyHeader(xid=1, zxid=10, err=0)
        with pytest.raises(ValueError, match="Unexpected AddWatchMode: 999"):
            connection._read_response(header, b"", 0)

    def test_read_response_remove_watches_unexpected_type(self) -> None:
        from collections import deque
        from kazoo.protocol.serialization import RemoveWatches

        client = Mock()

        async_obj = Mock()
        request = RemoveWatches("/a", 999)  # invalid type
        client._pending = deque([(request, async_obj, 1)])

        connection = ConnectionHandler(client, KazooRetry())
        header = ReplyHeader(xid=1, zxid=10, err=0)
        with pytest.raises(ValueError, match="Unexpected WatcherType: 999"):
            connection._read_response(header, b"", 0)


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

    def test_update_send_capacity(self) -> None:
        client = _make_mock_client()
        client.concurrent_request_limit = 5
        client._pending = [1, 2]
        conn = ConnectionHandler(client, KazooRetry())

        events: list[Any] = []
        conn._emit_event = events.append  # type: ignore[method-assign]
        conn._update_send_capacity()

        assert len(events) == 1
        assert isinstance(events[0], UpdateSendCapacity)
        assert events[0].can_send is True

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

    def test_run_io_loop_terminates_when_is_terminal(self) -> None:
        client = _make_mock_client()
        client._state_machine = Mock()
        client._state_machine.state = Connecting()
        conn = ConnectionHandler(client, KazooRetry())

        steps = 0

        def fake_poll_step() -> None:
            nonlocal steps
            steps += 1
            if steps == 3:
                client._state_machine.state = Inactive()

        conn._poll_step = fake_poll_step  # type: ignore[method-assign]
        conn._run_io_loop()
        assert steps == 3

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

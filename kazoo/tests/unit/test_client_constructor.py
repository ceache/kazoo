from __future__ import annotations

import pytest

from kazoo.client import KazooClient
from kazoo.exceptions import ConfigurationError
from kazoo.retry import KazooRetry


def test_invalid_handler() -> None:
    from kazoo.handlers.threading import (
        SequentialThreadingHandler,
    )

    with pytest.raises(ConfigurationError):
        KazooClient(
            handler=SequentialThreadingHandler,  # type: ignore[call-overload]
        )


def test_chroot() -> None:
    assert KazooClient(hosts="127.0.0.1:2181/").chroot == ""
    assert KazooClient(hosts="127.0.0.1:2181/a").chroot == "/a"
    assert KazooClient(hosts="127.0.0.1/a").chroot == "/a"
    assert KazooClient(hosts="127.0.0.1/a/b").chroot == "/a/b"
    assert (
        KazooClient(hosts="127.0.0.1:2181,127.0.0.1:2182/a/b").chroot == "/a/b"
    )


def test_connection_timeout() -> None:
    from kazoo.handlers.threading import (
        KazooTimeoutError,
    )

    client = KazooClient(hosts="127.0.0.1:9")
    assert client.handler.timeout_exception is KazooTimeoutError

    with pytest.raises(client.handler.timeout_exception):
        client.start(0.1)


def test_ordered_host_selection() -> None:
    client = KazooClient(
        hosts="127.0.0.1:9,127.0.0.2:9/a", randomize_hosts=False
    )
    hosts = [h for h in client.hosts]
    assert hosts == [("127.0.0.1", 9), ("127.0.0.2", 9)]


def test_invalid_hostname() -> None:
    client = KazooClient(hosts="nosuchhost/a")
    timeout = client.handler.timeout_exception
    with pytest.raises(timeout):
        client.start(0.1)


def test_another_invalid_hostname() -> None:
    with pytest.raises(ValueError):
        KazooClient(hosts="/nosuchhost/a")


def test_retry_options_dict() -> None:
    client = KazooClient(
        command_retry=dict(max_tries=99), connection_retry=dict(delay=99)
    )
    assert isinstance(client._conn_retry, KazooRetry)
    assert isinstance(client._retry, KazooRetry)
    assert client._retry.max_tries == 99
    assert client._conn_retry.delay == 99


def test_persistent_watchers_collections_initialized() -> None:
    client = KazooClient(hosts="127.0.0.1:2181")
    assert client._persistent_watchers == {}
    assert client._persistent_recursive_watchers == {}


def test_reset_watchers_clears_persistent_watchers() -> None:
    client = KazooClient(hosts="127.0.0.1:2181")
    dummy_cb = lambda event: None  # noqa: E731
    client._persistent_watchers["/a"].add(dummy_cb)
    client._persistent_recursive_watchers["/b"].add(dummy_cb)

    client._reset_watchers()

    assert len(client._persistent_watchers) == 0
    assert len(client._persistent_recursive_watchers) == 0


def test_client_lifecycle_properties() -> None:
    from kazoo.protocol.statemachine import (
        AuthFailed,
        Connected,
        Connecting,
        Inactive,
    )

    client = KazooClient(hosts="127.0.0.1:2181")

    # Initial state is Inactive
    assert client.connected is False
    assert client.is_stopped is True
    assert client.is_terminal is False

    # Connected state
    client._state_machine.state = Connected()
    assert client.connected is True
    assert client.is_stopped is False
    assert client.is_terminal is False

    # Connecting state
    client._state_machine.state = Connecting()
    assert client.connected is False
    assert client.is_stopped is False
    assert client.is_terminal is False

    # AuthFailed state
    client._state_machine.state = AuthFailed()
    assert client.connected is False
    assert client.is_stopped is False
    assert client.is_terminal is True

    # Inactive state & double stop no-op
    client._state_machine.state = Inactive()
    assert client.is_stopped is True
    client.stop()
    assert client.is_stopped is True


def test_start_async_during_expired_and_post_connect_suspended() -> None:
    """start_async() reuses/attaches result during reconnects without hang."""
    from kazoo.protocol.statemachine import (
        ExpiredSession,
        Suspended,
    )

    client = KazooClient(hosts="127.0.0.1:2181")
    conn = client._connection

    # 1. Duplicate start_async() during ExpiredSession while _conn_result is
    # still pending returns the existing _conn_result without stopping.
    pending = client.handler.async_result()
    client._conn_result = pending
    client._state_machine.state = ExpiredSession(session_id=1)
    conn.connection_stopped.clear()
    try:
        assert client.start_async() is pending

        # 2. Previously connected client (ready _conn_result) now reconnecting
        # in Suspended attaches a fresh pending result instead of calling
        # _safe_close() without CloseRequested().
        pending.set(True)
        client._state_machine.state = Suspended(
            host="127.0.0.1", port=2181, session_id=1
        )
        reattached = client.start_async()
        assert reattached is not pending
        assert not reattached.ready()

        # When connection re-establishes, the reattached result resolves.
        from kazoo.protocol.serialization import Connect
        from kazoo.protocol.statemachine import (
            ConnectResponseReceived,
            Handshaking,
        )

        client._state_machine.state = Handshaking(
            host="127.0.0.1", port=2181, num_hosts=1
        )
        client.send_event(
            ConnectResponseReceived(
                connect_result=Connect(0, 0, 10000, 1, b"pwd", False)
            )
        )
        assert reattached.ready()
        assert reattached.get_nowait() is True
        assert client.start_async().get_nowait() is True
    finally:
        conn.connection_stopped.set()


def test_constructor_sasl_options_and_bool_limits() -> None:
    """Validate SASLConfig edge cases and reject bool integer limits."""
    from kazoo.protocol.sasl import SASLConfig

    with pytest.raises(ConfigurationError):
        KazooClient(sasl_options={})

    with pytest.raises(ConfigurationError):
        KazooClient(max_buffer=True)

    with pytest.raises(ConfigurationError):
        KazooClient(concurrent_request_limit=True)

    cfg = SASLConfig(mechanism="DIGEST-MD5", username="u", password="p:w")
    c_cfg = KazooClient(sasl_options=cfg)
    assert c_cfg._connection.sasl_config == cfg

    c_empty_pw = KazooClient(
        sasl_options={
            "mechanism": "PLAIN",
            "username": "u",
            "password": "",
        }
    )
    assert c_empty_pw._connection.sasl_config is not None
    assert c_empty_pw._connection.sasl_config.password == ""

    cb = lambda: ("u", "p")  # noqa: E731
    c_cb = KazooClient(
        sasl_options={"mechanism": "DIGEST-MD5", "callback": cb}
    )
    assert c_cb._connection.sasl_config is not None
    assert ("callback", cb) in c_cb._connection.sasl_config.extra_options

    c_custom_mech = KazooClient(sasl_options={"mechanism": "EXTERNAL"})
    assert c_custom_mech._connection.sasl_config is not None
    assert c_custom_mech._connection.sasl_config.mechanism == "EXTERNAL"

    with pytest.deprecated_call():
        c_legacy = KazooClient(auth_data=[("sasl", "user:pass:with:colons")])
    assert c_legacy._connection.sasl_config is not None
    assert c_legacy._connection.sasl_config.username == "user"
    assert c_legacy._connection.sasl_config.password == "pass:with:colons"

    with pytest.deprecated_call(), pytest.raises(ConfigurationError):
        KazooClient(auth_data=[("sasl", "no-colon")])

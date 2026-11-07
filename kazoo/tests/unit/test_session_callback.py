from __future__ import annotations

import time
import pytest

from kazoo.client import KazooClient
from kazoo.exceptions import AuthFailedError
from kazoo.protocol.statemachine import (
    AuthFailedEvent,
    ConnectionLostEvent,
    HandshakeCompleted,
    SessionExpiredEvent,
)
from kazoo.protocol.states import KazooState


def test_session_callback_states() -> None:
    client = KazooClient()

    client.send_event(HandshakeCompleted())
    assert client.state == KazooState.CONNECTED

    client.send_event(AuthFailedEvent())
    assert client.state == KazooState.LOST

    client.send_event(ConnectionLostEvent(keeper_state=-250))
    assert client.state == KazooState.SUSPENDED


def test_start_fails_immediately_on_auth_failed() -> None:
    client = KazooClient()

    def fake_conn_start() -> None:
        client.send_event(AuthFailedEvent())

    client._connection.start = (  # type: ignore[method-assign]
        fake_conn_start
    )
    start_time = time.monotonic()
    with pytest.raises(AuthFailedError):
        client.start(timeout=10.0)
    elapsed = time.monotonic() - start_time
    assert elapsed < 1.0


def test_start_fails_immediately_with_underlying_auth_error() -> None:
    client = KazooClient()
    expected_err = AuthFailedError("custom auth error")

    def fake_conn_start() -> None:
        client.send_event(AuthFailedEvent(error=expected_err))

    client._connection.start = (  # type: ignore[method-assign]
        fake_conn_start
    )
    with pytest.raises(AuthFailedError) as exc_info:
        client.start(timeout=10.0)
    assert exc_info.value is expected_err


def test_start_async_triggers_on_auth_failed() -> None:
    client = KazooClient()
    expected_err = AuthFailedError("custom auth error")

    def fake_conn_start() -> None:
        client.send_event(AuthFailedEvent(error=expected_err))

    client._connection.start = (  # type: ignore[method-assign]
        fake_conn_start
    )
    res = client.start_async()
    assert res.ready()
    assert not res.successful()
    assert res.exception is expected_err
    assert res.wait(timeout=5.0)
    with pytest.raises(AuthFailedError) as exc_info:
        res.get()
    assert exc_info.value is expected_err


def test_start_async_success() -> None:
    client = KazooClient()

    def fake_conn_start() -> None:
        client.send_event(HandshakeCompleted())

    client._connection.start = (  # type: ignore[method-assign]
        fake_conn_start
    )
    res = client.start_async()
    assert res.ready()
    assert res.successful()
    assert res.get() is True
    assert client.connected

    # Calling start_async when already connected returns completed result
    res2 = client.start_async()
    assert res2.ready()
    assert res2.successful()
    assert res2.get() is True


def test_start_async_recovers_from_expired_session() -> None:
    client = KazooClient()

    def fake_conn_start() -> None:
        # First attempt receives EXPIRED_SESSION (not terminal)
        client.send_event(SessionExpiredEvent())
        # Second attempt receives CONNECTED
        client.send_event(HandshakeCompleted())

    client._connection.start = (  # type: ignore[method-assign]
        fake_conn_start
    )
    res = client.start_async()
    assert res.ready()
    assert res.successful()
    assert res.get() is True
    assert client.connected

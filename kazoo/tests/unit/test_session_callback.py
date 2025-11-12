from kazoo.client import KazooClient
from kazoo.protocol.states import KazooState, KeeperState


def test_session_callback_states():
    client = KazooClient()
    client._handle = 1
    client._live.set()

    result = client._session_callback(KeeperState.CONNECTED)
    assert result is None

    # Now with stopped
    client._stopped.set()
    result = client._session_callback(KeeperState.CONNECTED)
    assert result is None

    # Test several state transitions
    client._stopped.clear()
    client.start_async = lambda: True
    client._session_callback(KeeperState.CONNECTED)
    assert client.state == KazooState.CONNECTED

    client._session_callback(KeeperState.AUTH_FAILED)
    assert client.state == KazooState.LOST

    client._handle = 1
    client._session_callback(-250)
    assert client.state == KazooState.SUSPENDED

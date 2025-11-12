from unittest import mock

import pytest

try:
    # Use eventlet socket if available, for testing eventlet-specific behavior
    from kazoo.handlers.eventlet import green_socket as socket

    EVENTLET_HANDLER_AVAILABLE = True
except ImportError:
    import socket

    EVENTLET_HANDLER_AVAILABLE = False


def test_timeout_arg():
    """Test that create_tcp_connection handles timeouts correctly."""
    from kazoo.handlers import utils

    with mock.patch.object(socket, "create_connection") as create_connection:
        with mock.patch.object(utils, "_set_default_tcpsock_options"):
            # Ensure a gap between calls to time.time() does not result in
            # create_connection being called with a negative timeout
            # argument.
            with mock.patch.object(utils.time, "time", side_effect=range(10)):
                utils.create_tcp_connection(
                    socket,
                    ("127.0.0.1", 2181),
                    timeout=1.5,
                )

            for call_args in create_connection.call_args_list:
                timeout = call_args[0][1]
                assert timeout >= 0, "socket timeout must be nonnegative"


def test_ssl_server_hostname():
    """Test that SSL server hostname is passed correctly."""
    from kazoo.handlers import utils

    with mock.patch.object(utils, "_set_default_tcpsock_options"):
        with mock.patch.object(
            utils.ssl.SSLContext,
            "wrap_socket",
            autospec=True,
        ) as wrap_socket:
            utils.create_tcp_connection(
                socket,
                ("127.0.0.1", 2181),
                timeout=1.5,
                hostname="fakehostname",
                use_ssl=True,
            )

            for call_args in wrap_socket.call_args_list:
                server_hostname = call_args[1]["server_hostname"]
                assert server_hostname == "fakehostname"


def test_ssl_server_check_hostname():
    """Test SSL hostname checking is enabled correctly."""
    from kazoo.handlers import utils

    with mock.patch.object(utils, "_set_default_tcpsock_options"):
        with mock.patch.object(
            utils.ssl.SSLContext,
            "wrap_socket",
            autospec=True,
        ) as wrap_socket:
            utils.create_tcp_connection(
                socket,
                ("127.0.0.1", 2181),
                timeout=1.5,
                hostname="fakehostname",
                use_ssl=True,
                check_hostname=True,
            )

            for call_args in wrap_socket.call_args_list:
                ssl_context = call_args[0][0]
                assert ssl_context.check_hostname


def test_ssl_server_check_hostname_config_validation():
    """Test that enabling check_hostname requires verify_certs."""
    from kazoo.handlers import utils

    with pytest.raises(ValueError):
        utils.create_tcp_connection(
            socket,
            ("127.0.0.1", 2181),
            timeout=1.5,
            hostname="fakehostname",
            use_ssl=True,
            verify_certs=False,
            check_hostname=True,
        )


def test_timeout_arg_eventlet():
    """Test timeout argument handling with eventlet."""
    if not EVENTLET_HANDLER_AVAILABLE:
        pytest.skip("eventlet handler not available.")

    from kazoo.handlers import utils

    with mock.patch.object(socket, "create_connection") as create_connection:
        with mock.patch.object(utils, "_set_default_tcpsock_options"):
            # Ensure a gap between calls to time.time() does not result in
            # create_connection being called with a negative timeout
            # argument.
            with mock.patch.object(utils.time, "time", side_effect=range(10)):
                utils.create_tcp_connection(
                    socket,
                    ("127.0.0.1", 2181),
                    timeout=1.5,
                )

            for call_args in create_connection.call_args_list:
                timeout = call_args[0][1]
                assert timeout >= 0, "socket timeout must be nonnegative"


def test_slow_connect():
    """Test that a slow connection results in a timeout."""
    # create_tcp_connection should raise a socket timeout if it
    # takes longer than the specified "timeout" to create a connection.
    from kazoo.handlers import utils

    # Simulate a second passing between calls to check the current time.
    with mock.patch.object(utils.time, "time", side_effect=range(10)):
        with pytest.raises(socket.error):
            utils.create_tcp_connection(
                socket,
                ("127.0.0.1", 2181),
                timeout=0.5,
            )


def test_negative_timeout():
    """Test that a negative timeout raises an error."""
    from kazoo.handlers.utils import create_tcp_connection, socket

    with pytest.raises(socket.error):
        create_tcp_connection(socket, ("127.0.0.1", 2181), timeout=-1)


def test_zero_timeout():
    """Test that a zero timeout raises an error."""
    # Rather than pass '0' through as a timeout to
    # socket.create_connection, create_tcp_connection should raise
    # socket.error. This is because the socket library treats '0' as an
    # indicator to create a non-blocking socket.
    from kazoo.handlers import utils

    # Simulate no time passing between calls to check the current time.
    with mock.patch.object(utils.time, "time", return_value=0):
        with pytest.raises(socket.error):
            utils.create_tcp_connection(
                socket,
                ("127.0.0.1", 2181),
                timeout=0,
            )

"""Kazoo handler helpers"""

from __future__ import annotations

import functools
import socket
import ssl
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar, cast

from typing_extensions import ParamSpec

from kazoo.interfaces import IAsyncResult
from kazoo.protocol.tls import TLSConfig, build_ssl_context

if TYPE_CHECKING:
    from types import ModuleType

    from kazoo.interfaces import Socket


# sentinel objects
# Note: This needs to be a unique object that is not None, as None is used to
# indicate a successful result in AsyncResult.
# This should probably be an Enum, it would certainly be cleaner, but don't
# want to change the code too much.
_NONE = object()

CallbackFunc = Callable[..., None]


class AsyncResult(IAsyncResult):
    """A one-time event that stores a value or an exception"""

    def __init__(
        self,
        handler: Any,
        condition_factory: Callable[[], Any],
        timeout_factory: Callable[[], Any],
    ) -> None:
        self._handler = handler
        self._exception: object | Exception | None = _NONE
        self._condition = condition_factory()
        self._callbacks: list[CallbackFunc] = []
        self._timeout_factory = timeout_factory
        self.value: Any = None

    def ready(self) -> bool:
        """Return true if and only if it holds a value or an
        exception"""
        return self._exception is not _NONE

    def successful(self) -> bool:
        """Return true if and only if it is ready and holds a value"""
        return self._exception is None

    @property
    def exception(self) -> Exception | None:
        if self._exception is not _NONE:
            return self._exception  # type: ignore[return-value]
        return None

    def set(self, value: Any = None) -> None:
        """Store the value. Wake up the waiters."""
        with self._condition:
            self.value = value
            self._exception = None
            self._do_callbacks()
            self._condition.notify_all()

    def set_exception(self, exception: Exception) -> None:
        """Store the exception. Wake up the waiters."""
        with self._condition:
            self._exception = exception
            self._do_callbacks()
            self._condition.notify_all()

    def get(self, block: bool = True, timeout: float | None = None) -> Any:
        """Return the stored value or raise the exception.

        If there is no value raises TimeoutError.

        """
        with self._condition:
            if self._exception is not _NONE:
                if self._exception is None:
                    return self.value
                raise self._exception  # type: ignore[misc]
            elif block:
                self._condition.wait(timeout)
                if self._exception is not _NONE:
                    if self._exception is None:
                        return self.value
                    raise self._exception  # type: ignore[misc]

            # if we get to this point we timeout
            raise self._timeout_factory()

    def get_nowait(self) -> Any:
        """Return the value or raise the exception without blocking.

        If nothing is available, raises TimeoutError

        """
        return self.get(block=False)

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the instance is ready."""
        with self._condition:
            if not self.ready():
                self._condition.wait(timeout)
        return self._exception is not _NONE

    def rawlink(self, callback: CallbackFunc) -> None:
        """Register a callback to call when a value or an exception is
        set"""
        with self._condition:
            if callback not in self._callbacks:
                self._callbacks.append(callback)

            # Are we already set? Dispatch it now
            if self.ready():
                self._do_callbacks()

    def unlink(self, callback: CallbackFunc) -> None:
        """Remove the callback set by :meth:`rawlink`"""
        with self._condition:
            if self.ready():
                # Already triggered, ignore
                return

            if callback in self._callbacks:
                self._callbacks.remove(callback)

    def _do_callbacks(self) -> None:
        """Execute the callbacks that were registered by :meth:`rawlink`.
        If the handler is in running state this method only schedules
        the calls to be performed by the handler. If it's stopped,
        the callbacks are called right away."""

        for callback in self._callbacks:
            if self._handler.running:
                self._handler.completion_queue.put(
                    functools.partial(callback, self)
                )
            else:
                functools.partial(callback, self)()


def _set_default_tcpsock_options(module: ModuleType, sock: Socket) -> Socket:
    sock.setsockopt(module.IPPROTO_TCP, module.TCP_NODELAY, 1)
    return sock


def create_socket_pair(module: ModuleType) -> tuple[Socket, Socket]:
    """Create a connected pair of sockets using ``module.socketpair()``."""
    return cast("tuple[Socket, Socket]", module.socketpair())


def create_tcp_socket(module: ModuleType) -> Socket:
    """Create a TCP socket with default Kazoo options."""
    sock: Socket = module.socket(module.AF_INET, module.SOCK_STREAM)
    _set_default_tcpsock_options(module, sock)
    return sock


def create_tcp_connection(
    module: ModuleType,
    address: tuple[str, str | int],
    hostname: str | None = None,
    timeout: float | None = None,
    use_ssl: bool = False,
    ca: str | None = None,
    certfile: str | None = None,
    keyfile: str | None = None,
    keyfile_password: str | None = None,
    verify_certs: bool = True,
    check_hostname: bool = False,
    options: ssl.Options | None = None,
    ciphers: str | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> Socket:
    end = None
    if timeout is None:
        # thanks to create_connection() developers for
        # this ugliness...
        timeout = module.getdefaulttimeout()
    if timeout is not None:
        end = time.monotonic() + timeout
    sock: Socket | None = None
    if use_ssl and ssl_context is None:
        ssl_context = build_ssl_context(
            TLSConfig(
                use_ssl=True,
                ca=ca,
                certfile=certfile,
                keyfile=keyfile,
                keyfile_password=keyfile_password,
                verify_certs=verify_certs,
                check_hostname=check_hostname,
                options=options,
                ciphers=ciphers,
            )
        )
    elif not use_ssl:
        ssl_context = None

    while True:
        timeout_at = end if end is None else end - time.monotonic()
        # The condition is not '< 0' here because socket.settimeout treats 0 as
        # a special case to put the socket in non-blocking mode.
        if timeout_at is not None and timeout_at <= 0:
            break

        if ssl_context is not None:
            try:
                # Query the address to get back its address family
                addrs = socket.getaddrinfo(
                    address[0], address[1], 0, socket.SOCK_STREAM
                )
                conn = ssl_context.wrap_socket(
                    module.socket(addrs[0][0]), server_hostname=hostname
                )
                conn.settimeout(timeout_at)
                conn.connect(address)
                sock = conn
                break
            except ssl.SSLError:
                raise
        else:
            try:
                # if we got a timeout, lets ensure that we decrement the time
                # otherwise there is no timeout set and we'll call it as such
                sock = module.create_connection(address, timeout_at)
                break
            except InterruptedError:
                # Retry on an interrupted connect attempt. This is a
                # standard Python 3 socket behavior and is enough for the
                # supported Python versions in this project.
                continue

    if sock is None:
        raise module.error

    _set_default_tcpsock_options(module, sock)
    return sock


CapturedResult = TypeVar("CapturedResult")
GenericArgs = ParamSpec("GenericArgs")


def capture_exceptions(
    async_result: IAsyncResult,
) -> Callable[
    [Callable[GenericArgs, CapturedResult]],
    Callable[GenericArgs, CapturedResult | None],
]:
    """Return a new decorated function that propagates the exceptions of the
    wrapped function to an async_result.

    :param async_result: An async result implementing :class:`IAsyncResult`

    """

    def capture(
        function: Callable[GenericArgs, CapturedResult],
    ) -> Callable[GenericArgs, CapturedResult | None]:
        @functools.wraps(function)
        def captured_function(
            *args: GenericArgs.args, **kwargs: GenericArgs.kwargs
        ) -> CapturedResult | None:
            try:
                return function(*args, **kwargs)
            except Exception as exc:
                async_result.set_exception(exc)
                return None

        return captured_function

    return capture


def wrap(
    async_result: IAsyncResult,
) -> Callable[
    [Callable[GenericArgs, CapturedResult]],
    Callable[GenericArgs, CapturedResult | None],
]:
    """Return a new decorated function that propagates the return value or
    exception of wrapped function to an async_result.  NOTE: Only propagates a
    non-None return value.

    :param async_result: An async result implementing :class:`IAsyncResult`

    """

    def capture(
        function: Callable[GenericArgs, CapturedResult],
    ) -> Callable[GenericArgs, CapturedResult | None]:
        @capture_exceptions(async_result)
        def captured_function(
            *args: GenericArgs.args, **kwargs: GenericArgs.kwargs
        ) -> CapturedResult | None:
            value = function(*args, **kwargs)
            if value is not None:
                async_result.set(value)
            return value

        return captured_function

    return capture

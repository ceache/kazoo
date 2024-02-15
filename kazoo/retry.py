from __future__ import annotations

import copy
import dataclasses
import functools
import logging
import random
import sys
import time

from typing import TYPE_CHECKING

from kazoo.exceptions import (
    ConnectionClosedError,
    ConnectionLoss,
    KazooException,
    OperationTimeoutError,
    SessionExpiredError,
    ZookeeperError,
)

if TYPE_CHECKING:
    from typing import (
        Any,
        ClassVar,
        Optional,
        Callable,
        Tuple,
        Type,
        Protocol,
    )
    from typing_extensions import (
        TypeAlias,
    )

    RetriableExceptionType: TypeAlias = (
        "Tuple[Type[ZookeeperError]|Type[ForceRetryError], ...]"
    )

    from kazoo.handlers.utils import AsyncResult

    class KazooHandler(Protocol):
        sleep_func: Callable[[float], None]

        def async_result(self) -> AsyncResult: ...
        def spawn(self, func: Callable, *args: Any, **kwargs: Any) -> None: ...


_LOGGER = logging.getLogger(__name__)


class ForceRetryError(Exception):
    """Raised when some recipe logic wants to force a retry."""


class RetryFailedError(KazooException):
    """Raised when retrying an operation ultimately failed, after
    retrying the maximum number of attempts.
    """


class InterruptedError(RetryFailedError):
    """Raised when the retry is forcibly interrupted by the interrupt
    function"""


_RETRY_EXCEPTIONS = (
    ConnectionLoss,
    OperationTimeoutError,
    ForceRetryError,
)

_EXPIRED_EXCEPTIONS = (SessionExpiredError,)

_MISSING = object()


@dataclasses.dataclass(frozen=True)
class KazooRetry:
    """Helper for retrying a method in the face of retry-able exceptions.

    Create a :class:`KazooRetry` instance for retrying function calls.

    :param max_tries:
        How many times to retry the command. -1 means infinite tries.
    :param delay:
        Initial delay between retry attempts.
    :param backoff:
        Backoff multiplier between retry attempts. Defaults to 2 for
        exponential backoff.
    :param max_jitter:
        Percentage of jitter to apply to each retry's delay to ensure all
        clients to do not hammer the server at the same time. Between 0.0 and
        1.0.
    :param max_delay:
        Maximum delay in seconds, regardless of other backoff settings.
        Defaults to one minute.
    :param ignore_expire:
        Whether a session expiration should be ignored and treated as a
        retry-able command.
    :param interrupt:
        Function that will be called with no args that may return True if the
        retry should be ceased immediately. This will be called no more than
        every 0.1 seconds during a wait between retries.
    """

    handler: Any
    max_tries: int = 1
    delay: float = 0.1
    backoff: float = 2.0
    max_jitter: float = 0.4
    max_delay: float = 60.0
    deadline: Optional[float] = None
    interrupt: Optional[Callable] = None

    retry_exceptions: RetriableExceptionType = dataclasses.field(
        init=False, repr=False
    )

    ignore_expire: dataclasses.InitVar[bool] = True
    sleep_func: dataclasses.InitVar[Any] = _MISSING

    def __post_init__(self, ignore_expire: bool, sleep_func: Any) -> None:
        # Ensure max_jitter is in (0, 1)
        object.__setattr__(
            self, "max_jitter", max(min(self.max_jitter, 1.0), 0.0)
        )
        # Set the list of retry-able exceptions
        retry_exceptions = tuple(_RETRY_EXCEPTIONS)
        if ignore_expire:
            retry_exceptions += _EXPIRED_EXCEPTIONS
        object.__setattr__(self, "retry_exceptions", retry_exceptions)
        #
        assert self.sleep_func is _MISSING, "Deprecated"
        if self.sleep_func is not _MISSING:
            warnings.warn(
                "Passing a sleep_func to KazooRetry directly is deprecated",
                DeprecationWarning,
                stacklevel=2,
            )
            if self.sleep_func != self.handler.sleep_func:
                raise ConfigurationError(
                    "KazooRetry and handler" " must use the same sleep func"
                )

    def copy(self, /, **kwargs) -> KazooRetry:
        """Return a clone of this retry manager"""
        return dataclasses.replace(self, **kwargs)

    @dataclasses.dataclass
    class _RetryState:
        func: Any
        args: Any
        kwargs: Any
        # res: AsyncResult
        delay: float
        attempts: int = 0
        stoptime: Optional[float] = None

    def async_call(self, async_func, *args, **kwargs) -> AsyncResult:
        async_res = self.handler.async_result()

        state = KazooRetry._RetryState(
            func=async_func,
            args=args,
            kwargs=kwargs,
            # res=ares,
            delay=self.delay,
            stoptime=(
                None
                if self.deadline is None
                else time.monotonic() + self.deadline
            ),
        )

        self._async_try(state=state, async_res=async_res)
        return ares

    def _async_try(self, state: RetryState, async_res: AsyncResult) -> None:
        if state.attempts:
            # Have we reached our final attempt?
            # Note: max_tries == -1 means infinite tries.
            if state.attempts == self.max_tries:
                async_res.set_exception(
                    RetryFailedError("Too many retry attempts")
                )
                return

            # We are about to retry

            # Calculate sleep time between retries
            jitter = random.uniform(
                1.0 - self.max_jitter, 1.0 + self.max_jitter
            )
            sleeptime = state.delay * jitter

            # Abort if we are going to sleep past our stoptime
            if (
                state.stoptime is not None
                and time.monotonic() + sleeptime >= state.stoptime
            ):
                async_res.set_exception(
                    RetryFailedError("Exceeded retry deadline")
                )
                return

            if self.interrupt:
                remain_time = sleeptime
                while remain_time > 0:
                    # Break the time period down and sleep for no
                    # longer than 0.1 before calling the interrupt
                    self.handler.sleep_func(min(0.1, remain_time))
                    remain_time -= 0.1
                    if self.interrupt():
                        async_res.set_exception(InterruptedError())
                        return
            else:
                self.handler.sleep_func(sleeptime)

            # Update the delay
            state.delay = min(sleeptime * self.backoff, self.max_delay)

        state.attempts += 1
        step_ares = state.func(*state.args, **state.kwargs)
        # Chain evaluation of the result
        step_ares.rawlink(
            functools.partial(
                self._async_eval,
                state=state,
                async_res=async_res,
            )
        )

    def _async_eval(
        self,
        step_ares: AsyncResult,
        *,
        state: RetryState,
        async_res: AsyncResult,
    ):
        try:
            # Was this step successful?
            res = step_ares.get()
            async_res.set(res)
        except ConnectionClosedError as err:
            async_res.set_exception(err)
        except self.retry_exceptions:
            self.handler.spawn(
                functools.partial(
                    self._async_try,
                    state=state,
                    async_res=async_res,
                )
            )
        except ZookeeperError as err:
            async_res.set_exception(err)
        except Exception as err:
            _LOGGER.critical("Zookeeper error %s in retry", err)
            async_res.set_exception(err)

    def __call__(self, func, *args, **kwargs):
        """Call a function with arguments until it completes without
        throwing a Kazoo exception

        :param func:
            Function to call
        :param args:
            Positional arguments to call the function with
        :params kwargs:
            Keyword arguments to call the function with

        The function will be called until it doesn't throw one of the
        retryable exceptions (ConnectionLoss, OperationTimeout, or
        ForceRetryError), and optionally retrying on session
        expiration.
        """
        state = KazooRetry._RetryState(
            func=func,
            args=args,
            kwargs=kwargs,
            delay=self.delay,
            stoptime=(
                None
                if self.deadline is None
                else time.monotonic() + self.deadline
            ),
        )

        while True:
            try:
                return func(*args, **kwargs)
            except ConnectionClosedError:
                raise
            except self.retry_exceptions:
                # Note: max_tries == -1 means infinite tries.
                if state.attempts == self.max_tries:
                    raise RetryFailedError("Too many retry attempts")
                state.attempts += 1
                jitter = random.uniform(
                    1.0 - self.max_jitter, 1.0 + self.max_jitter
                )
                sleeptime = state.delay * jitter

                if (
                    state.stoptime is not None
                    and time.monotonic() + sleeptime >= state.stoptime
                ):
                    raise RetryFailedError("Exceeded retry deadline")

                if self.interrupt:
                    remain_time = sleeptime
                    while remain_time > 0:
                        # Break the time period down and sleep for no
                        # longer than 0.1 before calling the interrupt
                        self.handler.sleep_func(min(0.1, remain_time))
                        remain_time -= 0.1
                        if self.interrupt():
                            raise InterruptedError()
                else:
                    self.handler.sleep_func(sleeptime)
                state.delay = min(sleeptime * self.backoff, self.max_delay)

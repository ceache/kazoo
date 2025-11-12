import threading
from unittest.mock import Mock

import pytest

from kazoo.handlers import threading as threading_handler
from kazoo.handlers import utils


def test_proper_threading():
    """Test that the handler uses the correct threading event class."""
    h = threading_handler.SequentialThreadingHandler()
    h.start()
    # In Python 3.3 _Event is gone, before Event is a function
    event_class = getattr(threading, "_Event", threading.Event)
    assert isinstance(h.event_object(), event_class)
    h.stop()


def test_matching_async():
    """Test that the handler's async_result method returns an AsyncResult."""
    h = threading_handler.SequentialThreadingHandler()
    h.start()
    assert isinstance(h.async_result(), threading_handler.AsyncResult)
    h.stop()


def test_exception_raising():
    """Test that timeout exceptions can be raised correctly."""
    h = threading_handler.SequentialThreadingHandler()
    with pytest.raises(h.timeout_exception):
        raise h.timeout_exception("This is a timeout")


def test_double_start_stop():
    """Test that starting/stopping the handler multiple times is safe."""
    h = threading_handler.SequentialThreadingHandler()
    h.start()
    assert h._running is True
    h.start()  # should be a no-op
    h.stop()
    assert not h._running
    h.stop()  # should be a no-op


def test_huge_file_descriptor():
    """Test that the handler can select() on a large number of fds."""
    try:
        import resource
    except ImportError:
        pytest.skip("resource module unavailable on this platform")
    import socket

    try:
        # Increase the max number of open file descriptors
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (4096, hard))
    except (ValueError, resource.error):
        pytest.skip("couldn't raise fd limit high enough")

    socks = []
    try:
        # Create a large number of sockets
        while len(socks) < 4000:
            sock = utils.create_tcp_socket(socket)
            if sock.fileno() >= 4000:
                socks.append(sock)
                break
            socks.append(sock)

        h = threading_handler.SequentialThreadingHandler()
        h.start()
        # This should not raise an error
        h.select(socks, [], [], 0)
        h.stop()
    finally:
        # Clean up all the created sockets
        for sock in socks:
            sock.close()


@pytest.fixture
def mock_handler():
    """Fixture to provide a mocked handler with a completion_queue."""
    handler = Mock()
    handler.completion_queue = Mock()
    return handler


@pytest.fixture
def async_result(mock_handler):
    """Fixture to provide an AsyncResult instance."""
    return threading_handler.AsyncResult(mock_handler)


class TestAsyncResult:
    def test_ready(self, async_result):
        """Test the ready() and successful() states."""
        assert not async_result.ready()
        async_result.set("val")
        assert async_result.ready()
        assert async_result.successful()
        assert async_result.exception is None

    def test_callback_queued(self, async_result, mock_handler):
        """Test that a callback is queued when the result is set."""
        async_result.rawlink(lambda a: a)
        async_result.set("val")
        mock_handler.completion_queue.put.assert_called_once()

    def test_set_exception(self, async_result, mock_handler):
        """Test setting an exception on the result."""
        async_result.rawlink(lambda a: a)
        async_result.set_exception(ImportError("Error occurred"))
        assert isinstance(async_result.exception, ImportError)
        mock_handler.completion_queue.put.assert_called_once()

    def test_get_wait_while_setting(self, mock_handler):
        """Test that get() blocks until a value is set from another thread."""
        async_result = threading_handler.AsyncResult(mock_handler)
        result_list = []
        before_get = threading.Event()
        after_set = threading.Event()

        def wait_for_val():
            before_get.set()
            val = async_result.get()
            result_list.append(val)
            after_set.set()

        thread = threading.Thread(target=wait_for_val)
        thread.start()
        before_get.wait(timeout=5)  # Wait for thread to be ready

        async_result.set("fred")
        after_set.wait(timeout=5)  # Wait for thread to complete
        assert result_list == ["fred"]
        thread.join()

    def test_get_with_nowait(self):
        """Test non-blocking get() raises a timeout."""
        handler = threading_handler.SequentialThreadingHandler()
        async_result = threading_handler.AsyncResult(handler)
        timeout_exc = handler.timeout_exception

        with pytest.raises(timeout_exc):
            async_result.get(block=False)

        with pytest.raises(timeout_exc):
            async_result.get_nowait()

    def test_get_with_exception(self, mock_handler):
        """Test that get() raises an exception if one was set."""
        async_result = threading_handler.AsyncResult(mock_handler)
        result_list = []
        before_get = threading.Event()
        after_set = threading.Event()

        def wait_for_val():
            before_get.set()
            try:
                val = async_result.get()
                result_list.append(val)
            except ImportError:
                result_list.append("oops")
            finally:
                after_set.set()

        thread = threading.Thread(target=wait_for_val)
        thread.start()
        before_get.wait(timeout=5)

        async_result.set_exception(ImportError)
        after_set.wait(timeout=5)
        assert result_list == ["oops"]
        thread.join()

    def test_wait(self, mock_handler):
        """Test that wait() blocks until the result is ready."""
        async_result = threading_handler.AsyncResult(mock_handler)
        result_list = []
        before_wait = threading.Event()
        after_wait = threading.Event()

        def wait_for_val():
            before_wait.set()
            is_ready = async_result.wait(timeout=10)
            result_list.append(is_ready)
            after_wait.set()

        thread = threading.Thread(target=wait_for_val)
        thread.start()
        before_wait.wait(timeout=5)

        async_result.set("fred")
        after_wait.wait(timeout=15)
        assert result_list == [True]
        thread.join()

    def test_wait_race(self, mock_handler):
        """Test against race condition in IAsyncResult.wait()."""
        # Guards against the reappearance of:
        # https://github.com/python-zk/kazoo/issues/485
        async_result = threading_handler.AsyncResult(mock_handler)
        async_result.set("immediate")
        wait_finished = threading.Event()

        def wait_for_val():
            # This should not block at all
            async_result.wait(timeout=20)
            wait_finished.set()

        thread = threading.Thread(target=wait_for_val)
        thread.daemon = True
        thread.start()

        # If wait() didn't sleep, this will pass quickly.
        # If it slept, this will time out.
        wait_finished.wait(timeout=10)
        assert wait_finished.is_set()
        thread.join()

    def test_set_before_wait(self, mock_handler):
        """Test getting a value that was set before get() was called."""
        async_result = threading_handler.AsyncResult(mock_handler)
        result_list = []
        after_get = threading.Event()
        async_result.set("fred")

        def wait_for_val():
            val = async_result.get()
            result_list.append(val)
            after_get.set()

        thread = threading.Thread(target=wait_for_val)
        thread.start()
        after_get.wait(timeout=5)
        assert result_list == ["fred"]
        thread.join()

    def test_set_exc_before_wait(self, mock_handler):
        """Test getting an exception that was set before get() was called."""
        async_result = threading_handler.AsyncResult(mock_handler)
        result_list = []
        after_get = threading.Event()
        async_result.set_exception(ImportError)

        def wait_for_val():
            try:
                val = async_result.get()
                result_list.append(val)
            except ImportError:
                result_list.append("oops")
            finally:
                after_get.set()

        thread = threading.Thread(target=wait_for_val)
        thread.start()
        after_get.wait(timeout=5)
        assert result_list == ["oops"]
        thread.join()

    def test_linkage(self, async_result, mock_handler):
        """Test linking and unlinking callbacks."""
        after_get = threading.Event()

        def add_on(res):
            pass

        def wait_for_val():
            async_result.get()
            after_get.set()

        thread = threading.Thread(target=wait_for_val)
        thread.start()

        async_result.rawlink(add_on)
        async_result.set(b"fred")
        mock_handler.completion_queue.put.assert_called_once()

        async_result.unlink(add_on)
        after_get.wait(timeout=5)
        assert async_result.value == b"fred"
        thread.join()

    def test_linkage_not_ready(self, async_result, mock_handler):
        """Test linking a callback after the result is already set."""

        def add_on(res):
            pass

        async_result.set("fred")
        mock_handler.completion_queue.put.assert_not_called()
        async_result.rawlink(add_on)
        mock_handler.completion_queue.put.assert_called_once()

    def test_link_and_unlink(self, async_result, mock_handler):
        """Test that unlinking a callback prevents it from being called."""

        def add_on(res):
            pass

        async_result.rawlink(add_on)
        mock_handler.completion_queue.put.assert_not_called()
        async_result.unlink(add_on)
        async_result.set("fred")
        mock_handler.completion_queue.put.assert_not_called()

    def test_captured_exception(self, async_result):
        """Test the capture_exceptions decorator."""

        @utils.capture_exceptions(async_result)
        def exceptional_function():
            return 1 / 0

        exceptional_function()

        with pytest.raises(ZeroDivisionError):
            async_result.get()

    def test_no_capture_exceptions(self, async_result, mock_handler):
        """Test capture_exceptions with a non-exceptional function."""

        def add_on(res):
            pass

        async_result.rawlink(add_on)

        @utils.capture_exceptions(async_result)
        def regular_function():
            return True

        regular_function()

        mock_handler.completion_queue.put.assert_not_called()
        assert async_result.exception is None
        assert not async_result.ready()

    def test_wraps(self, async_result, mock_handler):
        """Test the wrap decorator."""

        def add_on(result):
            pass

        async_result.rawlink(add_on)

        @utils.wrap(async_result)
        def regular_function():
            return "hello"

        assert regular_function() == "hello"
        mock_handler.completion_queue.put.assert_called_once()
        assert async_result.get() == "hello"

    def test_multiple_callbacks(self):
        """Test that multiple callbacks are all called."""
        mockback1 = Mock(name="mockback1")
        mockback2 = Mock(name="mockback2")
        handler = threading_handler.SequentialThreadingHandler()
        handler.start()

        async_result = threading_handler.AsyncResult(handler)
        async_result.rawlink(mockback1)
        async_result.rawlink(mockback2)
        async_result.set("howdy")
        async_result.wait()
        handler.stop()

        mockback1.assert_called_once_with(async_result)
        mockback2.assert_called_once_with(async_result)

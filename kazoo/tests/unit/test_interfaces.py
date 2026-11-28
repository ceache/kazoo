from __future__ import annotations

import selectors
import time
from unittest.mock import patch

import pytest

from kazoo.handlers.threading import SequentialThreadingHandler
from kazoo.interfaces import IHandler


def test_ihandler_default_create_selector() -> None:
    class CustomHandler(IHandler):
        pass

    sel = CustomHandler().create_selector()
    try:
        assert isinstance(sel, selectors.BaseSelector)
    finally:
        sel.close()


def test_yield_control_threading_noop() -> None:
    class CustomHandler(IHandler):
        pass

    threading_handler = SequentialThreadingHandler()
    with patch.object(time, "sleep") as mock_sleep:
        CustomHandler().yield_control()
        threading_handler.yield_control()
        mock_sleep.assert_not_called()


def test_yield_control_gevent_switches_greenlets() -> None:
    gevent = pytest.importorskip("gevent")
    from kazoo.handlers.gevent import SequentialGeventHandler

    handler = SequentialGeventHandler()
    ran: list[str] = []

    def worker() -> None:
        ran.append("gevent")

    g = gevent.spawn(worker)
    try:
        handler.yield_control()
        assert ran == ["gevent"]
    finally:
        g.join(timeout=1.0)


def test_yield_control_eventlet_switches_greenlets() -> None:
    eventlet = pytest.importorskip("eventlet")
    from kazoo.handlers.eventlet import SequentialEventletHandler

    handler = SequentialEventletHandler()
    ran: list[str] = []

    def worker() -> None:
        ran.append("eventlet")

    g = eventlet.spawn(worker)
    try:
        handler.yield_control()
        assert ran == ["eventlet"]
    finally:
        g.wait()


def test_eventlet_selector_subclasses_base_selector() -> None:
    eventlet = pytest.importorskip("eventlet")
    from kazoo.handlers.eventlet import SequentialEventletHandler

    handler = SequentialEventletHandler()
    s_a, s_b = handler.create_socket_pair()
    try:
        s_a.setblocking(False)
        s_b.setblocking(False)
        s_b.send(b"x")
        with handler.create_selector() as sel:
            assert isinstance(sel, selectors.BaseSelector)
            sel.register(s_a, selectors.EVENT_READ, data="a")
            with patch.object(eventlet, "sleep") as mock_sleep:
                ready = sel.select(1.0)
                assert len(ready) == 1
                mock_sleep.assert_not_called()
    finally:
        s_a.close()
        s_b.close()


def test_handlers_expose_create_selector_and_no_legacy_select() -> None:
    from kazoo.handlers.threading import SequentialThreadingHandler

    handlers = [SequentialThreadingHandler()]
    try:
        from kazoo.handlers.gevent import SequentialGeventHandler

        handlers.append(SequentialGeventHandler())
    except ImportError:
        pass
    try:
        from kazoo.handlers.eventlet import SequentialEventletHandler

        handlers.append(SequentialEventletHandler())
    except ImportError:
        pass

    import os

    for handler in handlers:
        assert not hasattr(handler, "select")
        s_a, s_b = handler.create_socket_pair()
        try:
            assert os.get_inheritable(s_a.fileno()) is False
            assert os.get_inheritable(s_b.fileno()) is False
            s_a.setblocking(False)
            s_b.setblocking(False)
            with handler.create_selector() as sel:
                sel.register(s_a, selectors.EVENT_READ, data="a")
                assert sel.select(0.0) == []
                s_b.send(b"hi")
                ready = sel.select(1.0)
                assert len(ready) == 1
                assert ready[0][0].data == "a"
                assert s_a.recv(16) == b"hi"
                sel.modify(s_a, selectors.EVENT_WRITE, data="a")
                ready_w = sel.select(1.0)
                assert len(ready_w) == 1
                assert ready_w[0][1] & selectors.EVENT_WRITE
        finally:
            s_a.close()
            s_b.close()


def test_builtin_handlers_satisfy_ihandler_protocol() -> None:
    """All built-in handlers satisfy IHandler for KazooClient."""
    from kazoo.client import KazooClient

    h_thread: IHandler = SequentialThreadingHandler()
    KazooClient(handler=h_thread)

    try:
        from kazoo.handlers.gevent import SequentialGeventHandler
    except ImportError:
        pass
    else:
        h_gevent: IHandler = SequentialGeventHandler()
        KazooClient(handler=h_gevent)

    try:
        from kazoo.handlers.eventlet import SequentialEventletHandler
    except ImportError:
        pass
    else:
        h_eventlet: IHandler = SequentialEventletHandler()
        KazooClient(handler=h_eventlet)

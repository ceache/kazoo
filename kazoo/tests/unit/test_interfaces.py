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
    pytest.importorskip("eventlet")
    from kazoo.handlers.eventlet import SequentialEventletHandler

    handler = SequentialEventletHandler()
    with handler.create_selector() as sel:
        assert isinstance(sel, selectors.BaseSelector)

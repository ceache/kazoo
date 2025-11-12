import pytest

from kazoo import exceptions


def test_backwards_alias():
    """Verify that the old exception name alias exists."""
    assert hasattr(exceptions, "NoNodeException")
    assert exceptions.NoNodeException is exceptions.NoNodeError


def test_exceptions_code():
    """Verify that an exception can be retrieved by its code."""
    exc_cls = exceptions.EXCEPTIONS[-8]
    assert isinstance(exc_cls(), exceptions.BadArgumentsError)


def test_invalid_code():
    """Verify that retrieving an invalid code raises an error."""
    with pytest.raises(RuntimeError):
        exceptions.EXCEPTIONS.__getitem__(666)


def test_exceptions_construction():
    """Verify that an exception can be constructed correctly."""
    exc = exceptions.EXCEPTIONS[-101]()
    assert type(exc) is exceptions.NoNodeError
    assert exc.args == ()

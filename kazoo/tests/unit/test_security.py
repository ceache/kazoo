from __future__ import annotations

from typing import Any

from kazoo.security import ACL, Permissions


class TestACL:
    def _makeOne(self, *args: Any, **kwargs: Any) -> ACL:
        from kazoo.security import make_acl

        return make_acl(*args, **kwargs)

    def test_read_acl(self) -> None:
        acl = self._makeOne("digest", ":", read=True)
        assert Permissions.READ in acl.perms
        assert Permissions.WRITE not in acl.perms

    def test_all_perms(self) -> None:
        acl = self._makeOne(
            "digest",
            ":",
            read=True,
            write=True,
            create=True,
            delete=True,
            admin=True,
        )
        for perm in [
            Permissions.READ,
            Permissions.CREATE,
            Permissions.WRITE,
            Permissions.DELETE,
            Permissions.ADMIN,
        ]:
            assert perm in acl.perms
        assert Permissions.ALL in acl.perms

    def test_perm_membership(self) -> None:
        from kazoo.security import Id

        f = ACL(15, Id("fred", "bill"))
        assert Permissions.READ in f.perms
        assert Permissions.WRITE in f.perms
        assert Permissions.CREATE in f.perms
        assert Permissions.DELETE in f.perms
        assert Permissions.ADMIN not in f.perms

        f = ACL(16, Id("fred", "bill"))
        assert Permissions.ADMIN in f.perms
        assert Permissions.READ not in f.perms

        f = ACL(31, Id("fred", "bill"))
        assert Permissions.ALL in f.perms

    def test_acl_list_formatting(self) -> None:
        from kazoo.security import Id

        f15 = ACL(15, Id("fred", "bill"))
        assert f15._acl_list == ["READ", "WRITE", "CREATE", "DELETE"]

        f16 = ACL(16, Id("fred", "bill"))
        assert f16._acl_list == ["ADMIN"]

        f31 = ACL(31, Id("fred", "bill"))
        assert f31._acl_list == ["ALL"]

    def test_deprecated_acl_list(self) -> None:
        import pytest

        from kazoo.security import Id

        f = ACL(Permissions.READ, Id("fred", "bill"))
        with pytest.deprecated_call():
            assert f.acl_list == ["READ"]

    def test_perm_repr(self) -> None:
        # FIXME See above
        from kazoo.security import Id

        f = ACL(16, Id("fred", "bill"))
        assert "ACL(perms=16, acl_list=['ADMIN']" in repr(f)

    def test_acl_normalization(self) -> None:
        from kazoo.security import Id

        # int is normalized to Permissions IntFlag
        acl = ACL(15, Id("fred", "bill"))
        assert isinstance(acl.perms, Permissions)
        assert acl.perms == (
            Permissions.READ
            | Permissions.WRITE
            | Permissions.CREATE
            | Permissions.DELETE
        )

        # Permissions instance remains Permissions
        acl2 = ACL(Permissions.ADMIN, Id("fred", "bill"))
        assert isinstance(acl2.perms, Permissions)
        assert acl2.perms == Permissions.ADMIN

    def test_acl_unpacking(self) -> None:
        import pytest

        from kazoo.security import Id

        acl = ACL(Permissions.READ, Id("world", "anyone"))
        with pytest.deprecated_call():
            perms, ident = acl
        assert perms == Permissions.READ
        assert ident == Id("world", "anyone")

        with pytest.deprecated_call():
            scheme, id_str = ident
        assert scheme == "world"
        assert id_str == "anyone"

    def test_acl_indexing_and_len(self) -> None:
        import pytest

        from kazoo.security import Id

        ident = Id("world", "anyone")
        with pytest.deprecated_call():
            assert len(ident) == 2
        with pytest.deprecated_call():
            assert ident[0] == "world"
        with pytest.deprecated_call():
            assert ident[1] == "anyone"
        with pytest.deprecated_call():
            assert ident[-1] == "anyone"
        with pytest.deprecated_call():
            assert ident[-2] == "world"
        with pytest.deprecated_call():
            assert ident[:] == ("world", "anyone")
        with pytest.deprecated_call():
            assert ident[::-1] == ("anyone", "world")
        with pytest.deprecated_call(), pytest.raises(IndexError):
            _ = ident[2]

        acl = ACL(Permissions.READ, ident)
        with pytest.deprecated_call():
            assert len(acl) == 2
        with pytest.deprecated_call():
            assert acl[0] == Permissions.READ
        with pytest.deprecated_call():
            assert acl[1] == ident
        with pytest.deprecated_call():
            assert acl[-1] == ident
        with pytest.deprecated_call():
            assert acl[-2] == Permissions.READ
        with pytest.deprecated_call():
            assert acl[:] == (Permissions.READ, ident)
        with pytest.deprecated_call():
            assert acl[::-1] == (ident, Permissions.READ)
        with pytest.deprecated_call(), pytest.raises(IndexError):
            _ = acl[2]

    def test_bool_truthiness(self) -> None:
        import warnings

        from kazoo.security import Id

        ident = Id("world", "anyone")
        acl = ACL(Permissions.READ, ident)
        acl_none = ACL(Permissions.NONE, Id("", ""))

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            assert bool(ident) is True
            assert bool(acl) is True
            assert bool(acl_none) is True
            assert (True if ident else False) is True
            assert (True if acl else False) is True
            assert (True if acl_none else False) is True

    def test_ordering_and_negative_perms_rejected(self) -> None:
        import pytest

        from kazoo.security import Id

        id_a = Id("digest", "alice")
        id_b = Id("world", "anyone")
        assert sorted([id_b, id_a]) == [id_a, id_b]

        acl_low = ACL(Permissions.READ, id_b)
        acl_high = ACL(Permissions.WRITE, id_a)
        assert sorted([acl_high, acl_low]) == [acl_low, acl_high]

        with pytest.raises(ValueError, match="non-negative"):
            ACL(-1, id_b)

        # Positive unknown bits continue to round-trip via IntFlag.
        assert int(ACL(64, id_b).perms) == 64

    def test_invalid_state_transition_error_pickle_roundtrip(self) -> None:
        import pickle

        from kazoo.exceptions import InvalidStateTransitionError

        err = InvalidStateTransitionError("Connected", "UnexpectedEvent")
        restored = pickle.loads(pickle.dumps(err))
        assert isinstance(restored, InvalidStateTransitionError)
        assert restored.state == "Connected"
        assert restored.event == "UnexpectedEvent"
        assert str(restored) == str(err)

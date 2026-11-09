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
        from kazoo.security import Id

        acl = ACL(Permissions.READ, Id("world", "anyone"))
        perms, ident = acl
        assert perms == Permissions.READ
        assert ident == Id("world", "anyone")

        scheme, id_str = ident
        assert scheme == "world"
        assert id_str == "anyone"

    def test_acl_indexing_and_len(self) -> None:
        import pytest
        from kazoo.security import Id

        ident = Id("world", "anyone")
        assert len(ident) == 2
        assert ident[0] == "world"
        assert ident[1] == "anyone"
        assert ident[-1] == "anyone"
        assert ident[-2] == "world"
        with pytest.raises(IndexError):
            _ = ident[2]

        acl = ACL(Permissions.READ, ident)
        assert len(acl) == 2
        assert acl[0] == Permissions.READ
        assert acl[1] == ident
        assert acl[-1] == ident
        assert acl[-2] == Permissions.READ
        with pytest.raises(IndexError):
            _ = acl[2]

from kazoo import security


def test_read_acl():
    """Test creating an ACL with only read permissions."""
    acl = security.make_acl("digest", ":", read=True)
    assert acl.perms & security.Permissions.READ == security.Permissions.READ


def test_all_perms():
    """Test creating an ACL with all permissions."""
    acl = security.make_acl(
        "digest",
        ":",
        read=True,
        write=True,
        create=True,
        delete=True,
        admin=True,
    )
    for perm in [
        security.Permissions.READ,
        security.Permissions.CREATE,
        security.Permissions.WRITE,
        security.Permissions.DELETE,
        security.Permissions.ADMIN,
    ]:
        assert acl.perms & perm == perm


def test_perm_listing():
    """Test the string representation of ACL permissions."""
    f = security.ACL(15, "fred")
    assert "READ" in f.acl_list
    assert "WRITE" in f.acl_list
    assert "CREATE" in f.acl_list
    assert "DELETE" in f.acl_list

    f = security.ACL(16, "fred")
    assert "ADMIN" in f.acl_list

    f = security.ACL(31, "george")
    assert "ALL" in f.acl_list


def test_perm_repr():
    """Test the __repr__ of an ACL object."""
    f = security.ACL(16, "fred")
    assert "ACL(perms=16, acl_list=['ADMIN']" in repr(f)

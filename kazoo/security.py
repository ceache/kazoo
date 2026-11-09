"""Kazoo Security"""

from __future__ import annotations

import base64
from collections.abc import Iterator
import dataclasses
import enum
import hashlib
from typing import Any
from typing_extensions import deprecated


class Permissions(enum.IntFlag):
    NONE = 0
    READ = 1
    WRITE = 2
    CREATE = 4
    DELETE = 8
    ADMIN = 16
    ALL = 31


_ATOMIC_PERMISSIONS = (
    (Permissions.READ, "READ"),
    (Permissions.WRITE, "WRITE"),
    (Permissions.CREATE, "CREATE"),
    (Permissions.DELETE, "DELETE"),
    (Permissions.ADMIN, "ADMIN"),
)


@dataclasses.dataclass(frozen=True, slots=True)
class Id:
    """Represents a Zookeeper ID and ACL object"""

    scheme: str
    id: str

    @deprecated(
        "Tuple unpacking/iteration is deprecated; access attributes directly "
        "via .scheme and .id instead."
    )
    def __iter__(self) -> Iterator[str]:
        yield self.scheme
        yield self.id

    @deprecated(
        "Tuple indexing is deprecated; access attributes directly via "
        ".scheme and .id instead."
    )
    def __getitem__(self, index: int) -> str:
        if index in (0, -2):
            return self.scheme
        if index in (1, -1):
            return self.id
        raise IndexError("tuple index out of range")

    @deprecated(
        "len() on Id is deprecated; access attributes directly via "
        ".scheme and .id instead."
    )
    def __len__(self) -> int:
        return 2


@dataclasses.dataclass(frozen=True, slots=True)
class ACL:
    """An ACL for a Zookeeper Node

    An ACL object is created by using an :class:`Id` object along with
    a :class:`Permissions` setting. For convenience,
    :meth:`make_digest_acl` should be used to create an ACL object with
    the desired scheme, id, and permissions.
    """

    perms: Permissions | int
    id: Id

    def __post_init__(self) -> None:
        if not isinstance(self.perms, Permissions):
            object.__setattr__(self, "perms", Permissions(self.perms))

    @deprecated(
        "Tuple unpacking/iteration is deprecated; access attributes directly "
        "via .perms and .id instead."
    )
    def __iter__(self) -> Iterator[Any]:
        yield self.perms
        yield self.id

    @deprecated(
        "Tuple indexing is deprecated; access attributes directly via "
        ".perms and .id instead."
    )
    def __getitem__(self, index: int) -> Any:
        if index in (0, -2):
            return self.perms
        if index in (1, -1):
            return self.id
        raise IndexError("tuple index out of range")

    @deprecated(
        "len() on ACL is deprecated; access attributes directly via "
        ".perms and .id instead."
    )
    def __len__(self) -> int:
        return 2

    @property
    def _acl_list(self) -> list[str]:
        if self.perms & Permissions.ALL == Permissions.ALL:
            return ["ALL"]
        return [
            name for perm, name in _ATOMIC_PERMISSIONS if self.perms & perm
        ]

    @property
    @deprecated(
        "acl_list is deprecated; inspect .perms with Permissions flags "
        "directly instead."
    )
    def acl_list(self) -> list[str]:
        return self._acl_list

    def __repr__(self) -> str:
        return "ACL(perms=%d, acl_list=%s, id=%r)" % (
            self.perms,
            self._acl_list,
            self.id,
        )


# Shortcuts for common Ids
ANYONE_ID_UNSAFE = Id(scheme="world", id="anyone")
AUTH_IDS = Id(scheme="auth", id="")

# Shortcuts for common ACLs
OPEN_ACL_UNSAFE = [ACL(Permissions.ALL, ANYONE_ID_UNSAFE)]
CREATOR_ALL_ACL = [ACL(Permissions.ALL, AUTH_IDS)]
READ_ACL_UNSAFE = [ACL(Permissions.READ, ANYONE_ID_UNSAFE)]


def make_digest_acl_credential(username: str, password: str) -> str:
    """Create a SHA1 digest credential.

    .. note::

        This function uses UTF-8 to encode non-ASCII codepoints,
        whereas ZooKeeper uses the "default locale" for decoding.  It
        may be a good idea to start the JVM with `-Dfile.encoding=UTF-8`
        in non-UTF-8 locales.
        See: https://github.com/python-zk/kazoo/pull/584

    """
    credential = username.encode("utf-8") + b":" + password.encode("utf-8")
    cred_hash = base64.b64encode(hashlib.sha1(credential).digest()).strip()
    return username + ":" + cred_hash.decode("utf-8")


def make_acl(
    scheme: str,
    credential: str,
    read: bool = False,
    write: bool = False,
    create: bool = False,
    delete: bool = False,
    admin: bool = False,
    all: bool = False,
) -> ACL:
    """Given a scheme and credential, return an :class:`ACL` object
    appropriate for use with Kazoo.

    :param scheme: The scheme to use. I.e. `digest`.
    :param credential:
        A colon separated username, password. The password should be
        hashed with the `scheme` specified. The
        :meth:`make_digest_acl_credential` method will create and
        return a credential appropriate for use with the `digest`
        scheme.
    :param write: Write permission.
    :type write: bool
    :param create: Create permission.
    :type create: bool
    :param delete: Delete permission.
    :type delete: bool
    :param admin: Admin permission.
    :type admin: bool
    :param all: All permissions.
    :type all: bool

    :rtype: :class:`ACL`

    """
    if all:
        permissions = Permissions.ALL
    else:
        permissions = Permissions.NONE
        if read:
            permissions |= Permissions.READ
        if write:
            permissions |= Permissions.WRITE
        if create:
            permissions |= Permissions.CREATE
        if delete:
            permissions |= Permissions.DELETE
        if admin:
            permissions |= Permissions.ADMIN
    return ACL(permissions, Id(scheme, credential))


def make_digest_acl(
    username: str,
    password: str,
    read: bool = False,
    write: bool = False,
    create: bool = False,
    delete: bool = False,
    admin: bool = False,
    all: bool = False,
) -> ACL:
    """Create a digest ACL for Zookeeper with the given permissions

    This method combines :meth:`make_digest_acl_credential` and
    :meth:`make_acl` to create an :class:`ACL` object appropriate for
    use with Kazoo's ACL methods.

    :param username: Username to use for the ACL.
    :param password: A plain-text password to hash.
    :param write: Write permission.
    :type write: bool
    :param create: Create permission.
    :type create: bool
    :param delete: Delete permission.
    :type delete: bool
    :param admin: Admin permission.
    :type admin: bool
    :param all: All permissions.
    :type all: bool

    :rtype: :class:`ACL`

    """
    cred = make_digest_acl_credential(username, password)
    return make_acl(
        "digest",
        cred,
        read=read,
        write=write,
        create=create,
        delete=delete,
        admin=admin,
        all=all,
    )

"""Zookeeper Serializers, Deserializers, and Frame Codec"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING, ClassVar, TypeVar

from typing_extensions import dataclass_transform

from kazoo.exceptions import (
    ConnectionDropped,
    MarshallingError,
    ReplyTooLargeError,
    ZookeeperError,
    _error_for_code,
)
from kazoo.protocol.states import ZnodeStat
from kazoo.security import ACL, Id, Permissions

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import Any, TypeAlias

    from kazoo.client import WatchFunc
    from kazoo.interfaces import HasUnchroot, ICompletion

    _TransOpsTypes: TypeAlias = (
        "Create"
        " | Create2"
        " | CreateContainer"
        " | CreateTTL"
        " | Delete"
        " | SetData"
        " | CheckVersion"
    )
    _TransOpsResponse: TypeAlias = (
        str | bool | ZnodeStat | tuple[str, ZnodeStat] | ZookeeperError | None
    )

    ServerPacket: TypeAlias = (
        "Connect"
        " | PingResponse"
        " | AuthResponse"
        " | WatchPacket"
        " | SASLResponse"
        " | ResponsePacket"
    )

    PopPending: TypeAlias = Callable[
        [], tuple[Any, ICompletion[Any] | None, int]
    ]


_T = TypeVar("_T")


@dataclass_transform(frozen_default=True)
def _packet(cls: type[_T]) -> type[_T]:
    """Slotted dataclass that type checkers treat as frozen.

    Packets are immutable by contract. Immutability is enforced by mypy,
    not at runtime: frozen dataclasses set each field through
    object.__setattr__, which roughly triples construction cost.

    Note: ``frozen_default=True`` causes static type checkers to treat
    instances as ``Hashable``, even though runtime instances define
    ``__hash__ = None`` and are not hashable.
    """
    return dataclass(slots=True)(cls)


class ReservedXID(IntEnum):
    """Reserved ZooKeeper wire protocol XIDs."""

    CONNECT = 0
    WATCH = -1
    PING = -2
    AUTH = -4


# Struct objects with formats compiled
bool_struct = struct.Struct("B")
int_struct = struct.Struct("!i")
int_int_struct = struct.Struct("!ii")
int_int_long_struct = struct.Struct("!iiq")

int_long_int_long_struct = struct.Struct("!iqiq")
long_struct = struct.Struct("!q")
multiheader_struct = struct.Struct("!iBi")
reply_header_struct = struct.Struct("!iqi")
watch_header_struct = struct.Struct("!iqiii")
stat_struct = struct.Struct("!qqqqiiiqiiq")


def _read_string(buffer: bytes | memoryview, offset: int) -> tuple[str, int]:
    """Reads an int specified buffer into a string and returns the
    string and the new offset in the buffer"""
    length = int_struct.unpack_from(buffer, offset)[0]
    offset += int_struct.size
    if length < -1:
        raise MarshallingError(f"Invalid string length: {length}")
    if length == -1:
        # A note: write_str sends a length of -1 to indicate a value of None
        # was passed. Not entirely sure where this happens because none of the
        # callers of read_string seem to expect a None value.
        return None, offset  # type: ignore[return-value]
    else:
        index = offset
        offset += length
        return str(buffer[index : index + length], "utf-8"), offset


def _read_required_string(
    buffer: bytes | memoryview, offset: int
) -> tuple[str, int]:
    """Read a non-null length-prefixed UTF-8 string from ``buffer``."""
    length = int_struct.unpack_from(buffer, offset)[0]
    offset += int_struct.size
    if length < 0:
        raise MarshallingError(f"Invalid string length: {length}")
    index = offset
    offset += length
    return str(buffer[index : index + length], "utf-8"), offset


def _read_acl(bytes: bytes | memoryview, offset: int) -> tuple[ACL, int]:
    perms = int_struct.unpack_from(bytes, offset)[0]
    offset += int_struct.size
    scheme, offset = _read_required_string(bytes, offset)
    id, offset = _read_required_string(bytes, offset)
    return ACL(Permissions(perms), Id(scheme, id)), offset


def _write_acls(buf: bytearray, acls: Sequence[ACL]) -> None:
    """Append a length-prefixed ACL sequence to ``buf``."""
    buf.extend(int_struct.pack(len(acls)))
    for acl in acls:
        buf.extend(int_struct.pack(acl.perms))
        buf.extend(_write_string(acl.id.scheme))
        buf.extend(_write_string(acl.id.id))


def _write_string(bytes: str | None) -> bytes:
    # FIXME? This doesn't seem to behave sensibly if you pass in an empty
    # string. It will write a length of -1, which is the same as if you passed
    # in None. Shouldn't it write a length of 0 for an empty string? Or is
    # this just how the zookeeper protocol works?. This is different to
    # write_buffer, which writes a length of 0 for an empty buffer and -1 for
    # None.
    if not bytes:
        return int_struct.pack(-1)
    else:
        utf8_str = bytes.encode("utf-8")
        return int_struct.pack(len(utf8_str)) + utf8_str


def _write_buffer(bytes: bytes | None) -> bytes:
    if bytes is None:
        return int_struct.pack(-1)
    else:
        return int_struct.pack(len(bytes)) + bytes


def _read_buffer(
    buffer: bytes | memoryview, offset: int
) -> tuple[bytes | None, int]:
    length = int_struct.unpack_from(buffer, offset)[0]
    offset += int_struct.size
    if length < -1:
        raise MarshallingError(f"Invalid buffer length: {length}")
    if length == -1:
        return None, offset
    else:
        index = offset
        offset += length
        return bytes(buffer[index : index + length]), offset


@_packet
class Close:
    type: ClassVar[int] = -11

    @classmethod
    def serialize(cls) -> bytes:
        return b""

    @classmethod
    def deserialize(
        cls, bytes: bytes | memoryview, offset: int
    ) -> tuple[None, int]:
        return None, offset


@_packet
class Ping:
    type: ClassVar[int] = 11

    @classmethod
    def serialize(cls) -> bytes:
        return b""


@_packet
class Connect:
    protocol_version: int
    last_zxid_seen: int
    time_out: int
    session_id: int
    passwd: bytes
    read_only: bool

    type: ClassVar[int | None] = None

    def serialize(self) -> bytearray:
        b = bytearray()
        b.extend(
            int_long_int_long_struct.pack(
                self.protocol_version,
                self.last_zxid_seen,
                self.time_out,
                self.session_id,
            )
        )
        b.extend(_write_buffer(self.passwd))
        b.extend([1 if self.read_only else 0])
        return b

    @classmethod
    def deserialize(
        cls, bytes: bytes | memoryview, offset: int
    ) -> tuple[Connect, int]:
        proto_version, timeout, session_id = int_int_long_struct.unpack_from(
            bytes, offset
        )
        offset += int_int_long_struct.size
        password, offset = _read_buffer(bytes, offset)

        try:
            read_only = bool_struct.unpack_from(bytes, offset)[0] == 1
            offset += bool_struct.size
        except struct.error:
            read_only = False
        return (
            cls(
                proto_version,
                0,
                timeout,
                session_id,
                password or b"",
                read_only,
            ),
            offset,
        )


@_packet
class Create:
    path: str
    data: bytes | None
    acl: Sequence[ACL]
    flags: int

    type: ClassVar[int] = 1

    def serialize(self) -> bytearray:
        b = bytearray()
        b.extend(_write_string(self.path))
        b.extend(_write_buffer(self.data))
        _write_acls(b, self.acl)
        b.extend(int_struct.pack(self.flags))
        return b

    @classmethod
    def deserialize(
        cls, bytes: bytes | memoryview, offset: int
    ) -> tuple[str, int]:
        return _read_required_string(bytes, offset)


@_packet
class Delete:
    path: str
    version: int

    type: ClassVar[int] = 2

    def serialize(self) -> bytearray:
        b = bytearray()
        b.extend(_write_string(self.path))
        b.extend(int_struct.pack(self.version))
        return b

    @classmethod
    def deserialize(
        cls, bytes: bytes | memoryview, offset: int
    ) -> tuple[bool, int]:
        return True, offset


@_packet
class Exists:
    path: str
    watcher: WatchFunc | None

    type: ClassVar[int] = 3

    def serialize(self) -> bytearray:
        b = bytearray()
        b.extend(_write_string(self.path))
        b.extend([1 if self.watcher else 0])
        return b

    @classmethod
    def deserialize(
        cls, bytes: bytes | memoryview, offset: int
    ) -> tuple[ZnodeStat | None, int]:
        stat = ZnodeStat(*stat_struct.unpack_from(bytes, offset))
        offset += stat_struct.size
        return (stat if stat.czxid != -1 else None), offset


@_packet
class GetData:
    path: str
    watcher: WatchFunc | None

    type: ClassVar[int] = 4

    def serialize(self) -> bytearray:
        b = bytearray()
        b.extend(_write_string(self.path))
        b.extend([1 if self.watcher else 0])
        return b

    @classmethod
    def deserialize(
        cls, bytes: bytes | memoryview, offset: int
    ) -> tuple[tuple[bytes | None, ZnodeStat], int]:
        data, offset = _read_buffer(bytes, offset)
        stat = ZnodeStat(*stat_struct.unpack_from(bytes, offset))
        return (data, stat), offset + stat_struct.size


@_packet
class SetData:
    path: str
    data: bytes | None
    version: int

    type: ClassVar[int] = 5

    def serialize(self) -> bytearray:
        b = bytearray()
        b.extend(_write_string(self.path))
        b.extend(_write_buffer(self.data))
        b.extend(int_struct.pack(self.version))
        return b

    @classmethod
    def deserialize(
        cls, bytes: bytes | memoryview, offset: int
    ) -> tuple[ZnodeStat, int]:
        stat = ZnodeStat(*stat_struct.unpack_from(bytes, offset))
        return stat, offset + stat_struct.size


@_packet
class GetACL:
    path: str

    type: ClassVar[int] = 6

    def serialize(self) -> bytearray:
        return bytearray(_write_string(self.path))

    @classmethod
    def deserialize(
        cls, bytes: bytes | memoryview, offset: int
    ) -> tuple[tuple[list[ACL], ZnodeStat], int]:
        count = int_struct.unpack_from(bytes, offset)[0]
        offset += int_struct.size
        if count < -1:
            raise MarshallingError(f"Invalid ACL count: {count}")
        if count == -1:  # pragma: nocover
            # A null ACL list is still followed by the stat.
            stat = ZnodeStat(*stat_struct.unpack_from(bytes, offset))
            return ([], stat), offset + stat_struct.size

        acls = []
        for c in range(count):
            acl, offset = _read_acl(bytes, offset)
            acls.append(acl)
        stat = ZnodeStat(*stat_struct.unpack_from(bytes, offset))
        return (acls, stat), offset + stat_struct.size


@_packet
class SetACL:
    path: str
    acls: Sequence[ACL]
    version: int

    type: ClassVar[int] = 7

    def serialize(self) -> bytearray:
        b = bytearray()
        b.extend(_write_string(self.path))
        _write_acls(b, self.acls)
        b.extend(int_struct.pack(self.version))
        return b

    @classmethod
    def deserialize(
        cls, bytes: bytes | memoryview, offset: int
    ) -> tuple[ZnodeStat, int]:
        stat = ZnodeStat(*stat_struct.unpack_from(bytes, offset))
        return stat, offset + stat_struct.size


@_packet
class GetChildren:
    path: str
    watcher: WatchFunc | None

    type: ClassVar[int] = 8

    def serialize(self) -> bytearray:
        b = bytearray()
        b.extend(_write_string(self.path))
        b.extend([1 if self.watcher else 0])
        return b

    @classmethod
    def deserialize(
        cls, bytes: bytes | memoryview, offset: int
    ) -> tuple[list[str], int]:
        count = int_struct.unpack_from(bytes, offset)[0]
        offset += int_struct.size
        if count < -1:
            raise MarshallingError(f"Invalid children count: {count}")
        if count == -1:  # pragma: nocover
            return [], offset

        children = []
        for c in range(count):
            child, offset = _read_required_string(bytes, offset)
            children.append(child)
        return children, offset


@_packet
class Sync:
    path: str

    type: ClassVar[int] = 9

    def serialize(self) -> bytes:
        return _write_string(self.path)

    @classmethod
    def deserialize(
        cls, buffer: bytes | memoryview, offset: int
    ) -> tuple[str, int]:
        return _read_required_string(buffer, offset)


@_packet
class GetChildren2:
    path: str
    watcher: WatchFunc | None

    type: ClassVar[int] = 12

    def serialize(self) -> bytearray:
        b = bytearray()
        b.extend(_write_string(self.path))
        b.extend([1 if self.watcher else 0])
        return b

    @classmethod
    def deserialize(
        cls, bytes: bytes | memoryview, offset: int
    ) -> tuple[tuple[list[str], ZnodeStat], int]:
        count = int_struct.unpack_from(bytes, offset)[0]
        offset += int_struct.size
        if count < -1:
            raise MarshallingError(f"Invalid children count: {count}")
        if count == -1:  # pragma: nocover
            # A null children list is still followed by the stat.
            stat = ZnodeStat(*stat_struct.unpack_from(bytes, offset))
            return ([], stat), offset + stat_struct.size

        children: list[str] = []
        for c in range(count):
            child, offset = _read_required_string(bytes, offset)
            children.append(child)
        stat = ZnodeStat(*stat_struct.unpack_from(bytes, offset))
        return (children, stat), offset + stat_struct.size


@_packet
class CheckVersion:
    path: str
    version: int

    type: ClassVar[int] = 13

    def serialize(self) -> bytearray:
        b = bytearray()
        b.extend(_write_string(self.path))
        b.extend(int_struct.pack(self.version))
        return b


@_packet
class Create2:
    path: str
    data: bytes | None
    acl: Sequence[ACL]
    flags: int

    type: ClassVar[int] = 15

    def serialize(self) -> bytearray:
        b = bytearray()
        b.extend(_write_string(self.path))
        b.extend(_write_buffer(self.data))
        _write_acls(b, self.acl)
        b.extend(int_struct.pack(self.flags))
        return b

    @classmethod
    def deserialize(
        cls, bytes: bytes | memoryview, offset: int
    ) -> tuple[tuple[str, ZnodeStat], int]:
        path, offset = _read_required_string(bytes, offset)
        stat = ZnodeStat(*stat_struct.unpack_from(bytes, offset))
        return (path, stat), offset + stat_struct.size


@_packet
class CreateContainer:
    path: str
    data: bytes | None
    acl: Sequence[ACL]
    flags: int

    type: ClassVar[int] = 19

    def serialize(self) -> bytearray:
        b = bytearray()
        b.extend(_write_string(self.path))
        b.extend(_write_buffer(self.data))
        _write_acls(b, self.acl)
        b.extend(int_struct.pack(self.flags))
        return b

    @classmethod
    def deserialize(
        cls, bytes: bytes | memoryview, offset: int
    ) -> tuple[tuple[str, ZnodeStat], int]:
        path, offset = _read_required_string(bytes, offset)
        stat = ZnodeStat(*stat_struct.unpack_from(bytes, offset))
        return (path, stat), offset + stat_struct.size


@_packet
class CreateTTL:
    path: str
    data: bytes | None
    acl: Sequence[ACL]
    flags: int
    ttl: int

    type: ClassVar[int] = 21

    def serialize(self) -> bytearray:
        b = bytearray()
        b.extend(_write_string(self.path))
        b.extend(_write_buffer(self.data))
        _write_acls(b, self.acl)
        b.extend(int_struct.pack(self.flags))
        b.extend(long_struct.pack(self.ttl))
        return b

    @classmethod
    def deserialize(
        cls, bytes: bytes | memoryview, offset: int
    ) -> tuple[tuple[str, ZnodeStat], int]:
        path, offset = _read_required_string(bytes, offset)
        stat = ZnodeStat(*stat_struct.unpack_from(bytes, offset))
        return (path, stat), offset + stat_struct.size


@_packet
class Transaction:
    operations: list[_TransOpsTypes]

    type: ClassVar[int] = 14

    def serialize(self) -> bytearray:
        b = bytearray()
        for op in self.operations:
            b.extend(multiheader_struct.pack(op.type, False, -1))
            b.extend(op.serialize())
        b.extend(multiheader_struct.pack(-1, True, -1))
        return b

    @classmethod
    def deserialize(
        cls, bytes: bytes | memoryview, offset: int
    ) -> tuple[list[_TransOpsResponse], int]:
        results: list[_TransOpsResponse] = []
        unpack_hdr = multiheader_struct.unpack_from
        hdr_size = multiheader_struct.size
        unpack_stat = stat_struct.unpack_from
        stat_size = stat_struct.size
        unpack_int = int_struct.unpack_from
        int_size = int_struct.size

        op_type, done, _ = unpack_hdr(bytes, offset)
        offset += hdr_size
        if done not in (0, 1):
            raise MarshallingError(f"Invalid MultiHeader done flag: {done}")
        while not done:
            response: _TransOpsResponse = None
            if op_type == Create.type:
                response, offset = _read_required_string(bytes, offset)
            elif op_type in (
                Create2.type,
                CreateContainer.type,
                CreateTTL.type,
            ):
                path, offset = _read_required_string(bytes, offset)
                stat = ZnodeStat._make(unpack_stat(bytes, offset))
                offset += stat_size
                response = (path, stat)
            elif op_type == Delete.type or op_type == CheckVersion.type:
                response = True
            elif op_type == SetData.type:
                response = ZnodeStat(*unpack_stat(bytes, offset))
                offset += stat_size
            elif op_type == -1:
                err = unpack_int(bytes, offset)[0]
                offset += int_size
                response = _error_for_code(err)
            else:
                raise MarshallingError(
                    f"Unknown transaction op_type: {op_type}"
                )
            if response is not None:
                results.append(response)
            op_type, done, _ = unpack_hdr(bytes, offset)
            offset += hdr_size
            if done not in (0, 1):
                raise MarshallingError(
                    f"Invalid MultiHeader done flag: {done}"
                )
        return results, offset

    @staticmethod
    def unchroot(
        client: HasUnchroot, response: list[_TransOpsResponse]
    ) -> list[_TransOpsResponse]:
        resp: list[_TransOpsResponse] = []
        for result in response:
            if isinstance(result, str):
                resp.append(client.unchroot(result))
            elif isinstance(result, ZnodeStat):  # Need to test before tuple
                resp.append(result)
            elif isinstance(result, tuple):
                resp.append((client.unchroot(result[0]), result[1]))
            else:
                resp.append(result)
        return resp


@_packet
class Reconfig:
    joining: str | None
    leaving: str | None
    new_members: str | None
    config_id: int

    type: ClassVar[int] = 16

    def serialize(self) -> bytearray:
        b = bytearray()
        b.extend(_write_string(self.joining))
        b.extend(_write_string(self.leaving))
        b.extend(_write_string(self.new_members))
        b.extend(long_struct.pack(self.config_id))
        return b

    @classmethod
    def deserialize(
        cls, bytes: bytes | memoryview, offset: int
    ) -> tuple[tuple[bytes | None, ZnodeStat], int]:
        data, offset = _read_buffer(bytes, offset)
        stat = ZnodeStat(*stat_struct.unpack_from(bytes, offset))
        return (data, stat), offset + stat_struct.size


@_packet
class RemoveWatches:
    path: str
    watcher_type: int

    type: ClassVar[int] = 18

    def serialize(self) -> bytearray:
        b = bytearray()
        b.extend(_write_string(self.path))
        b.extend(int_struct.pack(self.watcher_type))
        return b

    @classmethod
    def deserialize(
        cls, bytes: bytes | memoryview, offset: int
    ) -> tuple[None, int]:
        return None, offset


@_packet
class Auth:
    auth_type: int
    scheme: str
    auth: str

    type: ClassVar[int] = 100

    def serialize(self) -> bytes:
        return (
            int_struct.pack(self.auth_type)
            + _write_string(self.scheme)
            + _write_string(self.auth)
        )


@_packet
class SASL:
    challenge: bytes | None

    type: ClassVar[int] = 102

    def serialize(self) -> bytearray:
        b = bytearray()
        b.extend(_write_buffer(self.challenge))
        return b

    @classmethod
    def deserialize(
        cls, bytes: bytes | memoryview, offset: int
    ) -> tuple[bytes | None, int]:
        challenge, offset = _read_buffer(bytes, offset)
        return challenge, offset


@_packet
class AddWatch:
    path: str
    watcher: WatchFunc
    mode: int

    type: ClassVar[int] = 106

    def serialize(self) -> bytearray:
        b = bytearray()
        b.extend(_write_string(self.path))
        b.extend(int_struct.pack(self.mode))
        return b

    @classmethod
    def deserialize(
        cls, bytes: bytes | memoryview, offset: int
    ) -> tuple[None, int]:
        # The server replies with an ErrorResponse: a single int (always 0).
        return None, offset + int_struct.size


@_packet
class ReplyHeader:
    xid: int
    zxid: int
    err: int

    @classmethod
    def deserialize(
        cls, bytes: bytes | memoryview, offset: int
    ) -> tuple[ReplyHeader, int]:
        """Given bytes and the current bytes offset, return a
        :class:`ReplyHeader` instance and the new offset"""
        new_offset = offset + reply_header_struct.size
        return (
            cls(*reply_header_struct.unpack_from(bytes, offset)),
            new_offset,
        )


@_packet
class MultiHeader:
    type: int | None
    done: bool
    err: int | None

    def serialize(self) -> bytearray:
        assert self.type is not None
        assert self.err is not None
        b = bytearray()
        b.extend(int_struct.pack(self.type))
        b.extend([1 if self.done else 0])
        b.extend(int_struct.pack(self.err))
        return b

    @classmethod
    def deserialize(
        cls, bytes: bytes | memoryview, offset: int
    ) -> tuple[MultiHeader, int]:
        t, done, err = multiheader_struct.unpack_from(bytes, offset)
        offset += multiheader_struct.size
        return cls(t, done == 1, err), offset


# =====================================================================
# Decoded Server-to-Client Protocol Packets & Unified FrameDecoder
# =====================================================================


@_packet
class PingResponse:
    """Decoded server heartbeat PING reply (xid == ReservedXID.PING)."""

    zxid: int
    err: int = 0

    @classmethod
    def deserialize(
        cls, buffer: bytes | memoryview, offset: int
    ) -> tuple[PingResponse, int]:
        """Direct/test helper; bypassed by FrameDecoder._decode_packet()."""
        _, zxid, err = reply_header_struct.unpack_from(buffer, offset)
        return cls(zxid=zxid, err=err), offset + reply_header_struct.size


@_packet
class AuthResponse:
    """Decoded server AUTH reply (xid == ReservedXID.AUTH)."""

    zxid: int
    err: int = 0

    @classmethod
    def deserialize(
        cls, buffer: bytes | memoryview, offset: int
    ) -> tuple[AuthResponse, int]:
        """Direct/test helper; bypassed by FrameDecoder._decode_packet()."""
        _, zxid, err = reply_header_struct.unpack_from(buffer, offset)
        return cls(zxid=zxid, err=err), offset + reply_header_struct.size


@_packet
class WatchPacket:
    """Decoded server WATCH notification packet (xid == ReservedXID.WATCH)."""

    zxid: int
    type: int
    state: int
    path: str

    @classmethod
    def deserialize(
        cls, buffer: bytes | memoryview, offset: int
    ) -> tuple[WatchPacket, int]:
        """Direct/test helper; bypassed by FrameDecoder._decode_packet()."""
        _, zxid, _, watch_type, state = watch_header_struct.unpack_from(
            buffer, offset
        )
        path, offset = _read_required_string(
            buffer, offset + watch_header_struct.size
        )
        return (
            cls(zxid=zxid, type=watch_type, state=state, path=path),
            offset,
        )


@_packet
class SASLResponse:
    """Decoded server SASL challenge/response packet."""

    xid: int
    zxid: int
    err: int
    challenge: bytes | None = None


JUTE_MAX_BUFFER = 0xFFFFF
"""Default ``jute.maxbuffer`` limit of a ZooKeeper server, in bytes.

Both the server and :class:`FrameDecoder` enforce this as a limit (see
:data:`REPLY_OVERHEAD` for the per-reply framing allowance).  Replies larger
than ``max_buffer + REPLY_OVERHEAD`` fail with
:class:`~kazoo.exceptions.ReplyTooLargeError` (or drop the connection for
reserved XIDs).

Configurable through :class:`~kazoo.client.KazooClient`'s ``max_buffer``
parameter.
"""
REPLY_OVERHEAD = 88
"""Framing allowance (16 header + 4 length + 68 stat) above ``max_buffer``."""

READ_WINDOW = 256 * 1024
"""Maximum start offset ``L`` (256 KiB) for any buffered frame."""

MOVE_MAX = 64 * 1024
"""Maximum partial-frame byte count (64 KiB) moved to offset 0 near ``L``."""


@_packet
class ResponsePacket:
    """Reply to an in-flight request, correlated and deserialized."""

    header: ReplyHeader
    request: Any
    async_object: ICompletion[Any] | None
    response: Any = None
    error: Exception | None = None


class FrameDecoder:
    """Sans-I/O length-prefixed frame codec and server packet decoder."""

    __slots__ = (
        "_max_buffer",
        "_max_payload",
        "_max_frame",
        "_capacity",
        "_buf",
        "_view",
        "_empty_view",
        "_head",
        "_tail",
        "_scan",
        "_frame_len",
        "_skip",
        "_pop_pending",
    )

    def __init__(
        self,
        *,
        max_buffer: int = JUTE_MAX_BUFFER,
        pop_pending: PopPending | None = None,
    ) -> None:
        if max_buffer < 0:
            raise ValueError(f"max_buffer must be non-negative: {max_buffer}")
        self._max_buffer = max_buffer
        self._max_payload = max_buffer + REPLY_OVERHEAD
        self._max_frame = 4 + self._max_payload
        self._capacity = READ_WINDOW + self._max_frame
        self._buf = bytearray()
        self._view: memoryview | None = None
        self._empty_view: memoryview | None = None
        self._head = 0
        self._tail = 0
        self._scan = 0
        # Payload length of the frame starting at ``_head``, or -1 until its
        # 4-byte header has been buffered and validated.
        self._frame_len = -1
        # Remaining payload/trailing bytes of an oversize frame still to skip.
        self._skip = 0
        self._pop_pending = pop_pending

    def _ensure_buffer(self) -> memoryview:
        """Lazily allocate the fixed frame buffer and its cached views."""
        if self._view is None:
            self._buf = bytearray(self._capacity)
            self._view = memoryview(self._buf)
            self._empty_view = self._view[:READ_WINDOW]
        return self._view

    def reset(self) -> None:
        """Clear buffered bytes and any pending oversize skip count."""
        self._head = 0
        self._tail = 0
        self._scan = 0
        self._frame_len = -1
        self._skip = 0

    def release(self) -> None:
        """Release cached views and free the backing buffer.

        The decoder may be reused afterward; the next :meth:`get_buffer`
        call allocates a fresh buffer.
        """
        self.reset()
        if self._empty_view is not None:
            self._empty_view.release()
            self._empty_view = None
        if self._view is not None:
            self._view.release()
            self._view = None
        self._buf = bytearray()

    def _advance_scan(self) -> None:
        """Advance ``_scan`` past complete normal frames and check headers."""
        buf = self._buf
        tail = self._tail
        s = self._scan
        max_payload = self._max_payload
        while True:
            avail = tail - s
            if avail < 4:
                break
            if avail >= 8:
                msg_len, xid = int_int_struct.unpack_from(buf, s)
                if msg_len < 16:
                    if s == self._head:
                        raise ConnectionDropped(
                            f"Invalid frame length: {msg_len}"
                        )
                    break
                if s == self._head:
                    self._frame_len = msg_len
                if msg_len > max_payload:
                    if xid <= 0:
                        if s == self._head:
                            raise ConnectionDropped(
                                f"Oversize frame length {msg_len} for "
                                f"non-request xid {xid}"
                            )
                    break
            else:
                msg_len = int_struct.unpack_from(buf, s)[0]
                if msg_len < 16:
                    if s == self._head:
                        raise ConnectionDropped(
                            f"Invalid frame length: {msg_len}"
                        )
                    break
                if s == self._head:
                    self._frame_len = msg_len
                break
            total = 4 + msg_len
            if avail < total:
                break
            s += total
        self._scan = s

    @property
    def frame_ready(self) -> bool:
        """Return True if a complete unpopped frame is already buffered."""
        avail = self._tail - self._head
        n = self._frame_len
        if n < 0:
            if avail < 4:
                return False
            n = int_struct.unpack_from(self._buf, self._head)[0]
            if n < 16:
                raise ConnectionDropped(f"Invalid frame length: {n}")
            self._frame_len = n
        if n > self._max_payload:
            return avail >= 20
        return avail >= 4 + n

    def get_buffer(self, sizehint: int = -1) -> memoryview:
        """Return a writable memoryview for ``socket.recv_into``.

        All complete frames (or actionable oversize headers) must be popped
        before asking for a window past ``READ_WINDOW``; raises ``BufferError``
        if complete frames must be popped first.

        ``sizehint`` is ignored; it is accepted so that the method matches
        ``asyncio.BufferedProtocol.get_buffer``.

        .. warning::
            Never call ``release()`` on the returned view and never use it
            in a ``with`` block. When nothing is buffered (or while skipping
            an oversize frame) it is a cached slice of the decoder's view, not
            a fresh allocation. Releasing it breaks this decoder for good, and
            :meth:`reset` does not recover it.
        """
        del sizehint
        view = self._ensure_buffer()
        empty_view = self._empty_view
        assert empty_view is not None
        if self._skip > 0:
            return empty_view

        head = self._head
        tail = self._tail
        if head == tail:
            # Nothing buffered: return cached [0:READ_WINDOW] slice.
            self._head = 0
            self._tail = 0
            self._scan = 0
            return empty_view

        end = READ_WINDOW
        msg_len = -1
        while True:
            s = self._scan
            if s >= READ_WINDOW:
                raise BufferError("pop frames before requesting a buffer")
            avail = tail - s
            if avail < 4:
                end = s + 4 if s + 4 > READ_WINDOW else READ_WINDOW
                break
            msg_len = (
                self._frame_len
                if s == head and self._frame_len >= 0
                else int_struct.unpack_from(self._buf, s)[0]
            )
            if msg_len < 16:
                if s > head:
                    raise BufferError("pop frames before requesting a buffer")
                raise ConnectionDropped(f"Invalid frame length: {msg_len}")
            if msg_len > self._max_payload:
                if s > head and avail >= 8:
                    xid = int_struct.unpack_from(self._buf, s + 4)[0]
                    if xid <= 0:
                        raise BufferError(
                            "pop frames before requesting a buffer"
                        )
                if avail >= 20:
                    raise BufferError("pop frames before requesting a buffer")
                end = s + 20 if s + 20 > READ_WINDOW else READ_WINDOW
                break
            total = 4 + msg_len
            if avail >= total:
                self._advance_scan()
                continue
            end = s + total
            if end < READ_WINDOW:
                end = READ_WINDOW
            break

        p = tail - head
        if end - tail < MOVE_MAX and p <= MOVE_MAX:
            view[:p] = view[head:tail]
            s -= head
            self._head = 0
            self._tail = p
            self._scan = s
            tail = p
            if avail < 4:
                end = s + 4 if s + 4 > READ_WINDOW else READ_WINDOW
            elif msg_len > self._max_payload:
                end = s + 20 if s + 20 > READ_WINDOW else READ_WINDOW
            else:
                end = s + 4 + msg_len
                if end < READ_WINDOW:
                    end = READ_WINDOW

        if end <= tail:
            raise BufferError("pop frames before requesting a buffer")
        return view[tail:end]

    def buffer_updated(self, nbytes: int) -> None:
        """Advance the write cursor after ``recv_into`` writes ``nbytes``."""
        if nbytes < 0:
            raise ValueError(f"nbytes must be non-negative: {nbytes}")
        if nbytes == 0:
            return
        if self._skip > 0:
            if nbytes > READ_WINDOW:
                raise ValueError("nbytes exceeds read window capacity")
            d = min(self._skip, nbytes)
            self._skip -= d
            if d == nbytes:
                self._head = 0
                self._tail = 0
                self._scan = 0
                return
            self._head = d
            self._tail = nbytes
            self._scan = d
            self._frame_len = -1
            self._advance_scan()
            return
        if self._tail + nbytes > self._capacity:
            raise ValueError("nbytes exceeds main frame buffer capacity")
        self._tail += nbytes
        self._advance_scan()

    def _feed(self, data: bytes | bytearray | memoryview) -> None:
        """Internal test-only helper to feed bytes without ``recv_into``.

        Copies ``data`` in chunks through :meth:`get_buffer` and
        :meth:`buffer_updated`. Runtime I/O does not call this method; it
        reads directly into ``get_buffer()`` via ``socket.recv_into()``.
        """
        if not data:
            return
        mv = memoryview(data)
        if mv.format != "B":
            mv = mv.cast("B")
        try:
            pos = 0
            total = len(mv)
            while pos < total:
                buf = self.get_buffer()
                take = min(total - pos, len(buf))
                buf[:take] = mv[pos : pos + take]
                self.buffer_updated(take)
                pos += take
        finally:
            mv.release()

    def _pop_request(self, xid: int) -> tuple[Any, ICompletion[Any] | None]:
        """Pop the oldest pending request and verify its XID matches xid."""
        if self._pop_pending is None:
            raise ConnectionDropped(
                f"Received reply for xid {xid} with no pending queue"
            )
        try:
            request, async_object, expected_xid = self._pop_pending()
        except IndexError as e:
            raise ConnectionDropped(
                f"Received unexpected reply for xid {xid} with empty pending"
            ) from e
        if xid != expected_xid:
            exc = RuntimeError(
                f"xids do not match, expected {expected_xid!r} "
                f"received {xid!r}"
            )
            if async_object is not None:
                async_object.set_exception(exc)
            raise exc
        return request, async_object

    def pop_packet(self) -> ServerPacket | None:
        """Pop and decode the next typed server packet if complete."""
        avail = self._tail - self._head
        n = self._frame_len
        if n < 0:
            if avail < 4:
                return None
            n = int_struct.unpack_from(self._buf, self._head)[0]
            if n < 16:
                raise ConnectionDropped(f"Invalid frame length: {n}")
            self._frame_len = n

        if n > self._max_payload:
            if avail >= 8:
                xid = int_struct.unpack_from(self._buf, self._head + 4)[0]
                if xid <= 0:
                    raise ConnectionDropped(
                        f"Oversize frame length {n} for non-request xid {xid}"
                    )
            if avail < 20:
                return None
            header = ReplyHeader(
                *reply_header_struct.unpack_from(self._buf, self._head + 4)
            )
            end = self._head + 4 + n
            if end > self._tail:
                self._skip = end - self._tail
                self._head = 0
                self._tail = 0
                self._scan = 0
            elif end == self._tail:
                self._skip = 0
                self._head = 0
                self._tail = 0
                self._scan = 0
            else:
                self._skip = 0
                self._head = end
                self._scan = end
            self._frame_len = -1

            request, async_object = self._pop_request(header.xid)
            if request.type == SASL.type:
                raise ConnectionDropped(
                    f"Oversize SASL reply of {n} bytes exceeds max_buffer "
                    f"({self._max_buffer} + {REPLY_OVERHEAD} = "
                    f"{self._max_payload} bytes)"
                )
            return ResponsePacket(
                header=header,
                request=request,
                async_object=async_object,
                error=ReplyTooLargeError(
                    f"Reply of {n} bytes exceeds max_buffer limit "
                    f"({self._max_buffer} + {REPLY_OVERHEAD} = "
                    f"{self._max_payload} bytes)"
                ),
            )

        start = self._head + 4
        end = start + n
        if end > self._tail:
            return None

        assert self._view is not None
        frame_view = self._view[start:end]
        # Consume the frame before decoding it, so the cursors are
        # consistent even when decoding raises.
        if end == self._tail:
            self._head = 0
            self._tail = 0
            self._scan = 0
        else:
            self._head = end
            if self._scan < end:
                self._scan = end
        self._frame_len = -1

        try:
            return self._decode_packet(frame_view)
        finally:
            frame_view.release()

    def _decode_packet(self, frame: bytes | memoryview) -> ServerPacket:
        """Internal helper to decode a complete raw frame into a ServerPacket.

        Called by :meth:`pop_packet` at runtime and directly by unit tests for
        single-frame decoding.
        """
        size = len(frame)
        if size < 16:
            raise ConnectionDropped(
                f"Invalid frame length: {size} (expected >= 16)"
            )
        # Every decoded frame must be consumed exactly: a length field that
        # claims more or fewer bytes than the frame holds is corruption.
        # Slicing clamps at the end of the frame, so an overstated last
        # field would otherwise pass unnoticed.
        xid, zxid, err = reply_header_struct.unpack_from(frame, 0)
        if xid <= 0:
            if xid == ReservedXID.CONNECT:
                try:
                    connect_result, end = Connect.deserialize(frame, 0)
                except Exception as e:
                    raise ConnectionDropped(
                        "invalid server connect response"
                    ) from e
                if end != size:
                    raise ConnectionDropped(
                        f"invalid server connect response: parsed {end} "
                        f"of {size} bytes"
                    )
                return connect_result
            if xid == ReservedXID.WATCH:
                try:
                    watch_type, state = int_int_struct.unpack_from(frame, 16)
                    path, end = _read_required_string(
                        frame, watch_header_struct.size
                    )
                except (
                    struct.error,
                    UnicodeDecodeError,
                    MarshallingError,
                ) as e:
                    raise ConnectionDropped(
                        "invalid server watch packet"
                    ) from e
                if end != size:
                    raise ConnectionDropped(
                        f"invalid server watch packet: parsed {end} "
                        f"of {size} bytes"
                    )
                return WatchPacket(
                    zxid=zxid, type=watch_type, state=state, path=path
                )
            if xid == ReservedXID.PING:
                return PingResponse(zxid=zxid, err=err)
            if xid == ReservedXID.AUTH:
                return AuthResponse(zxid=zxid, err=err)
            raise ConnectionDropped(f"Unknown reserved xid: {xid}")

        request, async_object = self._pop_request(xid)
        header = ReplyHeader(xid, zxid, err)

        if request.type == SASL.type:
            challenge: bytes | None = None
            if not err:
                try:
                    challenge, end = SASL.deserialize(frame, 16)
                except (struct.error, MarshallingError) as e:
                    raise ConnectionDropped(
                        "invalid server SASL response"
                    ) from e
                if end != size:
                    raise ConnectionDropped(
                        f"invalid server SASL response: parsed {end} "
                        f"of {size} bytes"
                    )
            return SASLResponse(
                xid=xid,
                zxid=zxid,
                err=err,
                challenge=challenge,
            )

        if err:
            return ResponsePacket(
                header=header,
                request=request,
                async_object=async_object,
            )

        try:
            response, end = request.deserialize(frame, 16)
        except Exception as exc:
            return ResponsePacket(
                header=header,
                request=request,
                async_object=async_object,
                error=exc,
            )
        if end != size:
            # The frame boundary is known, so the stream stays in sync:
            # fail this request only.
            return ResponsePacket(
                header=header,
                request=request,
                async_object=async_object,
                error=MarshallingError(
                    f"invalid {type(request).__name__} reply: parsed {end} "
                    f"of {size} bytes"
                ),
            )
        return ResponsePacket(
            header=header,
            request=request,
            async_object=async_object,
            response=response,
        )

    @staticmethod
    def encode_request_into(
        buf: bytearray, request: Any, xid: int | None = None
    ) -> None:
        """Append a 4-byte length-prefixed wire frame into ``buf``."""
        start = len(buf)
        buf.extend(b"\x00\x00\x00\x00")
        if xid is not None:
            buf.extend(int_struct.pack(xid))
        if request.type is not None:
            buf.extend(int_struct.pack(request.type))
        buf.extend(request.serialize())
        int_struct.pack_into(buf, start, len(buf) - start - 4)

    @staticmethod
    def encode_request(request: Any, xid: int | None = None) -> bytes:
        """Encode a client request into a 4-byte length-prefixed wire frame."""
        b = bytearray()
        FrameDecoder.encode_request_into(b, request, xid)
        return bytes(b)


__all__ = (
    "AddWatch",
    "Auth",
    "AuthResponse",
    "CheckVersion",
    "Close",
    "Connect",
    "Create",
    "Create2",
    "CreateContainer",
    "CreateTTL",
    "Delete",
    "Exists",
    "FrameDecoder",
    "GetACL",
    "GetChildren",
    "GetChildren2",
    "GetData",
    "JUTE_MAX_BUFFER",
    "MultiHeader",
    "Ping",
    "PingResponse",
    "Reconfig",
    "RemoveWatches",
    "ReplyHeader",
    "ResponsePacket",
    "SASL",
    "SASLResponse",
    "SetACL",
    "SetData",
    "Sync",
    "Transaction",
    "WatchPacket",
    "ReservedXID",
)

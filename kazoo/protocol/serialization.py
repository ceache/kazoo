"""Zookeeper Serializers, Deserializers, and Frame Codec"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import IntEnum
import struct
from typing import ClassVar, TYPE_CHECKING

from kazoo.exceptions import ConnectionDropped, EXCEPTIONS, ZookeeperError
from kazoo.protocol.states import ZnodeStat
from kazoo.security import ACL, Id, Permissions

if TYPE_CHECKING:
    from typing import Any, TypeAlias
    from collections.abc import Sequence
    from kazoo.client import KazooClient, WatchFunc

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
        " | ReplyPacket"
    )


class ReservedXID(IntEnum):
    """Reserved negative ZooKeeper wire protocol XIDs."""

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
stat_struct = struct.Struct("!qqqqiiiqiiq")


def _read_string(buffer: bytes, offset: int) -> tuple[str, int]:
    """Reads an int specified buffer into a string and returns the
    string and the new offset in the buffer"""
    length = int_struct.unpack_from(buffer, offset)[0]
    offset += int_struct.size
    if length < 0:
        # A note: write_str sends a length of -1 to indicate a value of None
        # was passed. Not entirely sure where this happens because none of the
        # callers of read_string seem to expect a None value.
        return None, offset  # type: ignore[return-value]
    else:
        index = offset
        offset += length
        return buffer[index : index + length].decode("utf-8"), offset


def _read_acl(bytes: bytes, offset: int) -> tuple[ACL, int]:
    perms = int_struct.unpack_from(bytes, offset)[0]
    offset += int_struct.size
    scheme, offset = _read_string(bytes, offset)
    id, offset = _read_string(bytes, offset)
    return ACL(Permissions(perms), Id(scheme, id)), offset


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


def _read_buffer(bytes: bytes, offset: int) -> tuple[bytes | None, int]:
    length = int_struct.unpack_from(bytes, offset)[0]
    offset += int_struct.size
    if length < 0:
        return None, offset
    else:
        index = offset
        offset += length
        return bytes[index : index + length], offset


@dataclass(frozen=True, slots=True)
class Close:
    type: ClassVar[int] = -11

    @classmethod
    def serialize(cls) -> bytes:
        return b""


@dataclass(frozen=True, slots=True)
class Ping:
    type: ClassVar[int] = 11

    @classmethod
    def serialize(cls) -> bytes:
        return b""


@dataclass(frozen=True, slots=True)
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
    def deserialize(cls, bytes: bytes, offset: int) -> tuple[Connect, int]:
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


@dataclass(frozen=True, slots=True)
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
        b.extend(int_struct.pack(len(self.acl)))
        for acl in self.acl:
            b.extend(
                int_struct.pack(acl.perms)
                + _write_string(acl.id.scheme)
                + _write_string(acl.id.id)
            )
        b.extend(int_struct.pack(self.flags))
        return b

    @classmethod
    def deserialize(cls, bytes: bytes, offset: int) -> str:
        return _read_string(bytes, offset)[0]


@dataclass(frozen=True, slots=True)
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
    def deserialize(cls, bytes: bytes, offset: int) -> bool:
        return True


@dataclass(frozen=True, slots=True)
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
    def deserialize(cls, bytes: bytes, offset: int) -> ZnodeStat | None:
        stat = ZnodeStat(*stat_struct.unpack_from(bytes, offset))
        return stat if stat.czxid != -1 else None


@dataclass(frozen=True, slots=True)
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
        cls, bytes: bytes, offset: int
    ) -> tuple[bytes | None, ZnodeStat]:
        data, offset = _read_buffer(bytes, offset)
        stat = ZnodeStat(*stat_struct.unpack_from(bytes, offset))
        return data, stat


@dataclass(frozen=True, slots=True)
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
    def deserialize(cls, bytes: bytes, offset: int) -> ZnodeStat:
        return ZnodeStat(*stat_struct.unpack_from(bytes, offset))


@dataclass(frozen=True, slots=True)
class GetACL:
    path: str

    type: ClassVar[int] = 6

    def serialize(self) -> bytearray:
        return bytearray(_write_string(self.path))

    @classmethod
    def deserialize(
        cls, bytes: bytes, offset: int
    ) -> tuple[list[ACL], ZnodeStat] | list[ACL]:
        count = int_struct.unpack_from(bytes, offset)[0]
        offset += int_struct.size
        if count == -1:  # pragma: nocover
            return []

        acls = []
        for c in range(count):
            acl, offset = _read_acl(bytes, offset)
            acls.append(acl)
        stat = ZnodeStat(*stat_struct.unpack_from(bytes, offset))
        return acls, stat


@dataclass(frozen=True, slots=True)
class SetACL:
    path: str
    acls: Sequence[ACL]
    version: int

    type: ClassVar[int] = 7

    def serialize(self) -> bytearray:
        b = bytearray()
        b.extend(_write_string(self.path))
        b.extend(int_struct.pack(len(self.acls)))
        for acl in self.acls:
            b.extend(
                int_struct.pack(acl.perms)
                + _write_string(acl.id.scheme)
                + _write_string(acl.id.id)
            )
        b.extend(int_struct.pack(self.version))
        return b

    @classmethod
    def deserialize(cls, bytes: bytes, offset: int) -> ZnodeStat:
        return ZnodeStat(*stat_struct.unpack_from(bytes, offset))


@dataclass(frozen=True, slots=True)
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
    def deserialize(cls, bytes: bytes, offset: int) -> list[str]:
        count = int_struct.unpack_from(bytes, offset)[0]
        offset += int_struct.size
        if count == -1:  # pragma: nocover
            return []

        children = []
        for c in range(count):
            child, offset = _read_string(bytes, offset)
            children.append(child)
        return children


@dataclass(frozen=True, slots=True)
class Sync:
    path: str

    type: ClassVar[int] = 9

    def serialize(self) -> bytes:
        return _write_string(self.path)

    @classmethod
    def deserialize(cls, buffer: bytes, offset: int) -> str:
        return _read_string(buffer, offset)[0]


@dataclass(frozen=True, slots=True)
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
        cls, bytes: bytes, offset: int
    ) -> tuple[list[str], ZnodeStat] | list[str]:
        count = int_struct.unpack_from(bytes, offset)[0]
        offset += int_struct.size
        if count == -1:  # pragma: nocover
            return []

        children: list[str] = []
        for c in range(count):
            child, offset = _read_string(bytes, offset)
            children.append(child)
        stat = ZnodeStat(*stat_struct.unpack_from(bytes, offset))
        return children, stat


@dataclass(frozen=True, slots=True)
class CheckVersion:
    path: str
    version: int

    type: ClassVar[int] = 13

    def serialize(self) -> bytearray:
        b = bytearray()
        b.extend(_write_string(self.path))
        b.extend(int_struct.pack(self.version))
        return b


@dataclass(frozen=True, slots=True)
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
        b.extend(int_struct.pack(len(self.acl)))
        for acl in self.acl:
            b.extend(
                int_struct.pack(acl.perms)
                + _write_string(acl.id.scheme)
                + _write_string(acl.id.id)
            )
        b.extend(int_struct.pack(self.flags))
        return b

    @classmethod
    def deserialize(cls, bytes: bytes, offset: int) -> tuple[str, ZnodeStat]:
        path, offset = _read_string(bytes, offset)
        stat = ZnodeStat(*stat_struct.unpack_from(bytes, offset))
        return path, stat


@dataclass(frozen=True, slots=True)
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
        b.extend(int_struct.pack(len(self.acl)))
        for acl in self.acl:
            b.extend(
                int_struct.pack(acl.perms)
                + _write_string(acl.id.scheme)
                + _write_string(acl.id.id)
            )
        b.extend(int_struct.pack(self.flags))
        return b

    @classmethod
    def deserialize(cls, bytes: bytes, offset: int) -> tuple[str, ZnodeStat]:
        path, offset = _read_string(bytes, offset)
        stat = ZnodeStat(*stat_struct.unpack_from(bytes, offset))
        return path, stat


@dataclass(frozen=True, slots=True)
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
        b.extend(int_struct.pack(len(self.acl)))
        for acl in self.acl:
            b.extend(
                int_struct.pack(acl.perms)
                + _write_string(acl.id.scheme)
                + _write_string(acl.id.id)
            )
        b.extend(int_struct.pack(self.flags))
        b.extend(long_struct.pack(self.ttl))
        return b

    @classmethod
    def deserialize(cls, bytes: bytes, offset: int) -> tuple[str, ZnodeStat]:
        path, offset = _read_string(bytes, offset)
        stat = ZnodeStat(*stat_struct.unpack_from(bytes, offset))
        return path, stat


@dataclass(frozen=True, slots=True)
class Transaction:
    operations: list[_TransOpsTypes]

    type: ClassVar[int] = 14

    def serialize(self) -> bytearray:
        b = bytearray()
        for op in self.operations:
            b.extend(
                MultiHeader(op.type, False, -1).serialize() + op.serialize()
            )
        return b + multiheader_struct.pack(-1, True, -1)

    @classmethod
    def deserialize(cls, bytes: bytes, offset: int) -> list[_TransOpsResponse]:
        header = MultiHeader(None, False, None)
        results: list[_TransOpsResponse] = []
        response: _TransOpsResponse = None
        while not header.done:
            if header.type == Create.type:
                response, offset = _read_string(bytes, offset)
            elif header.type in (
                Create2.type,
                CreateContainer.type,
                CreateTTL.type,
            ):
                path, offset = _read_string(bytes, offset)
                stat = ZnodeStat._make(stat_struct.unpack_from(bytes, offset))
                offset += stat_struct.size
                response = (path, stat)
            elif header.type == Delete.type:
                response = True
            elif header.type == SetData.type:
                response = ZnodeStat(*stat_struct.unpack_from(bytes, offset))
                offset += stat_struct.size
            elif header.type == CheckVersion.type:
                response = True
            elif header.type == -1:
                err = int_struct.unpack_from(bytes, offset)[0]
                offset += int_struct.size
                response = EXCEPTIONS[err]()
            if response:
                results.append(response)
            header, offset = MultiHeader.deserialize(bytes, offset)
        return results

    @staticmethod
    def unchroot(
        client: KazooClient, response: list[_TransOpsResponse]
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


@dataclass(frozen=True, slots=True)
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
        cls, bytes: bytes, offset: int
    ) -> tuple[bytes | None, ZnodeStat]:
        data, offset = _read_buffer(bytes, offset)
        stat = ZnodeStat(*stat_struct.unpack_from(bytes, offset))
        return data, stat


@dataclass(frozen=True, slots=True)
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
    def deserialize(cls, bytes: bytes, offset: int) -> None:
        del bytes, offset
        return None


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
class SASL:
    challenge: bytes | None

    type: ClassVar[int] = 102

    def serialize(self) -> bytearray:
        b = bytearray()
        b.extend(_write_buffer(self.challenge))
        return b

    @classmethod
    def deserialize(
        cls, bytes: bytes, offset: int
    ) -> tuple[bytes | None, int]:
        challenge, offset = _read_buffer(bytes, offset)
        return challenge, offset


@dataclass(frozen=True, slots=True)
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
    def deserialize(cls, bytes: bytes, offset: int) -> None:
        del bytes, offset
        return None


@dataclass(frozen=True, slots=True)
class Watch:
    type: int
    state: int
    path: str

    @classmethod
    def deserialize(cls, bytes: bytes, offset: int) -> tuple[Watch, int]:
        """Given bytes and the current bytes offset, return the
        type, state, path, and new offset"""
        type, state = int_int_struct.unpack_from(bytes, offset)
        offset += int_int_struct.size
        path, offset = _read_string(bytes, offset)
        return cls(type, state, path), offset


@dataclass(frozen=True, slots=True)
class ReplyHeader:
    xid: int
    zxid: int
    err: int

    @classmethod
    def deserialize(cls, bytes: bytes, offset: int) -> tuple[ReplyHeader, int]:
        """Given bytes and the current bytes offset, return a
        :class:`ReplyHeader` instance and the new offset"""
        new_offset = offset + reply_header_struct.size
        return (
            cls(*reply_header_struct.unpack_from(bytes, offset)),
            new_offset,
        )


@dataclass(frozen=True, slots=True)
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
    def deserialize(cls, bytes: bytes, offset: int) -> tuple[MultiHeader, int]:
        t, done, err = multiheader_struct.unpack_from(bytes, offset)
        offset += multiheader_struct.size
        return cls(t, done == 1, err), offset


# =====================================================================
# Decoded Server-to-Client Protocol Packets & Unified FrameDecoder
# =====================================================================


@dataclass(frozen=True, slots=True)
class PingResponse:
    """Decoded server heartbeat PING reply (xid == XID.PING)."""

    zxid: int
    err: int = 0


@dataclass(frozen=True, slots=True)
class AuthResponse:
    """Decoded server AUTH reply (xid == XID.AUTH)."""

    zxid: int
    err: int = 0


@dataclass(frozen=True, slots=True)
class WatchPacket:
    """Decoded server WATCH notification packet (xid == XID.WATCH)."""

    zxid: int
    watch: Watch


@dataclass(frozen=True, slots=True)
class SASLResponse:
    """Decoded server SASL challenge/response packet."""

    xid: int
    zxid: int
    err: int
    challenge: bytes | None = None


@dataclass(frozen=True, slots=True)
class ReplyPacket:
    """Decoded server RPC reply header and payload slice."""

    header: ReplyHeader
    buffer: bytes
    offset: int


class FrameDecoder:
    """Sans-I/O length-prefixed frame codec and server packet decoder."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._frames: deque[bytes] = deque()

    def reset(self) -> None:
        """Clear all buffered partial bytes and decoded frames."""
        self._buffer.clear()
        self._frames.clear()

    def bytes_needed(self) -> int:
        """Return the number of bytes needed to complete the next frame."""
        if self._frames:
            return 0
        buf_len = len(self._buffer)
        if buf_len < 4:
            return 4 - buf_len
        frame_length = int(int_struct.unpack_from(self._buffer, 0)[0])
        if frame_length < 0:
            raise ConnectionDropped(
                f"Invalid negative frame length: {frame_length}"
            )
        total_needed = 4 + frame_length
        if buf_len >= total_needed:
            return 0
        return total_needed - buf_len

    def feed(self, data: bytes) -> None:
        """Append incoming bytes and extract any completed frames."""
        if not data:
            return
        self._buffer.extend(data)
        while len(self._buffer) >= 4:
            frame_length = int_struct.unpack_from(self._buffer, 0)[0]
            if frame_length < 0:
                raise ConnectionDropped(
                    f"Invalid negative frame length: {frame_length}"
                )
            total_len = 4 + frame_length
            if len(self._buffer) < total_len:
                break
            self._frames.append(bytes(self._buffer[4:total_len]))
            del self._buffer[:total_len]

    def pop_raw_frame(self) -> bytes | None:
        """Pop the next complete raw frame payload if available."""
        if self._frames:
            return self._frames.popleft()
        return None

    def pop_packet(
        self,
        *,
        is_connect: bool = False,
        is_sasl: bool = False,
    ) -> ServerPacket | None:
        """Pop and decode the next typed server packet if a complete frame is
        available.
        """
        frame = self.pop_raw_frame()
        if frame is None:
            return None
        return self.decode_packet(
            frame, is_connect=is_connect, is_sasl=is_sasl
        )

    @staticmethod
    def decode_packet(
        frame: bytes,
        *,
        is_connect: bool = False,
        is_sasl: bool = False,
    ) -> ServerPacket:
        """Decode a complete raw frame into a typed ServerPacket."""
        if is_connect:
            try:
                connect_result, _ = Connect.deserialize(frame, 0)
                return connect_result
            except Exception as e:
                raise ConnectionDropped(
                    "invalid server connect response"
                ) from e

        header, offset = ReplyHeader.deserialize(frame, 0)
        if header.xid < 0:
            if header.xid == ReservedXID.PING:
                return PingResponse(zxid=header.zxid, err=header.err)
            if header.xid == ReservedXID.AUTH:
                return AuthResponse(zxid=header.zxid, err=header.err)
            if header.xid == ReservedXID.WATCH:
                watch, _ = Watch.deserialize(frame, offset)
                return WatchPacket(zxid=header.zxid, watch=watch)
        if is_sasl:
            challenge: bytes | None = None
            if not header.err:
                challenge, _ = SASL.deserialize(frame, offset)
            return SASLResponse(
                xid=header.xid,
                zxid=header.zxid,
                err=header.err,
                challenge=challenge,
            )
        return ReplyPacket(header=header, buffer=frame, offset=offset)

    @staticmethod
    def encode_request(request: Any, xid: int | None = None) -> bytes:
        """Encode a client request into a 4-byte length-prefixed wire frame."""
        b = bytearray()
        if xid:
            b.extend(int_struct.pack(xid))
        if request.type:
            b.extend(int_struct.pack(request.type))
        b += request.serialize()
        return int_struct.pack(len(b)) + b


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
    "MultiHeader",
    "Ping",
    "PingResponse",
    "Reconfig",
    "RemoveWatches",
    "ReplyHeader",
    "ReplyPacket",
    "SASL",
    "SASLResponse",
    "SetACL",
    "SetData",
    "Sync",
    "Transaction",
    "Watch",
    "WatchPacket",
    "ReservedXID",
)

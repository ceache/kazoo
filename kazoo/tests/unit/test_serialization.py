from __future__ import annotations

import collections
import random
import struct

import pytest

from kazoo.exceptions import (
    ConnectionDropped,
    MarshallingError,
    ReplyTooLargeError,
    ZookeeperError,
)
from kazoo.protocol import serialization
from kazoo.protocol.states import ZnodeStat
from kazoo.security import ACL, Id, Permissions

_int_struct = struct.Struct("!i")
_int_int_struct = struct.Struct("!ii")
_int_int_long_struct = struct.Struct("!iiq")
_int_long_int_long_struct = struct.Struct("!iqiq")
_long_struct = struct.Struct("!q")
_multiheader_struct = struct.Struct("!iBi")
_reply_header_struct = struct.Struct("!iqi")
_stat_struct = struct.Struct("!qqqqiiiqiiq")


def _write_string(value: str | None) -> bytes:
    if value is None:
        return _int_struct.pack(-1)
    encoded = value.encode("utf-8")
    return _int_struct.pack(len(encoded)) + encoded


def _write_buffer(value: bytes | None) -> bytes:
    if value is None:
        return _int_struct.pack(-1)
    return _int_struct.pack(len(value)) + value


def _make_stat() -> ZnodeStat:
    return ZnodeStat(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)


def _make_acl_list() -> list[ACL]:
    return [ACL(1, Id("scheme", "identifier"))]


class TestUtils:
    @pytest.mark.parametrize(
        ("s",),
        [
            pytest.param("hello-\u03c0", id="non_empty_string"),
            pytest.param(None, id="none_value"),
        ],
    )
    def test_write_read_string_roundtrip(self, s: str | None) -> None:
        b = serialization._write_string(s)
        decoded, offset = serialization._read_string(b, 0)
        assert decoded == s
        assert offset == len(b)

    def test_write_read_empty_string_roundtrip(self) -> None:
        b = serialization._write_string("")
        decoded, offset = serialization._read_string(b, 0)
        assert offset == len(b)
        assert decoded is None

    @pytest.mark.parametrize(
        ("data",),
        [
            pytest.param(b"\x00\xff\x01", id="non_empty_buffer"),
            pytest.param(b"", id="empty_buffer"),
            pytest.param(None, id="none_value"),
        ],
    )
    def test_write_read_buffer_roundtrip(self, data: bytes | None) -> None:
        b = serialization._write_buffer(data)
        decoded, offset = serialization._read_buffer(b, 0)
        assert decoded == data
        assert offset == len(b)

    def test_read_acl(self) -> None:
        perms = 7
        scheme = "scheme-x"
        idv = "ident-y"
        # build bytes: perms + scheme + id
        b = (
            serialization.int_struct.pack(perms)
            + serialization._write_string(scheme)
            + serialization._write_string(idv)
        )
        acl_obj, offset = serialization._read_acl(b, 0)
        assert isinstance(acl_obj.perms, Permissions)
        assert acl_obj.perms == perms
        assert acl_obj.id.scheme == scheme
        assert acl_obj.id.id == idv
        assert offset == len(b)


class TestClose:
    def test_type(self) -> None:
        assert serialization.Close.type == -11

    def test_serialize(self) -> None:
        assert serialization.Close.serialize() == b""
        assert serialization.Close().serialize() == b""


class TestPing:
    def test_type(self) -> None:
        assert serialization.Ping.type == 11

    def test_serialize(self) -> None:
        assert serialization.Ping.serialize() == b""
        assert serialization.Ping().serialize() == b""


class TestConnect:
    def test_serialize(self) -> None:
        obj = serialization.Connect(1, 2, 3, 4, b"pwd", True)
        assert obj.serialize() == (
            _int_long_int_long_struct.pack(1, 2, 3, 4)
            + _write_buffer(b"pwd")
            + b"\x01"
        )

    def test_deserialize(self) -> None:
        data = (
            _int_int_long_struct.pack(1, 3, 4)
            + _write_buffer(b"pwd")
            + b"\x01"
        )
        decoded, offset = serialization.Connect.deserialize(data, 0)
        assert decoded == serialization.Connect(1, 0, 3, 4, b"pwd", True)
        assert offset == len(data)


class TestCreate:
    def test_type(self) -> None:
        assert serialization.Create.type == 1

    def test_serialize(self) -> None:
        obj = serialization.Create("/path", b"data", _make_acl_list(), 3)
        assert obj.serialize() == (
            _write_string("/path")
            + _write_buffer(b"data")
            + _int_struct.pack(1)
            + _int_struct.pack(1)
            + _write_string("scheme")
            + _write_string("identifier")
            + _int_struct.pack(3)
        )

    def test_deserialize(self) -> None:
        body = _write_string("/path")
        assert serialization.Create.deserialize(body, 0) == (
            "/path",
            len(body),
        )


class TestDelete:
    def test_type(self) -> None:
        assert serialization.Delete.type == 2

    def test_serialize(self) -> None:
        obj = serialization.Delete("/path", 42)
        assert obj.serialize() == _write_string("/path") + _int_struct.pack(42)

    def test_deserialize(self) -> None:
        assert serialization.Delete.deserialize(b"", 0) == (True, 0)


class TestExists:
    def test_type(self) -> None:
        assert serialization.Exists.type == 3

    def test_serialize(self) -> None:
        obj = serialization.Exists("/path", None)
        assert obj.serialize() == _write_string("/path") + b"\x00"

    def test_deserialize(self) -> None:
        response = _stat_struct.pack(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)
        assert serialization.Exists.deserialize(response, 0) == (
            _make_stat(),
            len(response),
        )


class TestGetData:
    def test_type(self) -> None:
        assert serialization.GetData.type == 4

    def test_serialize(self) -> None:
        obj = serialization.GetData("/path", None)
        assert obj.serialize() == _write_string("/path") + b"\x00"

    def test_deserialize(self) -> None:
        response = _write_buffer(b"data") + _stat_struct.pack(
            1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11
        )
        (data, result_stat), end = serialization.GetData.deserialize(
            response, 0
        )
        assert data == b"data"
        assert result_stat == _make_stat()
        assert end == len(response)


class TestSetData:
    def test_type(self) -> None:
        assert serialization.SetData.type == 5

    def test_serialize(self) -> None:
        obj = serialization.SetData("/path", b"data", 7)
        assert obj.serialize() == (
            _write_string("/path")
            + _write_buffer(b"data")
            + _int_struct.pack(7)
        )

    def test_deserialize(self) -> None:
        assert serialization.SetData.deserialize(
            _stat_struct.pack(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11),
            0,
        ) == (_make_stat(), _stat_struct.size)


class TestGetACL:
    def test_type(self) -> None:
        assert serialization.GetACL.type == 6

    def test_serialize(self) -> None:
        obj = serialization.GetACL("/path")
        assert obj.serialize() == _write_string("/path")

    def test_deserialize(self) -> None:
        body = (
            _int_struct.pack(1)
            + _int_struct.pack(1)
            + _write_string("scheme")
            + _write_string("identifier")
            + _stat_struct.pack(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)
        )
        (acls, stat), end = serialization.GetACL.deserialize(body, 0)
        assert acls == _make_acl_list()
        assert stat == _make_stat()
        assert end == len(body)


class TestSetACL:
    def test_type(self) -> None:
        assert serialization.SetACL.type == 7

    def test_serialize(self) -> None:
        obj = serialization.SetACL("/path", _make_acl_list(), 2)
        assert obj.serialize() == (
            _write_string("/path")
            + _int_struct.pack(1)
            + _int_struct.pack(1)
            + _write_string("scheme")
            + _write_string("identifier")
            + _int_struct.pack(2)
        )

    def test_deserialize(self) -> None:
        assert serialization.SetACL.deserialize(
            _stat_struct.pack(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11),
            0,
        ) == (_make_stat(), _stat_struct.size)


class TestGetChildren:
    def test_type(self) -> None:
        assert serialization.GetChildren.type == 8

    def test_serialize(self) -> None:
        obj = serialization.GetChildren("/path", None)
        assert obj.serialize() == _write_string("/path") + b"\x00"

    def test_deserialize(self) -> None:
        body = _int_struct.pack(2) + _write_string("a") + _write_string("b")
        assert serialization.GetChildren.deserialize(body, 0) == (
            ["a", "b"],
            len(body),
        )


class TestSync:
    def test_type(self) -> None:
        assert serialization.Sync.type == 9

    def test_serialize(self) -> None:
        obj = serialization.Sync("/path")
        assert obj.serialize() == _write_string("/path")

    def test_deserialize(self) -> None:
        body = _write_string("/path")
        assert serialization.Sync.deserialize(body, 0) == ("/path", len(body))


class TestGetChildren2:
    def test_type(self) -> None:
        assert serialization.GetChildren2.type == 12

    def test_serialize(self) -> None:
        obj = serialization.GetChildren2("/path", None)
        assert obj.serialize() == _write_string("/path") + b"\x00"

    def test_deserialize(self) -> None:
        body = (
            _int_struct.pack(2)
            + _write_string("a")
            + _write_string("b")
            + _stat_struct.pack(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)
        )
        (children, stat), end = serialization.GetChildren2.deserialize(body, 0)
        assert children == ["a", "b"]
        assert stat == _make_stat()
        assert end == len(body)


class TestCheckVersion:
    def test_type(self) -> None:
        assert serialization.CheckVersion.type == 13

    def test_serialize(self) -> None:
        obj = serialization.CheckVersion("/path", 99)
        assert obj.serialize() == _write_string("/path") + _int_struct.pack(99)


class TestTransaction:
    def test_type(self) -> None:
        assert serialization.Transaction.type == 14

    def test_serialize(self) -> None:
        transaction = serialization.Transaction(
            [
                serialization.Create("/path", b"data", _make_acl_list(), 3),
                serialization.Delete("/path", 1),
            ]
        )
        serialized = transaction.serialize()
        assert serialized.startswith(
            serialization.MultiHeader(
                serialization.Create.type, False, -1
            ).serialize()
        )
        assert serialized.endswith(
            serialization.MultiHeader(-1, True, -1).serialize()
        )
        assert _write_string("/path") in serialized

    def test_deserialize(self) -> None:
        response = (
            _multiheader_struct.pack(serialization.Create.type, 0, -1)
            + _write_string("/path")
            + _multiheader_struct.pack(serialization.Delete.type, 0, -1)
            + _multiheader_struct.pack(-1, 1, -1)
        )
        assert serialization.Transaction.deserialize(response, 0) == (
            ["/path", True],
            len(response),
        )

    def test_deserialize_all_types(self) -> None:
        response = (
            _multiheader_struct.pack(serialization.Create.type, 0, -1)
            + _write_string("/path")
            + _multiheader_struct.pack(serialization.Delete.type, 0, -1)
            + _multiheader_struct.pack(serialization.SetData.type, 0, -1)
            + _stat_struct.pack(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)
            + _multiheader_struct.pack(serialization.CheckVersion.type, 0, -1)
            + _multiheader_struct.pack(-1, 0, -1)
            + _int_struct.pack(-4)
            + _multiheader_struct.pack(-1, 1, -1)
        )
        result, end = serialization.Transaction.deserialize(response, 0)
        assert end == len(response)
        assert result[0] == "/path"
        assert result[1] is True
        assert result[2] == _make_stat()
        assert result[3] is True
        assert isinstance(result[4], ZookeeperError)
        assert result[4].__class__.__name__ == "ConnectionLoss"

    def test_unchroot(self) -> None:
        class DummyClient:
            def __init__(self, chroot: str) -> None:
                self.chroot = chroot

            def unchroot(self, path: str) -> str:
                if self.chroot == path:
                    return "/"
                if path.startswith(self.chroot):
                    return path[len(self.chroot) :]
                return path

        response = ["/a/b", True, "/a/c"]
        client = DummyClient("/a")
        result = serialization.Transaction.unchroot(
            client,  # type: ignore[arg-type]
            response,  # type: ignore[arg-type]
        )
        assert result == ["/b", True, "/c"]

    def test_serialize_container_and_ttl(self) -> None:
        transaction = serialization.Transaction(
            [
                serialization.CreateContainer(
                    "/cnt", b"data", _make_acl_list(), 4
                ),
                serialization.CreateTTL(
                    "/ttl", b"data", _make_acl_list(), 5, 5000
                ),
            ]
        )
        serialized = transaction.serialize()
        assert serialized.startswith(
            serialization.MultiHeader(
                serialization.CreateContainer.type, False, -1
            ).serialize()
        )
        assert serialized.endswith(
            serialization.MultiHeader(-1, True, -1).serialize()
        )
        assert _write_string("/cnt") in serialized
        assert _write_string("/ttl") in serialized

    def test_deserialize_container_and_ttl(self) -> None:
        stat_bytes = _stat_struct.pack(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)
        response = (
            _multiheader_struct.pack(serialization.CreateContainer.type, 0, -1)
            + _write_string("/cnt")
            + stat_bytes
            + _multiheader_struct.pack(serialization.CreateTTL.type, 0, -1)
            + _write_string("/ttl")
            + stat_bytes
            + _multiheader_struct.pack(-1, 0, -1)
            + _int_struct.pack(-6)
            + _multiheader_struct.pack(-1, 1, -1)
        )
        result, end = serialization.Transaction.deserialize(response, 0)
        assert end == len(response)
        assert result[0] == ("/cnt", _make_stat())
        assert result[1] == ("/ttl", _make_stat())
        assert isinstance(result[2], ZookeeperError)
        assert result[2].__class__.__name__ == "UnimplementedError"

    def test_unchroot_tuples(self) -> None:
        class DummyClient:
            def __init__(self, chroot: str) -> None:
                self.chroot = chroot

            def unchroot(self, path: str) -> str:
                if self.chroot == path:
                    return "/"
                if path.startswith(self.chroot):
                    return path[len(self.chroot) :]
                return path

        response = [("/a/cnt", _make_stat()), ("/a/ttl", _make_stat()), True]
        client = DummyClient("/a")
        result = serialization.Transaction.unchroot(
            client,  # type: ignore[arg-type]
            response,  # type: ignore[arg-type]
        )
        assert result == [("/cnt", _make_stat()), ("/ttl", _make_stat()), True]


class TestCreate2:
    def test_type(self) -> None:
        assert serialization.Create2.type == 15

    def test_serialize(self) -> None:
        obj = serialization.Create2("/path", b"data", _make_acl_list(), 4)
        assert obj.serialize() == (
            _write_string("/path")
            + _write_buffer(b"data")
            + _int_struct.pack(1)
            + _int_struct.pack(1)
            + _write_string("scheme")
            + _write_string("identifier")
            + _int_struct.pack(4)
        )

    def test_deserialize(self) -> None:
        body = _write_string("/path") + _stat_struct.pack(
            1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11
        )
        (path, stat), end = serialization.Create2.deserialize(body, 0)
        assert end == len(body)
        assert path == "/path"
        assert stat == _make_stat()


class TestReconfig:
    def test_type(self) -> None:
        assert serialization.Reconfig.type == 16

    def test_serialize(self) -> None:
        obj = serialization.Reconfig("join", "leave", "members", 37)
        assert obj.serialize() == (
            _write_string("join")
            + _write_string("leave")
            + _write_string("members")
            + _long_struct.pack(37)
        )

    def test_deserialize(self) -> None:
        body = _write_buffer(b"config-data") + _stat_struct.pack(
            1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11
        )
        (data, stat), end = serialization.Reconfig.deserialize(body, 0)
        assert end == len(body)
        assert data == b"config-data"
        assert stat == _make_stat()


class TestCreateContainer:
    def test_type(self) -> None:
        assert serialization.CreateContainer.type == 19

    def test_serialize(self) -> None:
        obj = serialization.CreateContainer(
            "/cnt", b"data", _make_acl_list(), 4
        )
        assert obj.serialize() == (
            _write_string("/cnt")
            + _write_buffer(b"data")
            + _int_struct.pack(1)
            + _int_struct.pack(1)
            + _write_string("scheme")
            + _write_string("identifier")
            + _int_struct.pack(4)
        )

    def test_serialize_none_data(self) -> None:
        obj = serialization.CreateContainer("/cnt", None, _make_acl_list(), 4)
        assert obj.serialize() == (
            _write_string("/cnt")
            + _write_buffer(None)
            + _int_struct.pack(1)
            + _int_struct.pack(1)
            + _write_string("scheme")
            + _write_string("identifier")
            + _int_struct.pack(4)
        )

    def test_deserialize(self) -> None:
        body = _write_string("/cnt") + _stat_struct.pack(
            1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11
        )
        (path, stat), end = serialization.CreateContainer.deserialize(body, 0)
        assert end == len(body)
        assert path == "/cnt"
        assert stat == _make_stat()


class TestCreateTTL:
    def test_type(self) -> None:
        assert serialization.CreateTTL.type == 21

    def test_serialize(self) -> None:
        obj = serialization.CreateTTL(
            "/ttl", b"data", _make_acl_list(), 5, 10000
        )
        assert obj.serialize() == (
            _write_string("/ttl")
            + _write_buffer(b"data")
            + _int_struct.pack(1)
            + _int_struct.pack(1)
            + _write_string("scheme")
            + _write_string("identifier")
            + _int_struct.pack(5)
            + _long_struct.pack(10000)
        )

    def test_serialize_none_data(self) -> None:
        obj = serialization.CreateTTL("/ttl", None, _make_acl_list(), 5, 10000)
        assert obj.serialize() == (
            _write_string("/ttl")
            + _write_buffer(None)
            + _int_struct.pack(1)
            + _int_struct.pack(1)
            + _write_string("scheme")
            + _write_string("identifier")
            + _int_struct.pack(5)
            + _long_struct.pack(10000)
        )

    def test_deserialize(self) -> None:
        body = _write_string("/ttl") + _stat_struct.pack(
            1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11
        )
        (path, stat), end = serialization.CreateTTL.deserialize(body, 0)
        assert end == len(body)
        assert path == "/ttl"
        assert stat == _make_stat()


class TestAuth:
    def test_type(self) -> None:
        assert serialization.Auth.type == 100

    def test_serialize(self) -> None:
        obj = serialization.Auth(1, "scheme", "auth")
        assert obj.serialize() == (
            _int_struct.pack(1)
            + _write_string("scheme")
            + _write_string("auth")
        )


class TestSASL:
    def test_type(self) -> None:
        assert serialization.SASL.type == 102

    def test_serialize(self) -> None:
        obj = serialization.SASL(b"challenge")
        assert obj.serialize() == _write_buffer(b"challenge")

    def test_deserialize(self) -> None:
        challenge, offset = serialization.SASL.deserialize(
            _write_buffer(b"challenge"), 0
        )
        assert challenge == b"challenge"
        assert offset == len(_write_buffer(b"challenge"))


class TestReplyHeader:
    def test_deserialize(self) -> None:
        reply = serialization.ReplyHeader(1, 2, 3)
        data = _reply_header_struct.pack(1, 2, 3)
        decoded_reply, offset = serialization.ReplyHeader.deserialize(data, 0)
        assert decoded_reply == reply
        assert offset == len(data)


class TestServerPacketDeserializers:
    def test_close_deserialize(self) -> None:
        assert serialization.Close.deserialize(b"", 0) == (None, 0)

    def test_ping_response_deserialize(self) -> None:
        data = b"pad" + _reply_header_struct.pack(-2, 42, 0)
        assert serialization.PingResponse.deserialize(data, 3) == (
            serialization.PingResponse(zxid=42, err=0),
            len(data),
        )

    def test_auth_response_deserialize(self) -> None:
        data = b"pad" + _reply_header_struct.pack(-4, 99, -115)
        assert serialization.AuthResponse.deserialize(data, 3) == (
            serialization.AuthResponse(zxid=99, err=-115),
            len(data),
        )

    def test_watch_packet_deserialize(self) -> None:
        data = (
            b"pad"
            + struct.Struct("!iqiii").pack(-1, 777, 0, 3, 3)
            + _write_string("/watched/node")
        )
        assert serialization.WatchPacket.deserialize(data, 3) == (
            serialization.WatchPacket(
                zxid=777,
                type=3,
                state=3,
                path="/watched/node",
            ),
            len(data),
        )


class TestMultiHeader:
    def test_serialize(self) -> None:
        header = serialization.MultiHeader(1, True, 2)
        assert header.serialize() == _multiheader_struct.pack(1, 1, 2)

    def test_deserialize(self) -> None:
        data = _multiheader_struct.pack(1, 1, 2)
        decoded_multi, offset = serialization.MultiHeader.deserialize(data, 0)
        assert decoded_multi.type == 1
        assert decoded_multi.done is True
        assert decoded_multi.err == 2
        assert offset == len(data)


class TestRemoveWatches:
    def test_type(self) -> None:
        assert serialization.RemoveWatches.type == 18

    def test_serialize(self) -> None:
        obj = serialization.RemoveWatches("/path", 3)
        assert obj.serialize() == _write_string("/path") + _int_struct.pack(3)

    def test_deserialize(self) -> None:
        assert serialization.RemoveWatches.deserialize(b"", 0) == (None, 0)


class TestAddWatch:
    def test_type(self) -> None:
        assert serialization.AddWatch.type == 106

    def test_serialize(self) -> None:
        obj = serialization.AddWatch("/path", lambda event: None, 1)
        assert obj.serialize() == _write_string("/path") + _int_struct.pack(1)

    def test_deserialize(self) -> None:
        assert serialization.AddWatch.deserialize(_int_struct.pack(0), 0) == (
            None,
            4,
        )


class TestDataclassPacketsAndFrameDecoder:
    def test_all_packets_are_slotted_dataclasses(self) -> None:
        from dataclasses import is_dataclass

        packets = [
            serialization.Close(),
            serialization.Ping(),
            serialization.Connect(0, 0, 10000, 0, b"", False),
            serialization.Create("/p", b"", _make_acl_list(), 0),
            serialization.Delete("/p", -1),
            serialization.Exists("/p", None),
            serialization.GetData("/p", None),
            serialization.SetData("/p", b"", -1),
            serialization.GetACL("/p"),
            serialization.SetACL("/p", _make_acl_list(), -1),
            serialization.GetChildren("/p", None),
            serialization.Sync("/p"),
            serialization.GetChildren2("/p", None),
            serialization.CheckVersion("/p", 0),
            serialization.Create2("/p", b"", _make_acl_list(), 0),
            serialization.CreateContainer("/p", b"", _make_acl_list(), 4),
            serialization.CreateTTL("/p", b"", _make_acl_list(), 5, 1000),
            serialization.Transaction([]),
            serialization.Reconfig(None, None, "m", -1),
            serialization.RemoveWatches("/p", 1),
            serialization.Auth(0, "digest", "u:p"),
            serialization.SASL(b"tok"),
            serialization.AddWatch("/p", lambda ev: None, 0),
            serialization.ReplyHeader(1, 2, 0),
            serialization.MultiHeader(1, False, -1),
            serialization.PingResponse(10),
            serialization.AuthResponse(10),
            serialization.WatchPacket(10, 1, 3, "/p"),
            serialization.SASLResponse(1, 10, 0, b"tok"),
            serialization.ResponsePacket(
                serialization.ReplyHeader(1, 2, 0),
                serialization.Delete("/p", -1),
                None,
                True,
                None,
            ),
        ]
        for pkt in packets:
            assert is_dataclass(pkt)
            assert hasattr(type(pkt), "__slots__")
            assert not hasattr(pkt, "__dict__")

    def test_frame_decoder_encode_and_pop_watch_packet(self) -> None:
        decoder = serialization.FrameDecoder()
        encoded_ping = serialization.FrameDecoder.encode_request(
            serialization.Ping(), xid=serialization.ReservedXID.PING
        )
        assert encoded_ping == _int_struct.pack(8) + _int_int_struct.pack(
            serialization.ReservedXID.PING, 11
        )

        # Build a XID.WATCH (-1) server frame
        watch_payload = (
            _reply_header_struct.pack(serialization.ReservedXID.WATCH, 555, 0)
            + _int_int_struct.pack(3, 3)
            + _write_string("/watched")
        )
        decoder.feed(_int_struct.pack(len(watch_payload)) + watch_payload)
        pkt = decoder.pop_packet()
        assert pkt == serialization.WatchPacket(
            zxid=555,
            type=3,
            state=3,
            path="/watched",
        )

    def test_frame_decoder_buffer_resets_when_drained(self) -> None:
        decoder = serialization.FrameDecoder()
        f_ping = _reply_header_struct.pack(
            serialization.ReservedXID.PING, 10, 0
        )
        frame_wire = _int_struct.pack(len(f_ping)) + f_ping

        # Feed and pop far more than one buffer's worth of frames
        for _ in range(4000):
            decoder.feed(frame_wire)
            p = decoder.pop_packet()
            assert p == serialization.PingResponse(zxid=10, err=0)

        # A fully consumed fixed buffer resets cursors without reallocating
        assert decoder._head == 0
        assert decoder._tail == 0
        assert decoder._scan == 0
        assert len(decoder._buf) == decoder._capacity
        assert decoder.frame_ready is False

    def test_frame_decoder_large_payload_getdata(self) -> None:
        pending: collections.deque[tuple[object, object, int]] = (
            collections.deque([(serialization.GetData("/k", None), None, 1)])
        )
        decoder = serialization.FrameDecoder(pop_pending=pending.popleft)
        payload_max = b"x" * serialization.JUTE_MAX_BUFFER
        stat_bytes = serialization.stat_struct.pack(
            1, 2, 1000, 2000, 1, 0, 0, 0, len(payload_max), 0, 10
        )
        body = (
            _reply_header_struct.pack(1, 100, 0)
            + _write_buffer(payload_max)
            + stat_bytes
        )
        wire = _int_struct.pack(len(body)) + body

        pos = 0
        while pos < len(wire):
            buf = decoder.get_buffer()
            take = min(len(buf), len(wire) - pos)
            buf[:take] = wire[pos : pos + take]
            decoder.buffer_updated(take)
            pos += take
        pkt = decoder.pop_packet()
        assert isinstance(pkt, serialization.ResponsePacket)
        assert pkt.header.xid == 1
        assert pkt.header.zxid == 100
        assert pkt.error is None
        data, stat = pkt.response
        assert data == payload_max
        assert isinstance(data, bytes)
        assert stat.version == 1

    def test_frame_decoder_releases_view_between_replies(self) -> None:
        pending: collections.deque[tuple[object, object, int]] = (
            collections.deque(
                [
                    (serialization.Delete("/1", -1), None, 1),
                    (serialization.Delete("/2", -1), None, 2),
                ]
            )
        )
        decoder = serialization.FrameDecoder(pop_pending=pending.popleft)
        f1 = _reply_header_struct.pack(1, 100, 0)
        wire1 = _int_struct.pack(len(f1)) + f1
        f2 = _reply_header_struct.pack(2, 101, 0)
        wire2 = _int_struct.pack(len(f2)) + f2

        decoder.feed(wire1)
        p1 = decoder.pop_packet()
        assert isinstance(p1, serialization.ResponsePacket)
        assert p1.response is True

        decoder.feed(wire2)
        p2 = decoder.pop_packet()
        assert isinstance(p2, serialization.ResponsePacket)
        assert p1.header.xid == 1
        assert p2.header.xid == 2

    def test_frame_decoder_differential_against_decode_packet(self) -> None:
        stat_b = _stat_struct.pack(*range(11))

        def make_getdata(xid: int, n: int, rng: random.Random) -> bytes:
            return (
                _reply_header_struct.pack(xid, xid * 7, 0)
                + _int_struct.pack(n)
                + rng.randbytes(n)
                + stat_b
            )

        def make_watch(path: str) -> bytes:
            p = path.encode()
            return (
                _reply_header_struct.pack(-1, 9, 0)
                + _int_struct.pack(1)
                + _int_struct.pack(3)
                + _int_struct.pack(len(p))
                + p
            )

        def make_connect(read_only: bool | None) -> bytes:
            b = _int_int_long_struct.pack(0, 30_000, 12345) + _write_buffer(
                b"secret1234567890"
            )
            if read_only is not None:
                b += b"\x01" if read_only else b"\x00"
            return b

        def make_bodies(
            rng: random.Random,
            count: int,
            sizes: list[int],
            max_payload: int,
        ) -> tuple[list[bytes], list[tuple[object, object, int]]]:
            out: list[bytes] = []
            reqs: list[tuple[object, object, int]] = []
            xid_seq = 0
            for _ in range(count):
                k = rng.random()
                if k < 0.05:
                    out.append(make_connect(rng.choice([None, False, True])))
                elif k < 0.12:
                    out.append(_reply_header_struct.pack(-2, 5, 0))
                elif k < 0.17:
                    out.append(_reply_header_struct.pack(-4, 6, 0))
                elif k < 0.25:
                    out.append(make_watch("/a/" + "b" * rng.randrange(40)))
                elif k < 0.32:
                    xid_seq += 1
                    out.append(_reply_header_struct.pack(xid_seq, 1, 0))
                    reqs.append(
                        (serialization.Delete("/d", -1), None, xid_seq)
                    )
                elif k < 0.38:
                    xid_seq += 1
                    out.append(_reply_header_struct.pack(xid_seq, 1, -101))
                    reqs.append(
                        (serialization.Exists("/e", None), None, xid_seq)
                    )
                else:
                    xid_seq += 1
                    size = min(
                        rng.choice(sizes),
                        max(0, max_payload - serialization.REPLY_OVERHEAD),
                    )
                    out.append(
                        make_getdata(xid_seq, rng.randrange(size + 1), rng)
                    )
                    reqs.append(
                        (serialization.GetData("/g", None), None, xid_seq)
                    )
            return out, reqs

        def reference_decode_stream(
            stream: bytes,
            reqs: list[tuple[object, object, int]],
        ) -> list[object]:
            pos = 0
            req_idx = 0
            out: list[object] = []
            while pos < len(stream):
                (msg_len,) = _int_struct.unpack_from(stream, pos)
                pos += 4
                frame = stream[pos : pos + msg_len]
                pos += msg_len
                xid, zxid, err = _reply_header_struct.unpack_from(frame, 0)
                if xid == 0:
                    pkt: serialization.ServerPacket = (
                        serialization.Connect.deserialize(frame, 0)[0]
                    )
                elif xid == -1:
                    pkt = serialization.WatchPacket.deserialize(frame, 0)[0]
                elif xid == -2:
                    pkt = serialization.PingResponse.deserialize(frame, 0)[0]
                elif xid == -4:
                    pkt = serialization.AuthResponse.deserialize(frame, 0)[0]
                else:
                    req, _, exp_xid = reqs[req_idx]
                    req_idx += 1
                    assert xid == exp_xid
                    resp = None if err else req.deserialize(frame, 16)[0]
                    pkt = serialization.ResponsePacket(
                        header=serialization.ReplyHeader(xid, zxid, err),
                        request=req,
                        async_object=None,
                        response=resp,
                    )
                out.append(norm(pkt))
            return out

        def norm(pkt: serialization.ServerPacket) -> object:
            if isinstance(pkt, serialization.ResponsePacket):
                return (
                    "response",
                    pkt.header,
                    pkt.request,
                    pkt.response,
                    type(pkt.error) if pkt.error is not None else None,
                )
            return pkt

        def assert_invariants(dec: serialization.FrameDecoder) -> None:
            assert dec._tail <= dec._capacity
            pos = dec._head
            while pos < dec._tail:
                assert pos < serialization.READ_WINDOW
                if dec._tail - pos < 4:
                    break
                msg_len = _int_struct.unpack_from(dec._buf, pos)[0]
                if pos + 4 + msg_len > dec._tail:
                    break
                pos += 4 + msg_len

        def drain(
            dec: serialization.FrameDecoder,
            out: list[object],
            rng: random.Random,
            *,
            always: bool,
        ) -> None:
            while always or rng.random() < 0.7:
                pkt = dec.pop_packet()
                if pkt is None:
                    return
                out.append(norm(pkt))

        def via_recv_into(
            dec: serialization.FrameDecoder,
            stream: bytes,
            rng: random.Random,
            *,
            lazy_pop: bool,
        ) -> list[object]:
            out: list[object] = []
            pos = 0
            src = memoryview(stream)
            main = dec._buf
            while pos < len(src):
                if not lazy_pop:
                    assert not dec.frame_ready
                try:
                    buf = dec.get_buffer(4096)
                except BufferError:
                    assert lazy_pop and dec.frame_ready
                    drain(dec, out, rng, always=True)
                    continue
                take = min(
                    len(buf),
                    len(src) - pos,
                    rng.choice([1, 3, 100, 5000, 1 << 22]),
                )
                buf[:take] = src[pos : pos + take]
                dec.buffer_updated(take)
                pos += take
                assert dec._buf is main and len(main) == dec._capacity
                assert_invariants(dec)
                drain(dec, out, rng, always=not lazy_pop)
            drain(dec, out, rng, always=True)
            assert not dec.frame_ready
            assert dec._head == 0 and dec._tail == 0 and dec._scan == 0
            return out

        def via_feed(
            dec: serialization.FrameDecoder,
            stream: bytes,
            rng: random.Random,
            *,
            lazy_pop: bool,
        ) -> list[object]:
            out: list[object] = []
            pos = 0
            while pos < len(stream):
                avail = dec._tail - dec._scan
                if dec._scan >= serialization.READ_WINDOW:
                    drain(dec, out, rng, always=True)
                    continue
                if avail >= 4:
                    msg_len = _int_struct.unpack_from(dec._buf, dec._scan)[0]
                    max_take = max(
                        serialization.READ_WINDOW - dec._tail,
                        dec._scan + 4 + msg_len - dec._tail,
                    )
                else:
                    max_take = max(
                        serialization.READ_WINDOW - dec._tail,
                        dec._scan + 4 - dec._tail,
                    )
                if max_take <= 0:
                    drain(dec, out, rng, always=True)
                    continue
                n = min(rng.choice([1, 7, 4096, 65536, 262144]), max_take)
                dec.feed(stream[pos : pos + n])
                pos += n
                assert_invariants(dec)
                drain(dec, out, rng, always=not lazy_pop)
            drain(dec, out, rng, always=True)
            assert len(dec._buf) == dec._capacity
            return out

        configs = [
            (0, [0, 1, 30, 1000], 30),
            (
                300_000,
                [0, 30, 16000, 70000, 299_900, 300_000],
                30,
            ),
            (
                serialization.JUTE_MAX_BUFFER,
                [0, 30, 1000, 70000, 300000, serialization.JUTE_MAX_BUFFER],
                30,
            ),
            # High-count stream that repeatedly crosses READ_WINDOW
            (4096, [0, 16, 200, 1000, 4096], 600),
        ]
        saw_straddle_L = False
        for max_buffer, sizes, max_count in configs:
            seeds = 15 if max_buffer == serialization.JUTE_MAX_BUFFER else 40
            if max_count > 100:
                seeds = 8
            max_payload = max_buffer + serialization.REPLY_OVERHEAD
            for seed in range(seeds):
                rng = random.Random(seed)
                count = (
                    rng.randrange(300, max_count)
                    if max_count > 100
                    else rng.randrange(1, max_count)
                )
                bodies, reqs = make_bodies(rng, count, sizes, max_payload)
                stream = b"".join(_int_struct.pack(len(b)) + b for b in bodies)
                want = reference_decode_stream(stream, reqs)
                # Detect streams with a frame straddling READ_WINDOW
                off = 0
                for b in bodies:
                    end = off + 4 + len(b)
                    if off < serialization.READ_WINDOW < end:
                        saw_straddle_L = True
                    off = end
                for lazy in (False, True):
                    q1 = collections.deque(reqs)
                    dec = serialization.FrameDecoder(
                        max_buffer=max_buffer, pop_pending=q1.popleft
                    )
                    assert (
                        via_recv_into(dec, stream, rng, lazy_pop=lazy) == want
                    )
                    dec.reset()
                    q1.extend(reqs)
                    assert (
                        via_recv_into(dec, stream, rng, lazy_pop=lazy) == want
                    )
                    q2 = collections.deque(reqs)
                    dec_feed = serialization.FrameDecoder(
                        max_buffer=max_buffer, pop_pending=q2.popleft
                    )
                    assert (
                        via_feed(dec_feed, stream, rng, lazy_pop=lazy) == want
                    )
        assert saw_straddle_L is True

    def test_frame_decoder_boundary_splits_exact_fit_and_oversize(
        self,
    ) -> None:
        pending: collections.deque[tuple[object, object, int]] = (
            collections.deque(
                [
                    (serialization.Delete("/11", -1), None, 11),
                    (serialization.GetData("/12", None), None, 12),
                    (serialization.GetData("/13", None), None, 13),
                    (serialization.Delete("/14", -1), None, 14),
                ]
            )
        )
        dec = serialization.FrameDecoder(
            max_buffer=256, pop_pending=pending.popleft
        )
        assert (
            dec._capacity
            == serialization.READ_WINDOW
            + 4
            + 256
            + serialization.REPLY_OVERHEAD
        )

        # Split 4-byte frame header across 1-byte recv_into calls
        body = _reply_header_struct.pack(11, 22, 0)
        wire = _int_struct.pack(len(body)) + body
        for byte in wire:
            buf = dec.get_buffer()
            buf[0] = byte
            dec.buffer_updated(1)
        pkt = dec.pop_packet()
        assert isinstance(pkt, serialization.ResponsePacket)
        assert pkt.header == serialization.ReplyHeader(xid=11, zxid=22, err=0)
        assert pkt.response is True

        # Exact fit (n == max_buffer + REPLY_OVERHEAD) succeeds
        exact_n = 256 + serialization.REPLY_OVERHEAD
        stat_b = _stat_struct.pack(1, 2, 3, 4, 1, 0, 0, 0, 256, 0, 1)
        hdr_b = _reply_header_struct.pack(12, 23, 0)
        pad_len = exact_n - len(hdr_b) - 4 - len(stat_b)
        assert pad_len == 256
        exact_body = hdr_b + _write_buffer(b"a" * pad_len) + stat_b
        assert len(exact_body) == exact_n
        dec.feed(_int_struct.pack(len(exact_body)) + exact_body)
        assert dec.frame_ready is True
        exact_pkt = dec.pop_packet()
        assert isinstance(exact_pkt, serialization.ResponsePacket)
        data, _ = exact_pkt.response
        assert data == b"a" * pad_len

        # Oversize (n == max_buffer + REPLY_OVERHEAD + 1) reports
        # ReplyTooLargeError and skips remaining bytes across reads
        over_n = exact_n + 1
        over_hdr = _reply_header_struct.pack(13, 24, 0)
        over_body = over_hdr + b"b" * (over_n - len(over_hdr))
        next_body = _reply_header_struct.pack(14, 25, 0)
        over_wire = _int_struct.pack(len(over_body)) + over_body
        next_wire = _int_struct.pack(len(next_body)) + next_body

        # Feed just 20 bytes (4B len + 16B ReplyHeader) first
        dec.feed(over_wire[:20])
        assert dec.frame_ready is True
        over_pkt = dec.pop_packet()
        assert isinstance(over_pkt, serialization.ResponsePacket)
        assert isinstance(over_pkt.error, ReplyTooLargeError)
        assert "max_buffer" in str(over_pkt.error)
        assert over_pkt.header == serialization.ReplyHeader(
            xid=13, zxid=24, err=0
        )

        # Skip the rest across multiple chunks, then decode next_wire
        rest = over_wire[20:] + next_wire
        dec.feed(rest[:100])
        assert dec.pop_packet() is None
        dec.feed(rest[100:])
        next_pkt = dec.pop_packet()
        assert isinstance(next_pkt, serialization.ResponsePacket)
        assert next_pkt.header == serialization.ReplyHeader(
            xid=14, zxid=25, err=0
        )

    def test_frame_decoder_frame_length_does_not_outlive_its_frame(
        self,
    ) -> None:
        pending: collections.deque[tuple[object, object, int]] = (
            collections.deque(
                [
                    (serialization.Delete("/1", -1), None, 1),
                    (serialization.Delete("/2", -1), None, 2),
                    (serialization.Delete("/2b", -1), None, 2),
                ]
            )
        )
        dec = serialization.FrameDecoder(
            max_buffer=256, pop_pending=pending.popleft
        )

        def reply_wire(xid: int, size: int) -> bytes:
            body = _reply_header_struct.pack(xid, xid, 0)
            return _int_struct.pack(size) + body + b"\x00" * (size - len(body))

        def recv(data: bytes) -> None:
            buf = dec.get_buffer()
            buf[: len(data)] = data
            dec.buffer_updated(len(data))

        small = reply_wire(2, 16)

        # A large valid frame's length must not be applied to the next frame
        large = reply_wire(1, 256 + serialization.REPLY_OVERHEAD)
        recv(large[:4])
        recv(large[4:])
        pkt = dec.pop_packet()
        assert isinstance(pkt, serialization.ResponsePacket)
        assert pkt.header.xid == 1
        recv(small)
        pkt = dec.pop_packet()
        assert isinstance(pkt, serialization.ResponsePacket)
        assert pkt.header.xid == 2

        # Neither must the length of a frame abandoned by reset()
        recv(reply_wire(3, 200)[:14])
        assert dec.frame_ready is False
        dec.reset()
        recv(small)
        pkt = dec.pop_packet()
        assert isinstance(pkt, serialization.ResponsePacket)
        assert pkt.header.xid == 2
        assert dec.frame_ready is False

    def test_frame_decoder_feed_oversize_from_non_byte_view(self) -> None:
        pending: collections.deque[tuple[object, object, int]] = (
            collections.deque(
                [
                    (serialization.GetData("/1", None), None, 1),
                    (serialization.Delete("/2", -1), None, 2),
                ]
            )
        )
        dec = serialization.FrameDecoder(
            max_buffer=256, pop_pending=pending.popleft
        )
        hdr = _reply_header_struct.pack(1, 1, 0)
        n = serialization.READ_WINDOW + 100
        payload = bytes(i % 251 for i in range(n - len(hdr)))
        small = _reply_header_struct.pack(2, 2, 0)
        wire = (
            _int_struct.pack(n)
            + hdr
            + payload
            + _int_struct.pack(len(small))
            + small
        )

        view = memoryview(wire).cast("b")
        dec.feed(view[:1000])
        assert dec.frame_ready is True
        pkt = dec.pop_packet()
        assert isinstance(pkt, serialization.ResponsePacket)
        assert isinstance(pkt.error, ReplyTooLargeError)
        assert pkt.header.xid == 1

        # Feed remaining bytes (more than READ_WINDOW) in chunks
        pos = 1000
        while pos < len(view):
            chunk = view[pos : pos + 100_000]
            dec.feed(chunk)
            pos += len(chunk)

        pkt2 = dec.pop_packet()
        assert isinstance(pkt2, serialization.ResponsePacket)
        assert pkt2.header.xid == 2
        assert dec.pop_packet() is None

    def test_frame_decoder_move_rule_and_window_sizing(self) -> None:
        dec = serialization.FrameDecoder()
        L = serialization.READ_WINDOW

        # Small partial frame near L (<= MOVE_MAX) is moved to 0
        dec._head = 262_000
        dec._scan = 262_000
        dec._tail = L
        dec._buf[262_000:262_004] = _int_struct.pack(188)
        dec._frame_len = 188
        win = dec.get_buffer()
        assert dec._head == 0
        assert dec._scan == 0
        assert dec._tail == L - 262_000
        assert len(win) == L - dec._tail

        # Large partial frame near L is not moved; window ends at frame end
        dec.reset()
        dec._head = 249_600
        dec._scan = 249_600
        dec._tail = L
        full_n = 1_000_088
        dec._buf[249_600:249_604] = _int_struct.pack(full_n)
        dec._frame_len = full_n
        win = dec.get_buffer()
        assert dec._head == 249_600
        assert len(win) == 249_600 + 4 + full_n - L

        # Boundary at MOVE_MAX: p == MOVE_MAX moves, p == MOVE_MAX + 1 does not
        dec.reset()
        dec._head = L - serialization.MOVE_MAX
        dec._scan = L - serialization.MOVE_MAX
        dec._tail = L
        dec._buf[dec._head : dec._head + 4] = _int_struct.pack(
            serialization.MOVE_MAX + 100
        )
        dec._frame_len = serialization.MOVE_MAX + 100
        win = dec.get_buffer()
        assert dec._head == 0
        assert dec._tail == serialization.MOVE_MAX
        assert len(win) == L - serialization.MOVE_MAX

        dec.reset()
        p_over = serialization.MOVE_MAX + 1
        dec._head = L - p_over
        dec._scan = L - p_over
        dec._tail = L
        dec._buf[dec._head : dec._head + 4] = _int_struct.pack(p_over + 100)
        dec._frame_len = p_over + 100
        win = dec.get_buffer()
        assert dec._head == L - p_over
        assert len(win) == 104

    def test_frame_decoder_boundaries_and_oversize_errors(self) -> None:
        from unittest.mock import Mock

        # 1) msg_len 15 rejected, 16 accepted
        dec = serialization.FrameDecoder(max_buffer=0)
        with pytest.raises(ConnectionDropped, match="Invalid frame length"):
            dec.feed(_int_struct.pack(15))
        dec.reset()

        pending: collections.deque[tuple[object, object, int]] = (
            collections.deque([(serialization.Delete("/p", -1), None, 1)])
        )
        dec = serialization.FrameDecoder(
            max_buffer=0, pop_pending=pending.popleft
        )
        dec.feed(_int_struct.pack(16) + _reply_header_struct.pack(1, 1, 0))
        pkt16 = dec.pop_packet()
        assert isinstance(pkt16, serialization.ResponsePacket)
        assert pkt16.response is True

        # 2) max_buffer + 88 accepted, max_buffer + 89 rejected
        exact_n = 0 + serialization.REPLY_OVERHEAD  # 88
        stat_b = _stat_struct.pack(*range(11))
        exact_body = (
            _reply_header_struct.pack(2, 2, 0) + _write_buffer(b"") + stat_b
        )
        assert len(exact_body) == exact_n
        pending.append((serialization.GetData("/p", None), None, 2))
        dec.feed(_int_struct.pack(exact_n) + exact_body)
        pkt88 = dec.pop_packet()
        assert isinstance(pkt88, serialization.ResponsePacket)
        assert pkt88.error is None
        assert pkt88.response[0] == b""

        over_n = exact_n + 1  # 89
        over_body = _reply_header_struct.pack(3, 3, 0) + b"x" * (over_n - 16)
        next_body = _reply_header_struct.pack(4, 4, 0)
        pending.append((serialization.GetData("/p", None), None, 3))
        pending.append((serialization.Delete("/p", -1), None, 4))
        # Deliver oversize inside READ_WINDOW + next frame in one read
        dec.feed(
            _int_struct.pack(over_n)
            + over_body
            + _int_struct.pack(16)
            + next_body
        )
        pkt_over = dec.pop_packet()
        assert isinstance(pkt_over, serialization.ResponsePacket)
        assert isinstance(pkt_over.error, ReplyTooLargeError)
        assert "max_buffer" in str(pkt_over.error)
        assert dec._skip == 0
        pkt_next = dec.pop_packet()
        assert isinstance(pkt_next, serialization.ResponsePacket)
        assert pkt_next.header.xid == 4

        # 3) Oversize with xid mismatch fails async_object before raising
        async_obj = Mock()
        pending.append((serialization.GetData("/p", None), async_obj, 99))
        bad_xid_wire = (
            _int_struct.pack(over_n)
            + _reply_header_struct.pack(5, 5, 0)
            + b"x" * (over_n - 16)
        )
        dec.feed(bad_xid_wire[:24])
        with pytest.raises(
            RuntimeError, match="^xids do not match, expected 99 received 5$"
        ):
            dec.pop_packet()
        async_obj.set_exception.assert_called_once()

        # 4) Oversize with reserved or negative xids -> ConnectionDropped
        for bad_xid in (0, -1, -2, -4, -99):
            dec.reset()
            wire = (
                _int_struct.pack(over_n)
                + _reply_header_struct.pack(bad_xid, 1, 0)
                + b"x" * (over_n - 16)
            )
            with pytest.raises(ConnectionDropped, match="Oversize frame"):
                dec.feed(wire[:20])
            dec.reset()
            # Split after 4 bytes so xid arrives in second read
            dec.feed(wire[:4])
            with pytest.raises(ConnectionDropped, match="Oversize frame"):
                dec.feed(wire[4:20])

    def test_frame_decoder_dispatch_errors_and_connect_readonly(self) -> None:
        from unittest.mock import Mock

        pending: collections.deque[tuple[object, object, int]] = (
            collections.deque()
        )
        dec = serialization.FrameDecoder(pop_pending=pending.popleft)

        # Connect with readOnly=True, readOnly=False, and without readOnly
        for ro_bytes, expected_ro in (
            (b"\x01", True),
            (b"\x00", False),
            (b"", False),
        ):
            body = (
                _int_int_long_struct.pack(0, 30_000, 42)
                + _write_buffer(b"")
                + ro_bytes
            )
            dec.feed(_int_struct.pack(len(body)) + body)
            pkt = dec.pop_packet()
            assert isinstance(pkt, serialization.Connect)
            assert pkt.session_id == 42
            assert pkt.read_only is expected_ro

        # Corrupt 16-byte Connect response -> ConnectionDropped
        dec.feed(_int_struct.pack(16) + _int_int_long_struct.pack(0, 1, 2))
        with pytest.raises(
            ConnectionDropped, match="invalid server connect response"
        ):
            dec.pop_packet()

        # Unknown negative xid -> ConnectionDropped
        for unknown_xid in (-3, -99):
            body = _reply_header_struct.pack(unknown_xid, 1, 0)
            dec.feed(_int_struct.pack(len(body)) + body)
            with pytest.raises(
                ConnectionDropped, match="Unknown reserved xid"
            ):
                dec.pop_packet()

        # Empty pending on positive xid -> ConnectionDropped
        body_pos = _reply_header_struct.pack(1, 1, 0)
        dec.feed(_int_struct.pack(len(body_pos)) + body_pos)
        with pytest.raises(ConnectionDropped, match="empty pending"):
            dec.pop_packet()

        # pop_pending=None on positive xid -> ConnectionDropped
        dec_no_pending = serialization.FrameDecoder()
        dec_no_pending.feed(_int_struct.pack(len(body_pos)) + body_pos)
        with pytest.raises(ConnectionDropped, match="no pending queue"):
            dec_no_pending.pop_packet()

        # xid mismatch -> async_object failed before RuntimeError raised
        async_obj = Mock()
        pending.append((serialization.Delete("/p", -1), async_obj, 10))
        dec.feed(_int_struct.pack(16) + _reply_header_struct.pack(11, 5, 0))
        with pytest.raises(
            RuntimeError, match="^xids do not match, expected 10 received 11$"
        ):
            dec.pop_packet()
        async_obj.set_exception.assert_called_once()

        # Deserialize failure -> ResponsePacket.error set, next frame decodes
        a_err = Mock()
        a_ok = Mock()
        pending.append((serialization.GetData("/bad", None), a_err, 20))
        pending.append((serialization.Delete("/ok", -1), a_ok, 21))
        bad_getdata = _reply_header_struct.pack(20, 100, 0)  # missing body
        good_del = _reply_header_struct.pack(21, 101, 0)
        dec.feed(
            _int_struct.pack(len(bad_getdata))
            + bad_getdata
            + _int_struct.pack(len(good_del))
            + good_del
        )
        p_err = dec.pop_packet()
        assert isinstance(p_err, serialization.ResponsePacket)
        assert isinstance(p_err.error, struct.error)
        assert p_err.response is None
        assert p_err.async_object is a_err
        p_ok = dec.pop_packet()
        assert isinstance(p_ok, serialization.ResponsePacket)
        assert p_ok.error is None
        assert p_ok.response is True
        assert p_ok.async_object is a_ok

    def test_frame_decoder_buffer_identity_and_reset(self) -> None:
        pending: collections.deque[tuple[object, object, int]] = (
            collections.deque()
        )
        dec = serialization.FrameDecoder(pop_pending=pending.popleft)
        buf_id = id(dec._buf)

        def make_reply_wire(xid: int) -> bytes:
            b = _reply_header_struct.pack(xid, xid, 0)
            return _int_struct.pack(len(b)) + b

        for trigger in ("pop", "get_buffer", "feed", "reset"):
            dec.reset()
            pending.append((serialization.Delete("/p", -1), None, 1))
            dec.feed(make_reply_wire(1))
            pkt = dec.pop_packet()
            assert isinstance(pkt, serialization.ResponsePacket)
            if trigger == "pop":
                assert dec.pop_packet() is None
            elif trigger == "get_buffer":
                dec.get_buffer()
            elif trigger == "feed":
                dec.feed(b"")
            else:
                dec.reset()
            assert id(dec._buf) == buf_id

        ping_b = _reply_header_struct.pack(
            serialization.ReservedXID.PING, 1, 0
        )
        dec.feed(_int_struct.pack(len(ping_b)) + ping_b)
        assert isinstance(dec.pop_packet(), serialization.PingResponse)
        assert id(dec._buf) == buf_id

    def test_frame_decoder_short_or_negative_length_rejected(self) -> None:
        for bad_len in (-1, 0, 15):
            bad_hdr = _int_struct.pack(bad_len)
            d1 = serialization.FrameDecoder()
            with pytest.raises(
                ConnectionDropped, match="Invalid frame length"
            ):
                d1.feed(bad_hdr)

            d2 = serialization.FrameDecoder()
            mv = d2.get_buffer()
            mv[:4] = bad_hdr
            with pytest.raises(
                ConnectionDropped, match="Invalid frame length"
            ):
                d2.buffer_updated(4)

    def test_frame_decoder_coalesced_connect_sasl_reply_transitions(
        self,
    ) -> None:
        pending: collections.deque[tuple[object, object, int]] = (
            collections.deque(
                [
                    (serialization.SASL(b"t"), None, 1),
                    (serialization.Delete("/p", -1), None, 2),
                ]
            )
        )
        dec = serialization.FrameDecoder(pop_pending=pending.popleft)
        conn_body = (
            _int_int_long_struct.pack(0, 30_000, 999)
            + _write_buffer(b"secret1234567890")
            + b"\x00"
        )
        sasl_body = _reply_header_struct.pack(1, 10, 0) + _write_buffer(
            b"challenge"
        )
        rep_body = _reply_header_struct.pack(2, 11, 0)
        coalesced = (
            _int_struct.pack(len(conn_body))
            + conn_body
            + _int_struct.pack(len(sasl_body))
            + sasl_body
            + _int_struct.pack(len(rep_body))
            + rep_body
        )

        mv = dec.get_buffer()
        mv[: len(coalesced)] = coalesced
        dec.buffer_updated(len(coalesced))

        p1 = dec.pop_packet()
        assert isinstance(p1, serialization.Connect)
        assert p1.session_id == 999

        p2 = dec.pop_packet()
        assert isinstance(p2, serialization.SASLResponse)
        assert p2.challenge == b"challenge"

        p3 = dec.pop_packet()
        assert isinstance(p3, serialization.ResponsePacket)
        assert p3.header.xid == 2
        assert dec.frame_ready is False

    def test_frame_decoder_sizehint_and_buffer_updated_edges(self) -> None:
        dec = serialization.FrameDecoder(max_buffer=256)
        buf = dec.get_buffer(8192)
        assert len(buf) == serialization.READ_WINDOW

        # buffer_updated(0) is a no-op
        dec.buffer_updated(0)
        assert dec._tail == 0

        with pytest.raises(ValueError, match="non-negative"):
            dec.buffer_updated(-1)

        with pytest.raises(ValueError, match="main frame buffer capacity"):
            dec.buffer_updated(dec._capacity + 1)

    def test_frame_decoder_get_buffer_raises_when_full_unpopped(self) -> None:
        # 1) Complete frames filling up to READ_WINDOW
        pending: collections.deque[tuple[object, object, int]] = (
            collections.deque()
        )
        dec = serialization.FrameDecoder(
            max_buffer=256, pop_pending=pending.popleft
        )
        hdr = _reply_header_struct.pack(1, 1, 0)
        n = 256
        frame = _int_struct.pack(n) + hdr + b"\x00" * (n - len(hdr))
        # Feed until READ_WINDOW is reached (frame size 260)
        while dec._tail + len(frame) <= serialization.READ_WINDOW:
            pending.append((serialization.Delete("/p", -1), None, 1))
            dec.feed(frame)
        # Fill past READ_WINDOW with another frame straddling L
        rem = serialization.READ_WINDOW - dec._tail
        if rem > 0:
            pending.append((serialization.Delete("/p", -1), None, 2))
            dec.feed(
                _int_struct.pack(256)
                + _reply_header_struct.pack(2, 2, 0)
                + b"\x00" * (256 - len(hdr))
            )
        assert dec.frame_ready is True
        with pytest.raises(BufferError, match="pop frames"):
            dec.get_buffer()
        while dec.pop_packet() is not None:
            pass

        # 2) Oversize frame with >= 20 bytes buffered must be popped first
        pending.append((serialization.GetData("/p", None), None, 1))
        over_n = 256 + serialization.REPLY_OVERHEAD + 50
        over_wire = (
            _int_struct.pack(over_n) + hdr + b"\x00" * (over_n - len(hdr))
        )
        dec.feed(over_wire[:40])
        assert dec.frame_ready is True
        with pytest.raises(BufferError, match="pop frames"):
            dec.get_buffer()
        pkt = dec.pop_packet()
        assert isinstance(pkt, serialization.ResponsePacket)
        assert isinstance(pkt.error, ReplyTooLargeError)

    def test_frame_decoder_oversize_waits_for_header(self) -> None:
        pending: collections.deque[tuple[object, object, int]] = (
            collections.deque([(serialization.GetData("/p", None), None, 7)])
        )
        dec = serialization.FrameDecoder(
            max_buffer=256, pop_pending=pending.popleft
        )
        over_n = 256 + serialization.REPLY_OVERHEAD + 32
        buf = dec.get_buffer()
        buf[:4] = _int_struct.pack(over_n)
        dec.buffer_updated(4)
        assert dec.frame_ready is False

        buf2 = dec.get_buffer()
        hdr = _reply_header_struct.pack(7, 8, 0)
        buf2[: len(hdr)] = hdr
        dec.buffer_updated(len(hdr))
        assert dec.frame_ready is True
        pkt = dec.pop_packet()
        assert isinstance(pkt, serialization.ResponsePacket)
        assert isinstance(pkt.error, ReplyTooLargeError)
        assert dec._skip == over_n - len(hdr)

    def test_frame_decoder_pop_packet_decode_error_releases_view(
        self,
    ) -> None:
        pending: collections.deque[tuple[object, object, int]] = (
            collections.deque(
                [
                    (serialization.SASL(b"tok"), None, 1),
                    (serialization.Delete("/9", -1), None, 9),
                ]
            )
        )
        dec = serialization.FrameDecoder(pop_pending=pending.popleft)
        # 16-byte SASL err=0 frame missing challenge triggers struct.error
        bad_body = _reply_header_struct.pack(1, 2, 0)
        bad_wire = _int_struct.pack(len(bad_body)) + bad_body
        good_body = _reply_header_struct.pack(9, 10, 0)
        good_wire = _int_struct.pack(len(good_body)) + good_body
        dec.feed(bad_wire + good_wire)

        with pytest.raises(
            ConnectionDropped, match="invalid server SASL response"
        ):
            dec.pop_packet()

        assert dec._frame_len == -1
        pkt = dec.pop_packet()
        assert isinstance(pkt, serialization.ResponsePacket)
        assert pkt.header == serialization.ReplyHeader(xid=9, zxid=10, err=0)
        assert dec._head == 0 and dec._tail == 0

    def test_frame_decoder_malformed_watch_and_oversize_sasl(self) -> None:
        pending: collections.deque[tuple[object, object, int]] = (
            collections.deque()
        )
        dec = serialization.FrameDecoder(
            max_buffer=256, pop_pending=pending.popleft
        )

        # 1) Truncated WATCH frame (16 bytes < 20-byte watch_header_struct)
        short_watch = _reply_header_struct.pack(-1, 1, 0)
        dec.feed(_int_struct.pack(len(short_watch)) + short_watch)
        with pytest.raises(
            ConnectionDropped, match="invalid server watch packet"
        ):
            dec.pop_packet()

        # 2) WATCH frame with invalid UTF-8 path bytes -> ConnectionDropped
        bad_path = b"\xff\xfe"
        bad_watch = (
            _reply_header_struct.pack(-1, 2, 0)
            + _int_int_struct.pack(1, 3)
            + _int_struct.pack(len(bad_path))
            + bad_path
        )
        dec.feed(_int_struct.pack(len(bad_watch)) + bad_watch)
        with pytest.raises(
            ConnectionDropped, match="invalid server watch packet"
        ):
            dec.pop_packet()

        # 3) Oversize reply for pending SASL request -> ConnectionDropped
        over_n = 256 + serialization.REPLY_OVERHEAD + 10
        pending.append((serialization.SASL(b"tok"), None, 12))
        over_sasl = (
            _int_struct.pack(over_n)
            + _reply_header_struct.pack(12, 3, 0)
            + b"x" * (over_n - 16)
        )
        dec.feed(over_sasl[:32])
        with pytest.raises(ConnectionDropped, match="Oversize SASL reply"):
            dec.pop_packet()

    def test_frame_decoder_rejects_frame_length_mismatch(self) -> None:
        pending: collections.deque[tuple[object, object, int]] = (
            collections.deque()
        )
        dec = serialization.FrameDecoder(
            max_buffer=4096, pop_pending=pending.popleft
        )
        stat = _stat_struct.pack(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)

        def feed(body: bytes) -> None:
            dec.feed(_int_struct.pack(len(body)) + body)

        # Request replies whose fields do not span the frame fail only
        # that request; later frames still decode.
        bad_replies = [
            # Last string overstated: slicing would clamp silently.
            (
                serialization.Create("/p", b"", [], 0),
                _int_struct.pack(10) + b"/p",
            ),
            # Last child name overstated.
            (
                serialization.GetChildren("/p", None),
                _int_struct.pack(2) + _write_string("a") + _int_struct.pack(5),
            ),
            # Path understated: the stat is misread and bytes are left over.
            (
                serialization.Create2("/p", b"", [], 0),
                _int_struct.pack(1) + b"/p" + stat,
            ),
        ]
        for xid, (request, body) in enumerate(bad_replies, start=1):
            pending.append((request, None, xid))
            feed(_reply_header_struct.pack(xid, 1, 0) + body)
            pkt = dec.pop_packet()
            assert isinstance(pkt, serialization.ResponsePacket)
            assert isinstance(pkt.error, MarshallingError)
            assert f"of {16 + len(body)} bytes" in str(pkt.error)

        pending.append((serialization.Exists("/p", None), None, 9))
        feed(_reply_header_struct.pack(9, 1, 0) + stat)
        pkt = dec.pop_packet()
        assert isinstance(pkt, serialization.ResponsePacket)
        assert pkt.error is None
        assert pkt.response == _make_stat()

        # Watch, SASL and connect frames have no request to fail: the
        # connection is dropped.
        feed(
            _reply_header_struct.pack(-1, 1, 0)
            + _int_int_struct.pack(1, 3)
            + _int_struct.pack(10)
            + b"/p"
        )
        with pytest.raises(
            ConnectionDropped, match="invalid server watch packet: parsed"
        ):
            dec.pop_packet()

        pending.append((serialization.SASL(b"tok"), None, 10))
        feed(_reply_header_struct.pack(10, 1, 0) + _int_struct.pack(8) + b"ch")
        with pytest.raises(
            ConnectionDropped, match="invalid server SASL response: parsed"
        ):
            dec.pop_packet()

        connect = _int_int_long_struct.pack(0, 10000, 7) + _write_buffer(b"pw")
        feed(connect + b"\x01" + b"junk")
        with pytest.raises(
            ConnectionDropped, match="invalid server connect response: parsed"
        ):
            dec.pop_packet()

        # The optional trailing read-only byte may be absent.
        feed(connect)
        pkt = dec.pop_packet()
        assert pkt == serialization.Connect(0, 0, 10000, 7, b"pw", False)

    def test_frame_decoder_feed_never_grows_buffer(self) -> None:
        pending: collections.deque[tuple[object, object, int]] = (
            collections.deque(
                (serialization.Delete("/p", -1), None, 1) for _ in range(1500)
            )
        )
        dec = serialization.FrameDecoder(
            max_buffer=256, pop_pending=pending.popleft
        )
        orig_cap = dec._capacity
        body = _reply_header_struct.pack(1, 2, 0) + b"x" * 200
        frame = _int_struct.pack(len(body)) + body
        burst = frame * 1500
        assert len(burst) > serialization.READ_WINDOW

        with pytest.raises(BufferError, match="pop frames"):
            dec.feed(burst)
        assert len(dec._buf) == orig_cap

        while dec.pop_packet() is not None:
            pass
        dec.reset()
        assert len(dec._buf) == orig_cap

    def test_coalesced_valid_reply_preceding_corrupt_header_is_delivered(
        self,
    ) -> None:
        """Corrupt trailing header does not discard earlier valid reply."""
        pending: collections.deque[tuple[object, object, int]] = (
            collections.deque(
                [
                    (serialization.Create("/a", b"", [], 0), "res1", 1),
                    (serialization.Create("/b", b"", [], 0), "res2", 2),
                ]
            )
        )
        dec = serialization.FrameDecoder(
            max_buffer=256, pop_pending=pending.popleft
        )

        # 1. Valid Create reply coalesced with msg_len < 16 corrupt header.
        valid_body = _reply_header_struct.pack(1, 10, 0) + _write_string("/a")
        valid_frame = _int_struct.pack(len(valid_body)) + valid_body
        corrupt_short = _int_struct.pack(4) + b"\x00" * 4

        buf = dec.get_buffer()
        coalesced = valid_frame + corrupt_short
        buf[: len(coalesced)] = coalesced
        dec.buffer_updated(len(coalesced))

        # Requesting a buffer before popping valid frames raises BufferError
        with pytest.raises(BufferError, match="pop frames"):
            dec.get_buffer()

        pkt = dec.pop_packet()
        assert isinstance(pkt, serialization.ResponsePacket)
        assert pkt.response == "/a"
        assert pkt.async_object == "res1"

        with pytest.raises(ConnectionDropped, match="Invalid frame length"):
            dec.pop_packet()

        # 2. Valid Create reply coalesced with oversize non-request xid (-1).
        dec.reset()
        valid_body2 = _reply_header_struct.pack(2, 11, 0) + _write_string("/b")
        valid_frame2 = _int_struct.pack(len(valid_body2)) + valid_body2
        oversize_watch = _int_struct.pack(1024) + _int_struct.pack(-1)

        buf = dec.get_buffer()
        coalesced2 = valid_frame2 + oversize_watch
        buf[: len(coalesced2)] = coalesced2
        dec.buffer_updated(len(coalesced2))

        pkt2 = dec.pop_packet()
        assert isinstance(pkt2, serialization.ResponsePacket)
        assert pkt2.response == "/b"
        assert pkt2.async_object == "res2"

        with pytest.raises(ConnectionDropped, match="Oversize frame length"):
            dec.pop_packet()

    def test_malformed_wire_fields_and_null_counts(self) -> None:
        """Negative lengths, null watch paths, counts, and bad multiheaders."""
        from kazoo.exceptions import MarshallingError, SystemZookeeperError

        stat_bytes = _stat_struct.pack(1, 2, 3, 4, 1, 0, 0, 0, 0, 0, 0)
        dec = serialization.FrameDecoder(max_buffer=4096)

        # 1. WatchPacket with path length -1 or -2 -> ConnectionDropped
        for bad_len in (-1, -2):
            watch_body = serialization.watch_header_struct.pack(
                -1, 5, 0, 1, 3
            ) + _int_struct.pack(bad_len)
            dec.feed(_int_struct.pack(len(watch_body)) + watch_body)
            with pytest.raises(
                ConnectionDropped, match="invalid server watch packet"
            ):
                dec.pop_packet()

        # 2. GetACL & GetChildren2 with count == -1 unpack stat -> ([], stat)
        acls_res, end = serialization.GetACL.deserialize(
            _int_struct.pack(-1) + stat_bytes, 0
        )
        assert isinstance(acls_res, tuple)
        assert acls_res[0] == []
        assert acls_res[1].czxid == 1
        assert end == 4 + len(stat_bytes)

        ch2_res, end2 = serialization.GetChildren2.deserialize(
            _int_struct.pack(-1) + stat_bytes, 0
        )
        assert isinstance(ch2_res, tuple)
        assert ch2_res[0] == []
        assert ch2_res[1].czxid == 1
        assert end2 == 4 + len(stat_bytes)

        # count == -2 raises MarshallingError
        for cls in (
            serialization.GetACL,
            serialization.GetChildren,
            serialization.GetChildren2,
        ):
            with pytest.raises(MarshallingError):
                cls.deserialize(_int_struct.pack(-2) + stat_bytes, 0)

        # 3. Transaction: "" path kept, unknown err -> SystemZookeeperError,
        # done=2 and unknown op_type -> MarshallingError
        tx_ok = (
            serialization.multiheader_struct.pack(
                serialization.Create.type, 0, -1
            )
            + _int_struct.pack(0)
            + serialization.multiheader_struct.pack(-1, 0, -1)
            + _int_struct.pack(-999)
            + serialization.multiheader_struct.pack(-1, 1, -1)
        )
        results, _ = serialization.Transaction.deserialize(tx_ok, 0)
        assert results[0] == ""
        assert isinstance(results[1], SystemZookeeperError)

        tx_bad_done = serialization.multiheader_struct.pack(-1, 2, -1)
        with pytest.raises(MarshallingError, match="done flag"):
            serialization.Transaction.deserialize(tx_bad_done, 0)

        tx_bad_op = serialization.multiheader_struct.pack(
            999, 0, -1
        ) + serialization.multiheader_struct.pack(-1, 1, -1)
        with pytest.raises(MarshallingError, match="Unknown transaction"):
            serialization.Transaction.deserialize(tx_bad_op, 0)

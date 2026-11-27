from __future__ import annotations

import ssl
from dataclasses import FrozenInstanceError
from unittest.mock import patch

import pytest

from kazoo.client import KazooClient
from kazoo.protocol.tls import TLSConfig, build_ssl_context


class TestTLSConfig:
    def test_defaults_and_immutability(self) -> None:
        cfg = TLSConfig()
        assert cfg.use_ssl is False
        assert cfg.ca is None
        assert cfg.certfile is None
        assert cfg.keyfile is None
        assert cfg.keyfile_password is None
        assert cfg.verify_certs is True
        assert cfg.check_hostname is False
        assert cfg.options is None
        assert cfg.ciphers is None
        with pytest.raises(FrozenInstanceError):
            cfg.use_ssl = True  # type: ignore[misc]

    def test_validation_rejects_check_hostname_without_verify(self) -> None:
        with pytest.raises(ValueError, match="verify_certs must be True"):
            TLSConfig(use_ssl=True, verify_certs=False, check_hostname=True)

        # When use_ssl=False, TLSConfig allows inactive defaults/combinations,
        # while build_ssl_context still enforces the invariant.
        inactive_cfg = TLSConfig(
            use_ssl=False, verify_certs=False, check_hostname=True
        )
        with pytest.raises(ValueError, match="verify_certs must be True"):
            build_ssl_context(inactive_cfg)

    def test_client_init_validates_tls_config_eagerly(self) -> None:
        with pytest.raises(ValueError, match="verify_certs must be True"):
            KazooClient(
                hosts="127.0.0.1:2181",
                use_ssl=True,
                verify_certs=False,
                check_hostname=True,
            )

        client = KazooClient(
            hosts="127.0.0.1:2181",
            use_ssl=True,
            ca="/tmp/ca.pem",
            certfile="/tmp/cert.pem",
            keyfile="/tmp/key.pem",
            keyfile_password="secret",
            verify_certs=True,
            check_hostname=True,
        )
        assert client.tls_config == TLSConfig(
            use_ssl=True,
            ca="/tmp/ca.pem",
            certfile="/tmp/cert.pem",
            keyfile="/tmp/key.pem",
            keyfile_password="secret",
            verify_certs=True,
            check_hostname=True,
        )


class TestBuildSSLContext:
    def test_default_options_and_verify_required(self) -> None:
        ctx = build_ssl_context(TLSConfig(use_ssl=True))
        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.check_hostname is False
        assert (ctx.options & ssl.OP_NO_SSLv2) == ssl.OP_NO_SSLv2
        assert (ctx.options & ssl.OP_NO_SSLv3) == ssl.OP_NO_SSLv3

    def test_verify_certs_false_and_check_hostname_true(self) -> None:
        ctx_no_verify = build_ssl_context(
            TLSConfig(use_ssl=True, verify_certs=False, check_hostname=False)
        )
        assert ctx_no_verify.verify_mode == ssl.CERT_NONE
        assert ctx_no_verify.check_hostname is False

        ctx_check = build_ssl_context(
            TLSConfig(use_ssl=True, verify_certs=True, check_hostname=True)
        )
        assert ctx_check.verify_mode == ssl.CERT_REQUIRED
        assert ctx_check.check_hostname is True

    def test_custom_options_ciphers_ca_and_cert_chain(self) -> None:
        custom_opts = ssl.OP_NO_COMPRESSION
        cfg = TLSConfig(
            use_ssl=True,
            ca="/fake/ca.pem",
            certfile="/fake/cert.pem",
            keyfile="/fake/key.pem",
            keyfile_password="pw",
            options=custom_opts,
            ciphers="HIGH:!aNULL",
        )
        with (
            patch.object(ssl.SSLContext, "set_ciphers") as mock_ciphers,
            patch.object(ssl.SSLContext, "load_verify_locations") as mock_ca,
            patch.object(ssl.SSLContext, "load_cert_chain") as mock_chain,
        ):
            ctx = build_ssl_context(cfg)
            assert bool(ctx.options & ssl.OP_NO_COMPRESSION)
            mock_ciphers.assert_called_once_with("HIGH:!aNULL")
            mock_ca.assert_called_once_with("/fake/ca.pem")
            mock_chain.assert_called_once_with(
                certfile="/fake/cert.pem",
                keyfile="/fake/key.pem",
                password="pw",
            )

    def test_client_tls_config_dynamic_and_ssl_context_cached(self) -> None:
        """[L-3][E-3] tls_config reflects mutations; SSLContext is cached."""
        client = KazooClient(
            hosts="127.0.0.1:2181",
            use_ssl=True,
            verify_certs=True,
            check_hostname=False,
        )
        with patch(
            "kazoo.client.build_ssl_context", wraps=build_ssl_context
        ) as spy_build:
            ctx1 = client._get_ssl_context()
            ctx2 = client._get_ssl_context()
            assert ctx1 is not None
            assert ctx1 is ctx2
            assert spy_build.call_count == 1

            # Mutating a TLS attribute updates tls_config and rebuilds cache.
            client.verify_certs = False
            assert client.tls_config.verify_certs is False
            ctx3 = client._get_ssl_context()
            assert ctx3 is not None
            assert ctx3 is not ctx1
            assert ctx3.verify_mode == ssl.CERT_NONE
            assert spy_build.call_count == 2

            # Disabling use_ssl returns None without calling build_ssl_context.
            client.use_ssl = False
            assert client._get_ssl_context() is None
            assert spy_build.call_count == 2

    def test_create_tcp_connection_reuses_supplied_ssl_context(self) -> None:
        """[E-3] create_tcp_connection reuses supplied ssl_context."""
        import socket
        from unittest.mock import Mock

        from kazoo.handlers.utils import create_tcp_connection

        prebuilt = build_ssl_context(TLSConfig(use_ssl=True))
        wrapped = Mock()
        with (
            patch(
                "kazoo.handlers.utils.build_ssl_context",
                side_effect=AssertionError("should not rebuild"),
            ),
            patch("kazoo.handlers.utils._set_default_tcpsock_options"),
            patch.object(
                prebuilt, "wrap_socket", return_value=wrapped
            ) as mock_wrap,
        ):
            sock = create_tcp_connection(
                socket,
                ("127.0.0.1", 2181),
                hostname="zk.example",
                timeout=1.0,
                use_ssl=True,
                ssl_context=prebuilt,
            )
            assert sock is wrapped
            mock_wrap.assert_called_once()
            wrapped.connect.assert_called_once_with(("127.0.0.1", 2181))

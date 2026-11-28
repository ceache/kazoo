"""Typed TLS configuration and SSLContext construction for ZooKeeper."""

from __future__ import annotations

import ssl
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TLSConfig:
    """Typed TLS configuration for ZooKeeper client connections."""

    use_ssl: bool = False
    ca: str | None = None
    certfile: str | None = None
    keyfile: str | None = None
    keyfile_password: str | None = None
    verify_certs: bool = True
    check_hostname: bool = False
    options: ssl.Options | None = None
    ciphers: str | None = None

    def __post_init__(self) -> None:
        if self.use_ssl and self.check_hostname and not self.verify_certs:
            raise ValueError(
                "verify_certs must be True when check_hostname is True"
            )


def build_ssl_context(config: TLSConfig) -> ssl.SSLContext:
    """Construct an :class:`ssl.SSLContext` from a :class:`TLSConfig`."""
    # Disallow SSLv2 and SSLv3 (requiring TLSv1.0+) unless overridden.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    if config.options is not None:
        context.options = config.options
    else:
        context.options |= ssl.OP_NO_SSLv2
        context.options |= ssl.OP_NO_SSLv3

    if config.ciphers:
        context.set_ciphers(config.ciphers)

    # Load default CA certs
    context.load_default_certs(ssl.Purpose.SERVER_AUTH)
    if config.check_hostname and not config.verify_certs:
        raise ValueError(
            "verify_certs must be True when check_hostname is True"
        )
    # check_hostname must be set to False prior to setting verify_mode to
    # CERT_NONE.
    context.check_hostname = config.check_hostname
    context.verify_mode = (
        ssl.CERT_REQUIRED if config.verify_certs else ssl.CERT_NONE
    )
    if config.ca:
        context.load_verify_locations(config.ca)
    if config.certfile and config.keyfile:
        context.load_cert_chain(
            certfile=config.certfile,
            keyfile=config.keyfile,
            password=config.keyfile_password,
        )
    return context


__all__ = (
    "TLSConfig",
    "build_ssl_context",
)
